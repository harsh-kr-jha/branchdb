/*
 * Native branch storage maintenance. All destructive work is performed under
 * the native admission barrier, with catalog visibility explicitly bypassed.
 */
#include "postgres.h"
#include "access/branchheap.h"
#include "access/detoast.h"
#include "access/heapam.h"
#include "access/xact.h"
#include "catalog/pg_type_d.h"
#include "catalog/namespace.h"
#include "catalog/pg_tablespace_d.h"
#include "common/relpath.h"
#include "commands/defrem.h"
#include "executor/spi.h"
#include "miscadmin.h"
#include "nodes/parsenodes.h"
#include "storage/fd.h"
#include "storage/smgr.h"
#include "utils/builtins.h"
#include "utils/fmgrprotos.h"
#include "utils/hsearch.h"
#include "utils/memutils.h"
#include "utils/snapmgr.h"
#include "utils/rel.h"

/* Explicit padding keeps HASH_BLOBS keys deterministic on all alignments. */
typedef struct ToastKey
{
	Oid8		value;
	Oid			relation;
	uint32		padding;
} ToastKey;
typedef struct RetainedTuple
{
	Oid			descriptor;
	int64		generation;
	ItemPointerData tid;
} RetainedTuple;

static void
admin_begin(void)
{
	BranchMaintenanceAdmission();
	BranchCatalogPrepare();
	BranchCatalogBypass(true);
}

static Datum
field(HeapTuple t, TupleDesc d, int n)
{
	bool		isnull;
	Datum		value = SPI_getbinval(t, d, n, &isnull);

	if (isnull)
		elog(ERROR, "incomplete native maintenance metadata");
	return value;
}

Datum
branch_gc_toast(PG_FUNCTION_ARGS)
{
	MemoryContext caller = CurrentMemoryContext;
	RetainedTuple *tuples;
	Oid		   *toasts;
	uint64		i,
				n,
				ntoast;
	int64		removed = 0;
	HASHCTL		ctl = {0};
	HTAB	   *refs,
			   *seen;

	/* The caller's command snapshot may predate a writer we waited for. */
	admin_begin();
	PushActiveSnapshot(GetLatestSnapshot());
	PG_TRY();
	{
		SPI_connect();
		SPI_execute("SELECT (SELECT o.object FROM branch_native.objects o JOIN pg_class c ON c.oid=o.object "
					"WHERE o.catalog='pg_class'::regclass AND o.owner=v.branch AND o.source=v.relid "
					"AND c.relam=(SELECT oid FROM pg_am WHERE amname='branch_heap') LIMIT 1),"
					"v.generation,v.block,v.offset_no FROM branch_native.versions v WHERE NOT v.deleted", true, 0);
		n = SPI_processed;
		tuples = MemoryContextAllocZero(caller, sizeof(RetainedTuple) * Max(n, 1));
		for (i = 0; i < n; i++)
		{
			HeapTuple	t = SPI_tuptable->vals[i];
			TupleDesc	d = SPI_tuptable->tupdesc;

			tuples[i].descriptor = DatumGetObjectId(field(t, d, 1));
			tuples[i].generation = DatumGetInt64(field(t, d, 2));
			ItemPointerSet(&tuples[i].tid, (BlockNumber) DatumGetInt64(field(t, d, 3)), (OffsetNumber) DatumGetInt64(field(t, d, 4)));
		}
		SPI_execute("SELECT DISTINCT reltoastrelid FROM pg_class WHERE reltoastrelid<>0 AND "
					"relam=(SELECT oid FROM pg_am WHERE amname='branch_heap')", true, 0);
		ntoast = SPI_processed;
		toasts = MemoryContextAlloc(caller, sizeof(Oid) * Max(ntoast, 1));
		for (i = 0; i < ntoast; i++)
			toasts[i] = DatumGetObjectId(field(SPI_tuptable->vals[i], SPI_tuptable->tupdesc, 1));
		SPI_finish();
		ctl.keysize = sizeof(ToastKey);
		ctl.entrysize = sizeof(ToastKey);
		ctl.hcxt = caller;
		refs = hash_create("retained branch toast values", 128, &ctl, HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
		/* Divergent schemas require the tuple's birth descriptor, not main's. */
		for (i = 0; i < n; i++)
		{
			Relation	r = table_open(tuples[i].descriptor, AccessShareLock);
			TupleTableSlot *slot = table_slot_create(r, NULL);
			int			a;

			if ((int64) r->rd_locator.relNumber != tuples[i].generation)
				elog(ERROR, "TOAST GC generation mismatch");
			if (!GetHeapamTableAmRoutine()->tuple_fetch_row_version(r, &tuples[i].tid, SnapshotAny, slot))
				elog(ERROR, "retained branch tuple missing during TOAST GC");
			slot_getallattrs(slot);
			for (a = 0; a < slot->tts_tupleDescriptor->natts; a++)
			{
				Form_pg_attribute attr = TupleDescAttr(slot->tts_tupleDescriptor, a);

				if (!attr->attisdropped && !slot->tts_isnull[a] && attr->attlen == -1 &&
					VARATT_IS_EXTERNAL_ONDISK(DatumGetPointer(slot->tts_values[a])))
				{
					toast_external_data external;
					ToastKey	key;

					toast_external_info_get((struct varlena *) DatumGetPointer(slot->tts_values[a]), &external);
					MemSet(&key, 0, sizeof(key));
					key.relation = BranchStorageRelation(external.toastrelid);
					key.value = external.valueid;
					hash_search(refs, &key, HASH_ENTER, NULL);
				}
			}
			ExecDropSingleTupleTableSlot(slot);
			table_close(r, NoLock);
		}
		ctl.keysize = sizeof(Oid);
		ctl.entrysize = sizeof(Oid);
		seen = hash_create("visited physical toast roots", 32, &ctl, HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
		for (i = 0; i < ntoast; i++)
		{
			Oid			root = BranchStorageRelation(toasts[i]);
			bool		found;
			Relation	r;
			TableScanDesc scan;
			TupleTableSlot *slot;
			Snapshot	snapshot;

			hash_search(seen, &root, HASH_ENTER, &found);
			if (found)
				continue;
			r = table_open(root, RowExclusiveLock);
			slot = table_slot_create(r, NULL);
			snapshot = RegisterSnapshot(GetLatestSnapshot());
			scan = GetHeapamTableAmRoutine()->scan_begin(r, snapshot, 0, NULL, NULL, SO_TYPE_SEQSCAN);
			while (GetHeapamTableAmRoutine()->scan_getnextslot(scan, ForwardScanDirection, slot))
			{
				bool		isnull;
				ToastKey	key;
				Datum		id = slot_getattr(slot, 1, &isnull);

				MemSet(&key, 0, sizeof(key));
				key.relation = root;
				key.value = TupleDescAttr(RelationGetDescr(r), 0)->atttypid == OID8OID ? DatumGetUInt64(id) : DatumGetObjectId(id);
				if (isnull)
					elog(ERROR, "null toast chunk identifier");
				if (!hash_search(refs, &key, HASH_FIND, NULL))
				{
					TM_FailureData failure;

					if (heap_delete(r, &slot->tts_tid, GetCurrentCommandId(true), TABLE_DELETE_NO_LOGICAL,
									InvalidSnapshot, true, &failure) != TM_Ok)
						elog(ERROR, "TOAST GC delete failed");
					removed++;
				}
				ExecClearTuple(slot);
			}
			GetHeapamTableAmRoutine()->scan_end(scan);
			UnregisterSnapshot(snapshot);
			ExecDropSingleTupleTableSlot(slot);
			table_close(r, NoLock);
		}
		hash_destroy(refs);
		hash_destroy(seen);
		pfree(tuples);
		pfree(toasts);
		PopActiveSnapshot();
		BranchCatalogBypass(false);
	}
	PG_CATCH();
	{
		BranchCatalogBypass(false);
		PG_RE_THROW();
	} PG_END_TRY();
	PG_RETURN_INT64(removed);
}

/*
 * Parse only PostgreSQL permanent relation file names, not temporary files,
 * maps, version files or arbitrary directory entries.
 */
static bool
file_number(const char *name, Oid *number)
{
	char	   *end;
	unsigned long value;

	if (name[0] < '0' || name[0] > '9')
		return false;
	value = strtoul(name, &end, 10);
	if (!value || value > PG_UINT32_MAX)
		return false;
	if (*end == '_')
	{
		if (strncmp(end, "_fsm", 4) == 0)
			end += 4;
		else if (strncmp(end, "_vm", 3) == 0)
			end += 3;
		else if (strncmp(end, "_init", 5) == 0)
			end += 5;
		else
			return false;
	}
	if (*end == '.')
	{
		end++;
		if (*end < '0' || *end > '9')
			return false;
		while (*end >= '0' && *end <= '9')
			end++;
	}
	if (*end)
		return false;
	*number = (Oid) value;
	return true;
}

Datum
branch_gc_files(PG_FUNCTION_ARGS)
{
	HASHCTL		ctl = {0};
	HTAB	   *live,
			   *orphans;
	HASH_SEQ_STATUS iter;
	Oid		   *ids;
	uint64		i,
				n;
	Oid		   *candidate;
	int64		removed = 0;
	MemoryContext caller = CurrentMemoryContext;
	DIR		   *volatile dir = NULL;
	char	   *path;
	struct dirent *de;
	volatile bool iter_active = false;

	if (IsTransactionBlock() || IsSubTransaction() || TransactionIdIsValid(GetTopTransactionIdIfAny()))
		ereport(ERROR, (errcode(ERRCODE_ACTIVE_SQL_TRANSACTION), errmsg("file GC requires a fresh standalone transaction")));
	admin_begin();
	PushActiveSnapshot(GetLatestSnapshot());
	PG_TRY();
	{
		SPI_connect();
		SPI_execute("SELECT oid FROM pg_class WHERE relkind IN ('r','t','i','S','m')", true, 0);
		n = SPI_processed;
		ids = MemoryContextAlloc(caller, sizeof(Oid) * Max(n, 1));
		for (i = 0; i < n; i++)
			ids[i] = DatumGetObjectId(field(SPI_tuptable->vals[i], SPI_tuptable->tupdesc, 1));
		SPI_finish();
		ctl.keysize = sizeof(Oid);
		ctl.entrysize = sizeof(Oid);
		ctl.hcxt = caller;
		live = hash_create("referenced local database files", 256, &ctl, HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
		orphans = hash_create("unreferenced local database files", 32, &ctl, HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
		for (i = 0; i < n; i++)
		{
			Relation	r = relation_open(ids[i], AccessShareLock);

			if (r->rd_locator.dbOid == MyDatabaseId && r->rd_locator.spcOid == MyDatabaseTableSpace)
			{
				Oid			number = r->rd_locator.relNumber;

				hash_search(live, &number, HASH_ENTER, NULL);
			}
			relation_close(r, NoLock);
		}
		path = GetDatabasePath(MyDatabaseId, MyDatabaseTableSpace);
		dir = AllocateDir(path);
		if (!dir)
			ereport(ERROR, (errcode_for_file_access(), errmsg("cannot read native database directory %s: %m", path)));
		while ((de = ReadDir(dir, path)) != NULL)
		{
			Oid			number;

			if (file_number(de->d_name, &number) && !hash_search(live, &number, HASH_FIND, NULL))
				hash_search(orphans, &number, HASH_ENTER, NULL);
		}
		FreeDir(dir);
		dir = NULL;
		hash_seq_init(&iter, orphans);
		iter_active = true;
		while ((candidate = hash_seq_search(&iter)) != NULL)
		{
			RelFileLocator loc;
			SMgrRelation storage;

			loc.spcOid = MyDatabaseTableSpace;
			loc.dbOid = MyDatabaseId;
			loc.relNumber = *candidate;
			storage = smgropen(loc, INVALID_PROC_NUMBER);
			smgrdounlinkall(&storage, 1, false);
			smgrclose(storage);
			removed++;
		}
		iter_active = false;
		hash_destroy(live);
		hash_destroy(orphans);
		pfree(path);
		pfree(ids);
		PopActiveSnapshot();
		BranchCatalogBypass(false);
	}
	PG_CATCH();
	{
		if (iter_active)
			hash_seq_term(&iter);
		if (dir)
			FreeDir(dir);
		BranchCatalogBypass(false);
		PG_RE_THROW();
	} PG_END_TRY();
	PG_RETURN_INT64(removed);
}

/*
 * Called by standalone SQL VACUUM and by an autovacuum worker before its
 * ordinary per-OID loop. Commit garbage marking before physical reclamation.
 */
static void
run_maintenance(void)
{
	LOCAL_FCINFO(fcinfo, 0);
	InitFunctionCallInfoData(*fcinfo, NULL, 0, InvalidOid, NULL, NULL);
	if (ActiveSnapshotSet())
		elog(ERROR, "unexpected active snapshot at native maintenance boundary");
	BranchMaintenanceAdmission();
	InvalidateCatalogSnapshot();
	PushActiveSnapshot(GetTransactionSnapshot());
	branch_gc(fcinfo);
	branch_gc_toast(fcinfo);
	PopActiveSnapshot();
	CommitTransactionCommand();
	StartTransactionCommand();
	BranchCatalogPrepare();
	BranchMaintenanceAdmission();
	InvalidateCatalogSnapshot();
	PushActiveSnapshot(GetTransactionSnapshot());
	branch_gc_files(fcinfo);
	branch_maintain(fcinfo);
	PopActiveSnapshot();
}

bool
BranchMaintenanceWorker(void)
{
	char	   *saved_name = BranchName;
	bool		handled = false;

	/*
	 * Worker maintenance always runs as the database administrator, never as
	 * a client-selected child branch inherited from server configuration.
	 */
	BranchName = "main";
	PG_TRY();
	{
		BranchCatalogPrepare();
		if (BranchCatalogEnabled)
		{
			run_maintenance();
			handled = true;
		}
		BranchName = saved_name;
	}
	PG_CATCH();
	{
		BranchName = saved_name;
		PG_RE_THROW();
	} PG_END_TRY();
	return handled;
}

bool
BranchMaintenanceUtility(Node *node, bool top)
{
	VacuumStmt *stmt;
	ListCell   *lc;

	if (!BranchCatalogEnabled || !IsA(node, VacuumStmt))
		return false;
	stmt = (VacuumStmt *) node;
	if (!stmt->is_vacuumcmd)
		ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED), errmsg("native ANALYZE is not implemented")));
	PreventInTransactionBlock(top, "VACUUM");
	if (strcmp(BranchName, "main") != 0)
		ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED), errmsg("native VACUUM must run on main")));
	foreach(lc, stmt->options)
	{
		DefElem    *o = lfirst_node(DefElem, lc);

		if (strcmp(o->defname, "freeze") == 0 || strcmp(o->defname, "verbose") == 0)
			continue;
		if ((strcmp(o->defname, "full") == 0 || strcmp(o->defname, "analyze") == 0 || strcmp(o->defname, "truncate") == 0) && !defGetBoolean(o))
			continue;
		ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED), errmsg("unsupported native VACUUM option: %s", o->defname)));
	}
	foreach(lc, stmt->rels)
	{
		VacuumRelation *v = lfirst_node(VacuumRelation, lc);

		if (v->va_cols)
			ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED), errmsg("native column-specific VACUUM is not supported")));
		(void) RangeVarGetRelid(v->relation, AccessShareLock, false);
	}
	/* Match core VACUUM: discard PortalRunUtility's command snapshot. */
	if (ActiveSnapshotSet())
		PopActiveSnapshot();
	run_maintenance();
	return true;
}
