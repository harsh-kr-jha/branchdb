/*
 * Native branch lifecycle. Catalog retirement is transactional; physical unlink
 * is delegated to core dependency/storage code only after reachability permits.
 * Session locks use a private lock method, so SQL advisory unlock and DISCARD ALL
 * cannot remove the connection fence.
 */
#include "postgres.h"
#include "access/branchheap.h"
#include "access/xact.h"
#include "access/heapam.h"
#include "access/genam.h"
#include "catalog/pg_namespace_d.h"
#include "catalog/dependency.h"
#include "catalog/index.h"
#include "commands/defrem.h"
#include "commands/tablecmds.h"
#include "commands/repack.h"
#include "parser/parse_utilcmd.h"
#include "parser/parse_node.h"
#include "catalog/namespace.h"
#include "catalog/pg_class.h"
#include "catalog/pg_authid_d.h"
#include "catalog/pg_am_d.h"
#include "catalog/pg_constraint.h"
#include "catalog/pg_type.h"
#include "executor/spi.h"
#include "fmgr.h"
#include "miscadmin.h"
#include "nodes/makefuncs.h"
#include "storage/lock.h"
#include "storage/lwlock.h"
#include "storage/proc.h"
#include "utils/acl.h"
#include "utils/builtins.h"
#include "utils/hsearch.h"
#include "utils/inval.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/snapmgr.h"
#include "utils/syscache.h"
#include "utils/timestamp.h"
#include <signal.h>

bool		BranchLifecycleEnabled;
static int64 session_id = -1;
typedef struct RetiredKey
{
	Oid			catalog;
	Oid			object;
} RetiredKey;
static HTAB *retired;
static MemoryContext retired_context;
static bool refreshing;
static bool reclaiming;

static Datum
value(HeapTuple tuple, TupleDesc desc, int column)
{
	bool		isnull;
	Datum		datum = SPI_getbinval(tuple, desc, column, &isnull);

	if (isnull)
		elog(ERROR, "null native lifecycle metadata");
	return datum;
}

static void
query(const char *sql, bool readonly)
{
	BranchInternalQuery(sql, GetLatestSnapshot(), readonly);
}

static LOCKTAG
session_tag(int64 id, uint32 kind)
{
	LOCKTAG		tag;

	if (id < 0 || id > PG_UINT32_MAX)
		ereport(ERROR, (errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED), errmsg("native branch ID exceeds lifecycle lock range")));
	SET_LOCKTAG_ADVISORY(tag, MyDatabaseId, 0x42524e43, (uint32) id, kind);
	tag.locktag_lockmethodid = NATIVE_LOCKMETHOD;
	return tag;
}

int64
BranchLifecycleSessionId(void)
{
	return session_id;
}

void
BranchLifecycleSession(int64 id)
{
	LOCKTAG		gate = session_tag(id, 5),
				session = session_tag(id, 4);

	LockAcquire(&gate, ShareLock, false, false);
	LockAcquire(&session, ShareLock, true, false);
	SPI_connect();
	query(psprintf("SELECT id FROM branch_native.branches WHERE id=" INT64_FORMAT " AND name IS NOT NULL", id), true);
	if (SPI_processed != 1)
		ereport(FATAL, (errcode(ERRCODE_UNDEFINED_OBJECT), errmsg("native branch was deleted during connection startup")));
	SPI_finish();
	session_id = id;
	LWLockAcquire(ProcArrayLock, LW_EXCLUSIVE);
	MyProc->branchSessionId = id;
	LWLockRelease(ProcArrayLock);
	/* The transaction-scoped gate survives until startup commits. */
}

bool
BranchObjectRetired(Oid catalog, Oid object)
{
	RetiredKey	key = {catalog, object};

	return BranchLifecycleEnabled && retired && hash_search(retired, &key, HASH_FIND, NULL) != NULL;
}

void
BranchLifecycleRefresh(void)
{
	Oid			ns;
	HASHCTL		ctl = {0};
	HTAB	   *next;
	MemoryContext next_context;
	uint64		i;

	if (refreshing)
		return;
	ns = get_namespace_oid("branch_native", true);
	BranchLifecycleEnabled = OidIsValid(ns) && OidIsValid(get_relname_relid("storage", ns));
	if (!BranchLifecycleEnabled)
		return;
	refreshing = true;
	next_context = AllocSetContextCreate(TopMemoryContext, "retired native catalogs", ALLOCSET_DEFAULT_SIZES);
	ctl.keysize = sizeof(RetiredKey);
	ctl.entrysize = sizeof(RetiredKey);
	ctl.hcxt = next_context;
	next = hash_create("retired native objects", 32, &ctl, HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
	PG_TRY();
	{
		SPI_connect();
		query("SELECT catalog,object FROM branch_native.retired", true);
		for (i = 0; i < SPI_processed; i++)
		{
			RetiredKey	key;

			key.catalog = DatumGetObjectId(value(SPI_tuptable->vals[i], SPI_tuptable->tupdesc, 1));
			key.object = DatumGetObjectId(value(SPI_tuptable->vals[i], SPI_tuptable->tupdesc, 2));
			hash_search(next, &key, HASH_ENTER, NULL);
		}
		SPI_finish();
		if (retired_context)
			MemoryContextDelete(retired_context);
		retired_context = next_context;
		retired = next;
		refreshing = false;
	}
	PG_CATCH();
	{
		MemoryContextDelete(next_context);
		refreshing = false;
		PG_RE_THROW();
	}
	PG_END_TRY();
}

/*
 * A durable storage ID survives retirement of a logical table/index descriptor.
 * Its canonical descriptor is retained while aliases or versions need it.
 */
static void
sync_storage(void)
{
	query("INSERT INTO branch_native.storage(id,root,generation) SELECT nextval('branch_native.storage_ids'),x.root,x.generation FROM (SELECT DISTINCT "
		  "COALESCE(NULLIF(o.source,0),o.object) AS root,c.relfilenode::bigint AS generation "
		  "FROM branch_native.objects o JOIN pg_class c ON c.oid=o.object "
		  "WHERE o.catalog='pg_class'::regclass AND c.relfilenode<>0 AND c.relkind IN ('r','i','t')) x "
		  "WHERE NOT EXISTS(SELECT FROM branch_native.storage s WHERE s.root=x.root AND s.generation=x.generation) "
		  "ON CONFLICT(root,generation) DO NOTHING", false);
	query("INSERT INTO branch_native.storage_refs(object,storage) SELECT o.object,s.id "
		  "FROM branch_native.objects o JOIN pg_class c ON c.oid=o.object "
		  "JOIN branch_native.storage s ON s.root=COALESCE(NULLIF(o.source,0),o.object) AND s.generation=c.relfilenode::bigint "
		  "WHERE o.catalog='pg_class'::regclass AND c.relfilenode<>0 AND c.relkind IN ('r','i','t') "
		  "ON CONFLICT(object) DO UPDATE SET storage=excluded.storage", false);
}

static void
invalidate(void)
{
	CacheInvalidateCatalog(RelationRelationId);
	CacheInvalidateCatalog(TypeRelationId);
	CacheInvalidateCatalog(ConstraintRelationId);
	CacheInvalidateRelcacheAll();
	CommandCounterIncrement();
	InvalidateSystemCaches();
	BranchCatalogPrepare();
}

/*
 * Archive a table's complete descriptor graph. This retains divergent tuple
 * layouts until the last version needing that birth descriptor is collected.
 */
static void
retire_relation(Oid relid)
{
	char	   *graph = psprintf("WITH ids AS (SELECT %u::oid AS oid UNION SELECT indexrelid FROM pg_index WHERE indrelid=%u "
								 "UNION SELECT reltoastrelid FROM pg_class WHERE oid=%u AND reltoastrelid<>0 "
								 "UNION SELECT i.indexrelid FROM pg_index i JOIN pg_class c ON c.reltoastrelid=i.indrelid WHERE c.oid=%u) ", relid, relid, relid, relid);

	query(psprintf("%s INSERT INTO branch_native.retired SELECT 'pg_class'::regclass,oid FROM ids ON CONFLICT DO NOTHING", graph), false);
	query(psprintf("%s INSERT INTO branch_native.retired SELECT 'pg_type'::regclass,t.oid FROM ids JOIN pg_class c USING(oid) "
				   "JOIN pg_type t ON t.oid=c.reltype OR t.typelem=c.reltype WHERE c.reltype<>0 ON CONFLICT DO NOTHING", graph), false);
	query(psprintf("INSERT INTO branch_native.retired SELECT 'pg_constraint'::regclass,oid FROM pg_constraint WHERE conrelid=%u ON CONFLICT DO NOTHING", relid), false);
	query(psprintf("INSERT INTO branch_native.cleanup VALUES(%u) ON CONFLICT DO NOTHING", relid), false);
}

static void
terminate_sessions(int64 id, LOCKTAG *tag)
{
	int			count,
				i;
	VirtualTransactionId *holders = GetLockConflicts(tag, ExclusiveLock, &count);
	List	   *pids = NIL;
	ListCell   *cell;

	LWLockAcquire(ProcArrayLock, LW_SHARED);
	for (i = 0; i < count; i++)
	{
		PGPROC	   *proc = &ProcGlobal->allProcs[holders[i].procNumber];

		if (proc->databaseId == MyDatabaseId && proc->branchSessionId == id && proc->pid > 0)
			pids = lappend_int(pids, proc->pid);
	}
	LWLockRelease(ProcArrayLock);
	pfree(holders);
	foreach(cell, pids) (void) kill(lfirst_int(cell), SIGTERM);
	list_free(pids);
}

Datum
branch_drop(PG_FUNCTION_ARGS)
{
	char	   *name = text_to_cstring(PG_GETARG_TEXT_PP(0));
	bool		force = PG_GETARG_BOOL(1);
	int64		id;
	LOCKTAG		gate,
				session,
				admission;
	TimestampTz started;
	Oid		   *tables;
	uint64		n,
				i;
	MemoryContext caller = CurrentMemoryContext;

	if (!superuser())
		ereport(ERROR, (errcode(ERRCODE_INSUFFICIENT_PRIVILEGE), errmsg("branch deletion requires a superuser")));
	if (XactReadOnly)
		ereport(ERROR, (errcode(ERRCODE_READ_ONLY_SQL_TRANSACTION), errmsg("branch deletion requires a writable transaction")));
	if (!BranchLifecycleEnabled || strcmp(BranchName, "main") != 0)
		ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED), errmsg("branch deletion requires format 2 and a main connection")));
	if (session_id < 0)
		ereport(ERROR, (errcode(ERRCODE_INVALID_AUTHORIZATION_SPECIFICATION), errmsg("reconnect after native lifecycle initialization")));
	if (IsolationUsesXactSnapshot())
		ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED), errmsg("branch deletion requires READ COMMITTED")));
	if (strcmp(name, "main") == 0)
		ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE), errmsg("main cannot be deleted")));
	SPI_connect();
	query(psprintf("SELECT id FROM branch_native.branches WHERE name=%s", quote_literal_cstr(name)), true);
	if (SPI_processed != 1)
		ereport(ERROR, (errcode(ERRCODE_UNDEFINED_OBJECT), errmsg("native branch does not exist")));
	id = DatumGetInt64(value(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1));
	if (id == 0)
		ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE), errmsg("main cannot be deleted")));
	SPI_finish();
	gate = session_tag(id, 5);
	session = session_tag(id, 4);
	started = GetCurrentTimestamp();
	while (LockAcquire(&gate, ExclusiveLock, false, true) == LOCKACQUIRE_NOT_AVAIL)
	{
		if (TimestampDifferenceExceeds(started, GetCurrentTimestamp(), 10000))
			ereport(ERROR, (errcode(ERRCODE_LOCK_NOT_AVAILABLE), errmsg("timed out fencing branch connections")));
		CHECK_FOR_INTERRUPTS();
		pg_usleep(10000L);
	}
	while (LockAcquire(&session, ExclusiveLock, false, true) == LOCKACQUIRE_NOT_AVAIL)
	{
		if (!force)
			ereport(ERROR, (errcode(ERRCODE_OBJECT_IN_USE), errmsg("branch has connected sessions"), errhint("Disconnect them or use branch_drop(name, true).")));
		if (TimestampDifferenceExceeds(started, GetCurrentTimestamp(), 10000))
			ereport(ERROR, (errcode(ERRCODE_LOCK_NOT_AVAILABLE), errmsg("timed out terminating branch sessions")));
		terminate_sessions(id, &session);
		CHECK_FOR_INTERRUPTS();
		pg_usleep(10000L);
	}
	SET_LOCKTAG_ADVISORY(admission, MyDatabaseId, 0x42524e43, 0, 1);
	admission.locktag_lockmethodid = NATIVE_LOCKMETHOD;
	while (LockAcquire(&admission, ExclusiveLock, false, true) == LOCKACQUIRE_NOT_AVAIL)
	{
		if (LockHeldByMe(&admission, ShareLock, false))
			ereport(ERROR, (errcode(ERRCODE_T_R_SERIALIZATION_FAILURE), errmsg("retry transaction to obtain native deletion admission")));
		if (TimestampDifferenceExceeds(started, GetCurrentTimestamp(), 10000))
			ereport(ERROR, (errcode(ERRCODE_LOCK_NOT_AVAILABLE), errmsg("timed out waiting for native transactions")));
		CHECK_FOR_INTERRUPTS();
		pg_usleep(10000L);
	}
	BranchMaintenanceAdmission();
	BranchCatalogPrepare();
	BranchCatalogBypass(true);
	PG_TRY();
	{
		SPI_connect();
		query(psprintf("SELECT id FROM branch_native.branches WHERE id=" INT64_FORMAT " AND name=%s", id, quote_literal_cstr(name)), true);
		if (SPI_processed != 1)
			ereport(ERROR, (errcode(ERRCODE_UNDEFINED_OBJECT), errmsg("native branch was already deleted")));
		sync_storage();
		query(psprintf("SELECT c.oid FROM pg_class c JOIN branch_native.objects o ON o.object=c.oid AND o.catalog='pg_class'::regclass "
					   "WHERE o.owner=" INT64_FORMAT " AND c.relnamespace='public'::regnamespace AND c.relkind='r' "
					   "AND NOT EXISTS(SELECT FROM branch_native.retired r WHERE r.catalog='pg_class'::regclass AND r.object=c.oid)", id), true);
		n = SPI_processed;
		tables = MemoryContextAlloc(caller, sizeof(Oid) * Max(n, 1));
		for (i = 0; i < n; i++)
			tables[i] = DatumGetObjectId(value(SPI_tuptable->vals[i], SPI_tuptable->tupdesc, 1));
		for (i = 0; i < n; i++)
			retire_relation(tables[i]);
		pfree(tables);
		query(psprintf("DELETE FROM branch_native.roles WHERE branch=" INT64_FORMAT, id), false);
		query(psprintf("UPDATE branch_native.branches SET name=NULL WHERE id=" INT64_FORMAT, id), false);
		SPI_finish();
		BranchCatalogBypass(false);
		invalidate();
	}
	PG_CATCH();
	{
		BranchCatalogBypass(false);
		PG_RE_THROW();
	} PG_END_TRY();
	PG_RETURN_VOID();
}

/*
 * Core drop must not unlink a file still named by any other descriptor. This
 * hook is active only during our fenced cleanup, never during arbitrary DDL.
 */
bool
BranchStorageMayUnlink(Relation relation)
{
	bool		allow;

	if (!reclaiming)
		return true;
	SPI_connect();
	query(psprintf("SELECT 1 FROM pg_class WHERE oid<>%u AND relfilenode=%u AND reltablespace=%u LIMIT 1",
				   RelationGetRelid(relation), relation->rd_rel->relfilenode, relation->rd_rel->reltablespace), true);
	allow = SPI_processed == 0;
	SPI_finish();
	return allow;
}

Datum
branch_cleanup(PG_FUNCTION_ARGS)
{
	Oid		   *ids;
	uint64		i,
				n;
	int64		removed = 0;
	MemoryContext caller = CurrentMemoryContext;

	BranchMaintenanceAdmission();
	BranchCatalogPrepare();
	if (!BranchLifecycleEnabled)
		PG_RETURN_INT64(0);
	BranchCatalogBypass(true);
	PG_TRY();
	{
		SPI_connect();
		sync_storage();
		query("SELECT q.object FROM branch_native.cleanup q JOIN pg_class c ON c.oid=q.object ORDER BY q.object", true);
		n = SPI_processed;
		ids = MemoryContextAlloc(caller, sizeof(Oid) * Max(n, 1));
		for (i = 0; i < n; i++)
			ids[i] = DatumGetObjectId(value(SPI_tuptable->vals[i], SPI_tuptable->tupdesc, 1));
		SPI_finish();
		for (i = 0; i < n; i++)
		{
			bool		keep;
			ObjectAddress address;

			SPI_connect();
			query(psprintf("SELECT 1 FROM branch_native.objects o JOIN pg_class c ON c.oid=o.object "
						   "WHERE o.catalog='pg_class'::regclass AND o.object=%u AND ("

			/*
			 * Retain this birth layout while any version created with it
			 * remains.
			 */
						   "EXISTS(SELECT FROM branch_native.versions v WHERE v.branch=o.owner AND v.relid=COALESCE(NULLIF(o.source,0),o.object) AND v.generation=c.relfilenode::bigint) OR "

			/*
			 * Canonical descriptors are also physical-access/extension
			 * anchors.
			 */
						   "(o.source=o.object AND EXISTS(SELECT FROM branch_native.versions v WHERE v.relid=o.object AND v.generation=c.relfilenode::bigint)) OR "
						   "EXISTS(SELECT FROM branch_native.storage s JOIN branch_native.storage_refs r ON r.storage=s.id "
						   "WHERE s.root=o.object AND (r.object<>o.object AND NOT EXISTS(SELECT FROM branch_native.retired t WHERE t.catalog='pg_class'::regclass AND t.object=r.object))))", ids[i]), true);
			keep = SPI_processed > 0;
			SPI_finish();
			if (keep)
				continue;
			ObjectAddressSet(address, RelationRelationId, ids[i]);
			reclaiming = true;
			performDeletion(&address, DROP_CASCADE, PERFORM_DELETION_INTERNAL | PERFORM_DELETION_QUIETLY);
			reclaiming = false;
			CommandCounterIncrement();
			removed++;
		}
		pfree(ids);
		SPI_connect();
		query("DELETE FROM branch_native.cleanup q WHERE NOT EXISTS(SELECT FROM pg_class c WHERE c.oid=q.object)", false);
		query("DELETE FROM branch_native.objects o WHERE (o.catalog='pg_class'::regclass AND NOT EXISTS(SELECT FROM pg_class c WHERE c.oid=o.object)) "
			  "OR (o.catalog='pg_type'::regclass AND NOT EXISTS(SELECT FROM pg_type t WHERE t.oid=o.object)) "
			  "OR (o.catalog='pg_constraint'::regclass AND NOT EXISTS(SELECT FROM pg_constraint k WHERE k.oid=o.object))", false);
		query("DELETE FROM branch_native.retired r WHERE NOT EXISTS(SELECT FROM branch_native.objects o WHERE o.catalog=r.catalog AND o.object=r.object)", false);
		query("DELETE FROM branch_native.storage_refs r WHERE NOT EXISTS(SELECT FROM pg_class c WHERE c.oid=r.object)", false);
		query("DELETE FROM branch_native.storage s WHERE NOT EXISTS(SELECT FROM branch_native.storage_refs r WHERE r.storage=s.id)", false);
		query("DELETE FROM branch_native.branches b WHERE b.name IS NULL AND NOT EXISTS(SELECT FROM branch_native.versions v WHERE v.branch=b.id) "
			  "AND NOT EXISTS(SELECT FROM branch_native.roles r WHERE r.branch=b.id)", false);
		SPI_finish();
		BranchCatalogBypass(false);
		invalidate();
	}
	PG_CATCH();
	{
		reclaiming = false;
		BranchCatalogBypass(false);
		PG_RE_THROW();
	} PG_END_TRY();
	PG_RETURN_INT64(removed);
}

/*
 * Move the old family's access anchor, while preserving its durable storage
 * identity. The visible logical relation keeps its OID and starts a new family.
 */
static void
retarget(Oid current, Oid archive, Oid root, RelFileNumber generation)
{
	Oid			target = root == current ? archive : root;

	if (root == current)
	{
		query(psprintf("UPDATE branch_native.versions SET relid=%u WHERE relid=%u AND generation=%u", archive, root, generation), false);
		query(psprintf("UPDATE branch_native.objects SET source=%u WHERE catalog='pg_class'::regclass AND source=%u", archive, root), false);
		query(psprintf("UPDATE branch_native.storage SET root=%u WHERE root=%u AND generation=%u", archive, root, generation), false);
	}
	query(psprintf("UPDATE branch_native.objects SET source=%u WHERE catalog='pg_class'::regclass AND object=%u", current, current), false);
	query(psprintf("UPDATE branch_native.objects SET source=%u WHERE catalog='pg_class'::regclass AND object=%u", target, archive), false);
	CommandCounterIncrement();
}

static void
truncate_relation(Oid relid)
{
	Relation	old;
	Oid			holder,
				root;
	RelFileNumber generation;
	List	   *indexes;
	ListCell   *cell;
	Oid			saved;
	int			security;

	GetUserIdAndSecContext(&saved, &security);
	old = table_open(relid, NoLock);
	root = BranchStorageRelation(relid);
	generation = old->rd_locator.relNumber;
	indexes = list_copy(RelationGetIndexList(old));
	holder = InvalidOid;

	/*
	 * Creating retained descriptors is an internal storage operation, even
	 * when the caller has TRUNCATE but not CREATE privileges.
	 */
	SetUserIdAndSecContext(BOOTSTRAP_SUPERUSERID, security | SECURITY_LOCAL_USERID_CHANGE | SECURITY_RESTRICTED_OPERATION);
	BranchCatalogInternal = true;
	PG_TRY();
	{
		holder = make_new_heap(relid, old->rd_rel->reltablespace, old->rd_rel->relam, old->rd_rel->relpersistence, AccessExclusiveLock);
		foreach(cell, indexes)
		{
			Oid			indexid = lfirst_oid(cell),
						constraint,
						oldroot;
			RelFileNumber oldgeneration;
			Relation	index = index_open(indexid, AccessExclusiveLock);
			RangeVar   *rv = makeRangeVar("public", get_rel_name(holder), -1);
			IndexStmt  *stmt = generateClonedIndexStmt(rv, index, NULL, &constraint);
			ParseState *state = make_parsestate(NULL);
			ObjectAddress created;

			oldroot = BranchStorageRelation(indexid);
			oldgeneration = index->rd_locator.relNumber;
			stmt->idxname = psprintf("branch_retained_%u_%u", holder, indexid);
			stmt->primary = false;
			stmt->isconstraint = false;
			index_close(index, NoLock);
			created = DefineIndex(state, holder, stmt, InvalidOid, InvalidOid, InvalidOid, -1, false, false, false, false, true);
			free_parsestate(state);
			CommandCounterIncrement();
			BranchSwapRelationFiles(indexid, created.objectId);
			SPI_connect();
			retarget(indexid, created.objectId, oldroot, oldgeneration);
			SPI_finish();
		}
		BranchSwapRelationFiles(relid, holder);
		SPI_connect();
		retarget(relid, holder, root, generation);
		retire_relation(holder);
		SPI_finish();
		BranchCatalogInternal = false;
		SetUserIdAndSecContext(saved, security);
	}
	PG_CATCH();
	{
		BranchCatalogInternal = false;
		SetUserIdAndSecContext(saved, security);
		PG_RE_THROW();
	}
	PG_END_TRY();
	table_close(old, NoLock);
	list_free(indexes);
}

bool
BranchLifecycleUtility(Node *node)
{
	List	   *relations = NIL;
	ListCell   *cell;
	bool		truncating = IsA(node, TruncateStmt);

	if (!BranchLifecycleEnabled || BranchCatalogInternal || !IsTransactionState() || IsAbortedTransactionBlockState())
		return false;
	if (!truncating && (!IsA(node, DropStmt) || (((DropStmt *) node)->removeType != OBJECT_TABLE && ((DropStmt *) node)->removeType != OBJECT_INDEX)))
		return false;
	if (XactReadOnly)
		ereport(ERROR, (errcode(ERRCODE_READ_ONLY_SQL_TRANSACTION), errmsg("native lifecycle requires a writable transaction")));
	if (truncating)
	{
		TruncateStmt *stmt = (TruncateStmt *) node;

		if (stmt->restart_seqs)
			ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED), errmsg("native TRUNCATE RESTART IDENTITY is not implemented")));
		foreach(cell, stmt->relations)
		{
			Oid			id = RangeVarGetRelid(lfirst_node(RangeVar, cell), AccessExclusiveLock, false);

			relations = list_append_unique_oid(relations, id);
		}
	}
	else
	{
		DropStmt   *stmt = (DropStmt *) node;

		if (stmt->concurrent)
			ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED), errmsg("native concurrent DROP INDEX is not supported")));
		foreach(cell, stmt->objects)
		{
			Oid			id = RangeVarGetRelid(makeRangeVarFromNameList(lfirst(cell)), AccessExclusiveLock, stmt->missing_ok);

			if (OidIsValid(id))
				relations = list_append_unique_oid(relations, id);
		}
	}

	/*
	 * Resolve and lock names before admission: a reader can already hold a
	 * relation lock while waiting to enter the native executor.
	 */
	foreach(cell, relations)
	{
		Oid			id = lfirst_oid(cell);
		Relation	rel = relation_open(id, NoLock);
		bool		index = rel->rd_rel->relkind == RELKIND_INDEX;
		Oid			tableid = index ? IndexGetRelation(id, false) : id;

		if (rel->rd_rel->relnamespace != PG_PUBLIC_NAMESPACE || get_rel_relam(tableid) != BRANCH_HEAP_TABLE_AM_OID ||
			(truncating && index) || (!truncating && (((DropStmt *) node)->removeType == OBJECT_INDEX) != index))
			ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED), errmsg("native DROP/TRUNCATE supports branch-owned public tables and indexes only")));
		if (truncating)
		{
			if (pg_class_aclcheck(id, GetUserId(), ACL_TRUNCATE) != ACLCHECK_OK)
				aclcheck_error(ACLCHECK_NO_PRIV, OBJECT_TABLE, RelationGetRelationName(rel));
		}
		else if (!object_ownercheck(RelationRelationId, id, GetUserId()))
			aclcheck_error(ACLCHECK_NOT_OWNER, index ? OBJECT_INDEX : OBJECT_TABLE, RelationGetRelationName(rel));
		if (index && OidIsValid(get_index_constraint(id)))
			ereport(ERROR, (errcode(ERRCODE_DEPENDENT_OBJECTS_STILL_EXIST), errmsg("cannot drop a constraint index directly")));
		CheckTableNotInUse(rel, truncating ? "TRUNCATE" : "DROP");
		relation_close(rel, NoLock);
	}
	BranchLifecycleAdmission();
	BranchCatalogPrepare();
	BranchCatalogBypass(true);
	PG_TRY();
	{
		SPI_connect();
		sync_storage();
		SPI_finish();
		foreach(cell, relations)
		{
			Oid			id = lfirst_oid(cell);

			if (truncating)
			{
				/* Catalog updates must retain branch-aware uniqueness checks. */
				BranchCatalogBypass(false);
				truncate_relation(id);
				BranchCatalogBypass(true);
			}
			else
			{
				SPI_connect();
				retire_relation(id);
				SPI_finish();
			}
		}
		SPI_connect();
		sync_storage();
		SPI_finish();
		BranchCatalogBypass(false);
		invalidate();
	}
	PG_CATCH();
	{
		BranchCatalogBypass(false);
		PG_RE_THROW();
	} PG_END_TRY();
	list_free(relations);
	return true;
}
