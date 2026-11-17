/*-------------------------------------------------------------------------
 * Experimental native MVCC branches: append-only physical versions and
 * WAL-logged branch/version metadata. Not intended for production use.
 * All SQL reads use statement-snapshot metadata; writes validate against a
 * fresh snapshot after branch-wide serialization. No predecessor is modified.
 *-------------------------------------------------------------------------
 */
#include "postgres.h"
#include "access/branchheap.h"
#include "access/heapam.h"
#include "access/parallel.h"
#include "access/tableam_indexscan.h"
#include "catalog/index.h"
#include "commands/vacuum.h"
#include "access/xact.h"
#include "access/xlogrecovery.h"
#include "catalog/namespace.h"
#include "catalog/pg_am_d.h"
#include "catalog/pg_class.h"
#include "executor/spi.h"
#include "executor/executor.h"
#include "fmgr.h"
#include "miscadmin.h"
#include "nodes/parsenodes.h"
#include "storage/lock.h"
#include "utils/builtins.h"
#include "utils/memutils.h"
#include "utils/lsyscache.h"
#include "utils/inval.h"
#include "utils/snapmgr.h"

char *BranchName = NULL;
static const TableAmRoutine *heap;
static TableAmRoutine methods;

typedef struct BranchContext
{
	int64 id;
	int64 cutoff;
} BranchContext;

typedef struct BranchVersion
{
	int64 id;
	int64 root;
	ItemPointerData tid;
	bool deleted;
} BranchVersion;

typedef struct BranchScan
{
	TableScanDescData base;
	TableScanDesc inner;
	int count;
	BranchVersion *versions;
} BranchScan;

/* Prepared before B-tree locks. Never access SPI/catalogs from fetch_tid. */
static MemoryContext dirty_context;
static BranchVersion *dirty_versions;
static int dirty_count;
static Oid dirty_relid;
static RelFileNumber dirty_generation;

static void
reset_dirty(void)
{
	dirty_context = NULL;
	dirty_versions = NULL;
	dirty_count = 0;
	dirty_relid = InvalidOid;
}

static void
dirty_xact(XactEvent event, void *arg)
{
	reset_dirty();
}

static void
dirty_subxact(SubXactEvent event, SubTransactionId child,
			  SubTransactionId parent, void *arg)
{
	/* The old memory is owned by the top transaction and freed there. */
	reset_dirty();
}

static void
unsupported(const char *operation)
{
	ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
						errmsg("branch_heap prototype does not support %s", operation)));
}

static void
admission(LOCKMODE mode)
{
	LOCKTAG tag;

	if (BranchLifecycleEnabled && MyProcPort != NULL && BranchLifecycleSessionId() < 0)
		ereport(FATAL, (errcode(ERRCODE_INVALID_AUTHORIZATION_SPECIFICATION), errmsg("reconnect after native lifecycle initialization")));
	if (!BranchSessionAuthorized())
		ereport(ERROR, (errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
						errmsg("native branches require a bound login or a superuser")));
	if (IsParallelWorker() || IsolationUsesXactSnapshot())
		unsupported("parallel execution or isolation above READ COMMITTED");
	SET_LOCKTAG_ADVISORY(tag, MyDatabaseId, 0x42524e43, 0, 1);
	tag.locktag_lockmethodid = NATIVE_LOCKMETHOD;
	if (mode == ExclusiveLock && !LockHeldByMe(&tag, ExclusiveLock, false) && LockHeldByMe(&tag, ShareLock, false))
		unsupported("fork after accessing branch tables in the same transaction");
	if (!LockHeldByMe(&tag, mode, true))
		{
		LockAcquire(&tag, mode, false, false);
		if (BranchLifecycleEnabled && mode == ShareLock)
		{
			AcceptInvalidationMessages();
			BranchCatalogPrepare();
		}
	}
}

/* Explicit snapshots prevent SPI from accidentally replacing an outer scan's
 * statement snapshot. The metadata tables themselves always use normal heap. */
static void
query(const char *sql, Snapshot snapshot, bool readonly)
{
	BranchInternalQuery(sql, snapshot, readonly);
}

static int64
number(HeapTuple tuple, TupleDesc desc, int column)
{
	bool isnull;
	Datum d = SPI_getbinval(tuple, desc, column, &isnull);

	if (isnull)
		elog(ERROR, "null branch metadata");
	return DatumGetInt64(d);
}

static BranchContext
context(void)
{
	BranchContext b;

	query(psprintf("SELECT id, cutoff FROM branch_native.branches WHERE name = %s",
				   quote_literal_cstr(BranchName)), GetLatestSnapshot(), true);
	if (SPI_processed != 1)
		ereport(ERROR, (errcode(ERRCODE_UNDEFINED_OBJECT),
						errmsg("unknown native branch \"%s\"", BranchName)));
	b.id = number(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1);
	b.cutoff = number(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 2);
	return b;
}

static void
check_relation(Relation rel)
{
	if (rel->rd_rel->relpersistence != RELPERSISTENCE_PERMANENT ||
		rel->trigdesc != NULL)
		unsupported("temporary/unlogged tables or triggers");
}

static char *
relation_filter(Relation rel)
{
	return psprintf("relid = %u AND generation = " UINT64_FORMAT,
					BranchStorageRelation(RelationGetRelid(rel)), (uint64) rel->rd_locator.relNumber);
}

static char *
visible_filter(BranchContext b)
{
	if (b.id == 0)
		return pstrdup("branch = 0");
	return psprintf("(branch = " INT64_FORMAT " OR (branch = 0 AND id <= " INT64_FORMAT "))",
					b.id, b.cutoff);
}

static void
writer_lock(BranchContext b)
{
	LOCKTAG tag;

	SET_LOCKTAG_ADVISORY(tag, MyDatabaseId, 0x42524e43, (uint32) b.id, 2);
	tag.locktag_lockmethodid = NATIVE_LOCKMETHOD;
	if (!LockHeldByMe(&tag, ExclusiveLock, false))
		LockAcquire(&tag, ExclusiveLock, false, false);
}

/* Metadata scan outside buffer content locks. Retained physical versions have
 * no global xmax: normal heap MVCC plus this mapping supplies visibility. */
static TableScanDesc
branch_begin(Relation rel, Snapshot snapshot, int nkeys, ScanKeyData *key,
			 ParallelTableScanDesc parallel, uint32 flags)
{
	BranchScan *scan;
	BranchContext b;
	MemoryContext caller = CurrentMemoryContext;
	uint64 i;

	admission(ShareLock);
	check_relation(rel);
	if (parallel || !IsMVCCSnapshot(snapshot) ||
		!(flags & SO_TYPE_SEQSCAN))
		unsupported("nonsequential or non-MVCC scans");
	scan = palloc0(sizeof(*scan));
	SPI_connect();
	b = context();
	query(psprintf("SELECT DISTINCT ON (root_id) id, root_id, block, offset_no, deleted "
				   "FROM branch_native.versions WHERE %s AND %s "
				   "ORDER BY root_id, (branch = " INT64_FORMAT ") DESC, id DESC",
				   relation_filter(rel), visible_filter(b), b.id), snapshot, true);
	scan->count = SPI_processed;
	scan->versions = MemoryContextAllocZero(caller, sizeof(BranchVersion) * Max(scan->count, 1));
	for (i = 0; i < SPI_processed; i++)
	{
		HeapTuple t = SPI_tuptable->vals[i];
		TupleDesc d = SPI_tuptable->tupdesc;
		bool isnull;
		BranchVersion *v = &scan->versions[i];

		v->id = number(t, d, 1);
		v->root = number(t, d, 2);
		v->deleted = DatumGetBool(SPI_getbinval(t, d, 5, &isnull));
		if (!v->deleted)
			ItemPointerSet(&v->tid, (BlockNumber) number(t, d, 3),
							(OffsetNumber) number(t, d, 4));
	}
	SPI_finish();
	scan->inner = heap->scan_begin(rel, snapshot, nkeys, key, NULL, flags);
	scan->base = *scan->inner;
	return &scan->base;
}

static bool
branch_next(TableScanDesc desc, ScanDirection direction, TupleTableSlot *slot)
{
	BranchScan *scan = (BranchScan *) desc;
	int i;

	scan->inner->rs_instrument = desc->rs_instrument;
	while (heap->scan_getnextslot(scan->inner, direction, slot))
	{
		for (i = 0; i < scan->count; i++)
			if (!scan->versions[i].deleted &&
				ItemPointerEquals(&scan->versions[i].tid, &slot->tts_tid))
				return true;
	}
	return false;
}

static void
branch_end(TableScanDesc desc)
{
	BranchScan *scan = (BranchScan *) desc;

	heap->scan_end(scan->inner);
	pfree(scan->versions);
	pfree(scan);
}

static void
branch_rescan(TableScanDesc desc, ScanKeyData *key, bool set,
			  bool strat, bool sync, bool pagemode)
{
	BranchScan *scan = (BranchScan *) desc;

	heap->scan_rescan(scan->inner, key, set, strat, sync, pagemode);
}

static bool
selected(BranchVersion *versions, int count, ItemPointer tid)
{
	int i;

	for (i = 0; i < count; i++)
		if (!versions[i].deleted && ItemPointerEquals(&versions[i].tid, tid))
			return true;
	return false;
}

static void
prepare_dirty(Relation rel)
{
	MemoryContext old;
	BranchScan *scan;

	if (dirty_context)
		MemoryContextDelete(dirty_context);
	reset_dirty();
	dirty_context = AllocSetContextCreate(TopTransactionContext,
										 "branch uniqueness", ALLOCSET_DEFAULT_SIZES);
	old = MemoryContextSwitchTo(dirty_context);
	scan = (BranchScan *) branch_begin(rel, GetLatestSnapshot(), 0, NULL, NULL, SO_TYPE_SEQSCAN);
	dirty_count = scan->count;
	dirty_versions = palloc(sizeof(BranchVersion) * Max(dirty_count, 1));
	memcpy(dirty_versions, scan->versions, sizeof(BranchVersion) * dirty_count);
	branch_end(&scan->base);
	dirty_relid = RelationGetRelid(rel);
	dirty_generation = rel->rd_locator.relNumber;
	MemoryContextSwitchTo(old);
}

static bool
branch_fetch_tid(Relation rel, ItemPointer tid, Snapshot snapshot, bool *all_dead)
{
	/* A sibling-hidden or superseded tuple is NOT globally dead. */
	if (all_dead)
		*all_dead = false;
	if ((snapshot->snapshot_type != SNAPSHOT_DIRTY && snapshot != SnapshotSelf) ||
		!dirty_context || dirty_relid != RelationGetRelid(rel) ||
		dirty_generation != rel->rd_locator.relNumber)
		unsupported("constraint lookup without a prepared branch snapshot");
	if (snapshot->snapshot_type == SNAPSHOT_DIRTY)
	{
		snapshot->xmin = snapshot->xmax = InvalidTransactionId;
		snapshot->speculativeToken = 0;
	}
	if (!selected(dirty_versions, dirty_count, tid))
		return false;
	return heap->fetch_tid(rel, tid, snapshot, NULL);
}

static int64
reserve_id(void)
{
	query("SELECT nextval('branch_native.version_ids')", GetLatestSnapshot(), false);
	return number(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1);
}

static void
record_version(Relation rel, BranchContext b, int64 id, int64 root,
			   int64 predecessor, ItemPointer tid)
{
	query(psprintf("INSERT INTO branch_native.versions "
				   "(id, root_id, predecessor, branch, relid, generation, block, offset_no, deleted) "
				   "VALUES (" INT64_FORMAT "," INT64_FORMAT "," INT64_FORMAT "," INT64_FORMAT
				   ",%u," UINT64_FORMAT ",%u,%u,%s)",
				   id, root, predecessor, b.id, BranchStorageRelation(RelationGetRelid(rel)),
				   (uint64) rel->rd_locator.relNumber,
				   tid ? ItemPointerGetBlockNumber(tid) : 0,
				   tid ? ItemPointerGetOffsetNumber(tid) : 0,
				   tid ? "false" : "true"), GetLatestSnapshot(), false);
}

static BranchVersion
validate_old(Relation rel, BranchContext b, ItemPointer tid)
{
	BranchVersion v;
	int64 current;

	query(psprintf("SELECT id, root_id FROM branch_native.versions WHERE %s "
				   "AND NOT deleted AND block = %u AND offset_no = %u",
				   relation_filter(rel), ItemPointerGetBlockNumber(tid),
				   ItemPointerGetOffsetNumber(tid)), GetLatestSnapshot(), true);
	if (SPI_processed != 1)
		ereport(ERROR,(errcode(ERRCODE_T_R_SERIALIZATION_FAILURE),
					errmsg("native branch version was retired; retry transaction")));
	v.id = number(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1);
	v.root = number(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 2);
	query(psprintf("SELECT id FROM branch_native.versions WHERE %s AND root_id = " INT64_FORMAT
				   " AND %s ORDER BY (branch = " INT64_FORMAT ") DESC, id DESC LIMIT 1",
				   relation_filter(rel), v.root, visible_filter(b), b.id), GetLatestSnapshot(), true);
	current = SPI_processed ? number(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1) : 0;
	if (current != v.id)
		ereport(ERROR, (errcode(ERRCODE_T_R_SERIALIZATION_FAILURE),
						errmsg("native branch row changed concurrently; retry transaction")));
	return v;
}

static void
branch_insert(Relation rel, TupleTableSlot *slot, CommandId cid, uint32 options,
			  BulkInsertState bistate)
{
	BranchContext b;
	int64 id;

	admission(ShareLock);
	check_relation(rel);
	SPI_connect();
	b = context();
	writer_lock(b);
	id = reserve_id();
	heap->tuple_insert(rel, slot, cid, options, bistate);
	record_version(rel, b, id, id, 0, &slot->tts_tid);
	SPI_finish();
	prepare_dirty(rel);
}

static TM_Result
branch_update(Relation rel, ItemPointer old, TupleTableSlot *slot, CommandId cid,
			  uint32 options, Snapshot snapshot, Snapshot crosscheck, bool wait,
			  TM_FailureData *failure, LockTupleMode *mode, TU_UpdateIndexes *indexes)
{
	BranchContext b;
	BranchVersion v;
	int64 id;

	admission(ShareLock);
	check_relation(rel);
	SPI_connect();
	b = context();
	writer_lock(b);
	v = validate_old(rel, b, old);
	id = reserve_id();
	heap->tuple_insert(rel, slot, cid, 0, NULL);
	record_version(rel, b, id, v.root, v.id, &slot->tts_tid);
	SPI_finish();
	prepare_dirty(rel);
	*mode = LockTupleExclusive;
	*indexes = TU_All;
	return TM_Ok;
}

static TM_Result
branch_delete(Relation rel, ItemPointer tid, CommandId cid, uint32 options,
			  Snapshot snapshot, Snapshot crosscheck, bool wait, TM_FailureData *failure)
{
	BranchContext b;
	BranchVersion v;
	int64 id;

	admission(ShareLock);
	check_relation(rel);
	SPI_connect();
	b = context();
	writer_lock(b);
	v = validate_old(rel, b, tid);
	id = reserve_id();
	record_version(rel, b, id, v.root, v.id, NULL);
	SPI_finish();
	return TM_Ok;
}

static void
branch_multi(Relation rel, TupleTableSlot **slots, int count, CommandId cid,
			 uint32 options, BulkInsertState bistate)
{
	/* COPY FROM uses this path. Restrict until statement CID semantics tested. */
	unsupported("COPY FROM / multi_insert");
}

static void
branch_speculative(Relation rel, TupleTableSlot *slot, CommandId cid,
				   uint32 options, BulkInsertState bistate, uint32 token)
{
	unsupported("ON CONFLICT / speculative insert");
}

static bool
branch_index_next(IndexScanDesc scan, ScanDirection direction, TupleTableSlot *slot)
{
	BranchScan *mapping = (BranchScan *) scan->xs_table_opaque;

	while (tableam_index_getnext_tid(scan, direction))
	{
		/* Never set kill_prior_tuple for branch-local invisibility. */
		if (selected(mapping->versions, mapping->count, &scan->xs_heaptid) &&
			heap->tuple_fetch_row_version(scan->heapRelation, &scan->xs_heaptid,
									   scan->xs_snapshot, slot))
			return true;
	}
	ExecClearTuple(slot);
	return false;
}

static void
branch_index(IndexScanDesc scan, uint32 flags)
{
	if (scan->xs_want_itup || scan->indexRelation->rd_rel->relam != BTREE_AM_OID)
		unsupported("index-only or non-B-tree scans");
	scan->xs_table_opaque = branch_begin(scan->heapRelation, scan->xs_snapshot,
										 0, NULL, NULL, SO_TYPE_SEQSCAN);
	scan->xs_getnext_slot = branch_index_next;
}

static void
branch_index_reset(IndexScanDesc scan)
{
	/* Rescans/mark restores share the original statement's mapping. */
}

static void
branch_index_end(IndexScanDesc scan)
{
	branch_end((TableScanDesc) scan->xs_table_opaque);
}

static TM_Result
branch_lock(Relation rel, ItemPointer tid, Snapshot snapshot, TupleTableSlot *slot,
			CommandId cid, LockTupleMode mode, LockWaitPolicy policy, uint8 flags,
			TM_FailureData *failure)
{
	unsupported("row locking / EvalPlanQual");
	return TM_Invisible;
}

static bool
branch_fetch(Relation rel, ItemPointer tid, Snapshot snapshot, TupleTableSlot *slot)
{
	Snapshot visible = IsMVCCSnapshot(snapshot) ? snapshot : GetActiveSnapshot();
	TableScanDesc desc = branch_begin(rel, visible, 0, NULL, NULL, SO_TYPE_SEQSCAN);
	BranchScan *scan = (BranchScan *) desc;
	bool found = false;
	int i;

	for (i = 0; i < scan->count; i++)
		if (!scan->versions[i].deleted && ItemPointerEquals(&scan->versions[i].tid, tid))
		{
			found = heap->tuple_fetch_row_version(rel, tid, snapshot, slot);
			break;
		}
	branch_end(desc);
	return found;
}

static void
branch_vacuum(Relation rel, const VacuumParams *params, BufferAccessStrategy strategy)
{
	unsupported("VACUUM (retention is milestone 4)");
}

static double
branch_index_build(Relation rel, Relation index, IndexInfo *info, bool sync,
				   bool anyvisible, bool progress, BlockNumber start, BlockNumber blocks,
				   IndexBuildCallback callback, void *state, TableScanDesc scan)
{
	TableScanDesc ownscan;
	TupleTableSlot *slot;
	EState *estate;
	Snapshot snapshot;
	double count = 0;

	/* Index every retained committed version so old statement snapshots remain
	 * usable. Only the current main view participates in build-time uniqueness;
	 * B-tree's secondary spool retains shadowed entries without unique checks. */
	if (scan || anyvisible || start != 0 || info->ii_Concurrent ||
		index->rd_rel->relam != BTREE_AM_OID || !index->rd_index->indimmediate ||
		info->ii_Expressions != NIL || info->ii_Predicate != NIL ||
		info->ii_ExclusionOps != NULL)
		unsupported("parallel/concurrent, expression, partial, deferred or non-B-tree index builds");
	admission(ShareLock);
	SPI_connect();
	query("SELECT 1 FROM branch_native.branches WHERE id <> 0 LIMIT 1", GetLatestSnapshot(), true);
	if (SPI_processed && !BranchCatalogInternal)
	{
		if (!BranchCatalogEnabled || BranchStorageRelation(RelationGetRelid(rel)) != RelationGetRelid(rel))
			unsupported("index creation on shared inherited storage after fork");
		query(psprintf("SELECT 1 FROM branch_native.objects WHERE source=%u AND object<>%u LIMIT 1",RelationGetRelid(rel),RelationGetRelid(rel)),GetLatestSnapshot(),true);
		if (SPI_processed) unsupported("index creation on shared inherited storage after fork");
	}
	SPI_finish();
	slot = table_slot_create(rel, NULL);
	estate = CreateExecutorState();
	snapshot = RegisterSnapshot(GetLatestSnapshot());
	ownscan = branch_begin(rel, snapshot, 0, NULL, NULL, SO_TYPE_SEQSCAN);
	while (heap->scan_getnextslot(((BranchScan *) ownscan)->inner, ForwardScanDirection, slot))
	{
		Datum values[INDEX_MAX_KEYS];
		bool isnull[INDEX_MAX_KEYS];

		FormIndexDatum(info, slot, estate, values, isnull);
		callback(index, &slot->tts_tid, values, isnull,
				 selected(((BranchScan *) ownscan)->versions,
						  ((BranchScan *) ownscan)->count, &slot->tts_tid), state);
		count++;
		ExecClearTuple(slot);
	}
	branch_end(ownscan);
	UnregisterSnapshot(snapshot);
	ExecDropSingleTupleTableSlot(slot);
	FreeExecutorState(estate);
	return count;
}

static Oid
branch_toast_am(Relation rel)
{
	return HEAP_TABLE_AM_OID;
}

Datum
branch_heap_handler(PG_FUNCTION_ARGS)
{
	if (heap == NULL)
	{
		RegisterXactCallback(dirty_xact, NULL);
		RegisterSubXactCallback(dirty_subxact, NULL);
		heap = GetHeapamTableAmRoutine();
		methods = *heap;
		methods.scan_begin = branch_begin;
		methods.scan_getnextslot = branch_next;
		methods.scan_end = branch_end;
		methods.scan_rescan = branch_rescan;
		methods.scan_set_tidrange = NULL;
		methods.scan_getnextslot_tidrange = NULL;
		methods.scan_bitmap_next_tuple = NULL;
		methods.index_scan_begin = branch_index;
		methods.index_scan_reset = branch_index_reset;
		methods.index_scan_end = branch_index_end;
		methods.fetch_tid = branch_fetch_tid;
		methods.tuple_fetch_row_version = branch_fetch;
		methods.tuple_insert = branch_insert;
		methods.tuple_insert_speculative = branch_speculative;
		methods.multi_insert = branch_multi;
		methods.tuple_update = branch_update;
		methods.tuple_delete = branch_delete;
		methods.tuple_lock = branch_lock;
		methods.relation_vacuum = branch_vacuum;
		methods.relation_toast_am = branch_toast_am;
		methods.index_build_range_scan = branch_index_build;
	}
	PG_RETURN_POINTER(&methods);
}

Datum
branch_create(PG_FUNCTION_ARGS)
{
	char *name = text_to_cstring(PG_GETARG_TEXT_PP(0));
	BranchContext b;

	if (strlen(name) == 0 || strlen(name) >= NAMEDATALEN || strcmp(name, "main") == 0)
		ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE), errmsg("invalid branch name")));
	if (strcmp(BranchName, "main") != 0)
		unsupported("forking a child branch");
	if (!superuser())
		ereport(ERROR, (errcode(ERRCODE_INSUFFICIENT_PRIVILEGE), errmsg("branch administration requires a superuser")));
	admission(ExclusiveLock);
	if (BranchCatalogEnabled)
	{
		AcceptInvalidationMessages();
		BranchCatalogPrepare();
	}
	SPI_connect();
	query("SELECT 1 FROM pg_prepared_xacts WHERE database = current_database()",
		  GetLatestSnapshot(), true);
	if (SPI_processed)
		unsupported("fork with prepared transactions");
	b = context();
	Assert(b.id == 0);
	query(psprintf("INSERT INTO branch_native.branches(name, cutoff) "
				   "SELECT %s, COALESCE(max(id),0) FROM branch_native.versions",
				   quote_literal_cstr(name)), GetLatestSnapshot(), false);
	SPI_finish();
	BranchCatalogFork(name);
	PG_RETURN_VOID();
}

/* Until catalog branching exists, fail closed for schema/maintenance utilities
 * after a fork. The experimental database must be dedicated to this test. */
void
BranchGuardUtility(Node *node)
{
	bool forked;
	bool ready;
	Oid ns;

	if (BranchCatalogInternal) return;
	if (!IsTransactionState() || IsAbortedTransactionBlockState()) return;
	/* Administrative bootstrap metadata always uses ordinary heap storage. */
	if (superuser() && IsA(node, CreateSeqStmt))
	{
		RangeVar *rv = ((CreateSeqStmt *) node)->sequence;
		if (rv->schemaname && strcmp(rv->schemaname, "branch_native") == 0) return;
	}
	if (superuser() && (IsA(node, IndexStmt) || IsA(node, AlterTableStmt)))
	{
		RangeVar *rv = IsA(node, IndexStmt) ? ((IndexStmt *) node)->relation : ((AlterTableStmt *) node)->relation;
		if (rv->schemaname && strcmp(rv->schemaname, "branch_native") == 0) return;
	}
	if (superuser() && IsA(node, CreateStmt))
	{
		CreateStmt *stmt = (CreateStmt *) node;
		if (stmt->relation->schemaname && strcmp(stmt->relation->schemaname, "branch_native") == 0)
		{
			stmt->accessMethod = pstrdup("heap");
			return;
		}
	}

	switch (nodeTag(node))
	{
		case T_GrantStmt:
		case T_GrantRoleStmt:
		case T_CreateRoleStmt:
		case T_AlterRoleStmt:
			if (superuser()) return;
			break;
		case T_TransactionStmt:
			if (((TransactionStmt *) node)->kind == TRANS_STMT_PREPARE &&
				IsTransactionState() && !IsAbortedTransactionBlockState() &&
				OidIsValid(get_namespace_oid("branch_native", true)))
				unsupported("prepared transactions");
			return;
		case T_VariableSetStmt:
		case T_VariableShowStmt:
		case T_ExplainStmt:
		case T_PrepareStmt:
		case T_ExecuteStmt:
		case T_DeallocateStmt:
		case T_DiscardStmt:
		case T_FetchStmt:
		case T_ClosePortalStmt:
		case T_DeclareCursorStmt:
		case T_CopyStmt:
		case T_CheckPointStmt:
			return;
		default:
			break;
	}
	if (!IsTransactionState() || IsAbortedTransactionBlockState())
		return;
	ns = get_namespace_oid("branch_native", true);
	if (!OidIsValid(ns) || !OidIsValid(get_relname_relid("branches", ns)))
		return;
	admission(ShareLock);
	SPI_connect();
	query("SELECT 1 FROM branch_native.branches LIMIT 1", GetLatestSnapshot(), true);
	ready = SPI_processed != 0;
	query("SELECT 1 FROM branch_native.branches WHERE id <> 0 LIMIT 1", GetLatestSnapshot(), true);
	forked = SPI_processed != 0;
	SPI_finish();
	if (ready && BranchCatalogEnabled && (IsA(node,CreateStmt) || IsA(node,AlterTableStmt) || IsA(node,IndexStmt)))
	{
		LOCKTAG tag;
		BranchContext b;

		SPI_connect(); b=context(); SPI_finish();
		SET_LOCKTAG_ADVISORY(tag,MyDatabaseId,0x42524e43,(uint32)b.id,3);
		tag.locktag_lockmethodid = NATIVE_LOCKMETHOD;
		if (!LockHeldByMe(&tag,ExclusiveLock,false)) LockAcquire(&tag,ExclusiveLock,false,false);
		AcceptInvalidationMessages();
		BranchCatalogPrepare();
		BranchCatalogValidateCreate(node);
		if(IsA(node,IndexStmt) && ((IndexStmt *)node)->concurrent) unsupported("concurrent index creation");
		return;
	}
	if (ready && IsA(node, IndexStmt) && ((IndexStmt *) node)->concurrent)
		unsupported("concurrent index creation");
	if (ready && IsA(node, AlterTableStmt))
	{
		ListCell *lc;

		foreach(lc, ((AlterTableStmt *) node)->cmds)
			if (((AlterTableCmd *) lfirst(lc))->subtype != AT_AddIndex)
				unsupported("ALTER TABLE other than initial index constraints");
	}
	if (forked || (ready && !IsA(node, CreateStmt) && !IsA(node, IndexStmt) &&
				   !IsA(node, AlterTableStmt)))
		unsupported("schema/maintenance utilities (catalog branching is milestone 3)");
}

/* Logical garbage marking precedes ordinary physical vacuum. A branch retains
 * its selected values/tombstones by durable version ID, independent of XIDs. */
Datum
branch_gc(PG_FUNCTION_ARGS)
{
	int64		count = 0;
	MemoryContext caller = CurrentMemoryContext;
	uint64		i,
				n;
	typedef struct DeadVersion
	{
		int64		id;
		Oid			relid;
		int64		generation;
		ItemPointerData tid;
		bool		deleted;
	} DeadVersion;
	DeadVersion *dead;

	BranchMaintenanceAdmission();
	BranchCatalogPrepare();
	BranchCatalogBypass(true);
	PG_TRY();
	{
		SPI_connect();
		query(psprintf("SELECT v.id,v.relid,v.generation,v.block,v.offset_no,v.deleted FROM branch_native.versions v "
					   "WHERE NOT EXISTS (SELECT FROM branch_native.branches b WHERE b.name IS NOT NULL %s AND v.id=(SELECT w.id "
					   "FROM branch_native.versions w WHERE w.relid=v.relid AND w.generation=v.generation "
					   "AND w.root_id=v.root_id AND (w.branch=b.id OR (w.branch=0 AND w.id<=b.cutoff)) "
					   "ORDER BY (w.branch=b.id) DESC,w.id DESC LIMIT 1)) ORDER BY v.id", BranchLifecycleEnabled ?
					   "AND EXISTS(SELECT FROM branch_native.objects o JOIN pg_class c ON c.oid=o.object "
					   "WHERE o.catalog='pg_class'::regclass AND o.owner=b.id AND o.source=v.relid AND c.relfilenode::bigint=v.generation "
					   "AND NOT EXISTS(SELECT FROM branch_native.retired r WHERE r.catalog=o.catalog AND r.object=o.object))" : ""), GetLatestSnapshot(), true);
		n = SPI_processed;
		dead = MemoryContextAllocZero(caller, sizeof(DeadVersion) * Max(n, 1));
		for (i = 0; i < n; i++)
		{
			HeapTuple	t = SPI_tuptable->vals[i];
			TupleDesc	d = SPI_tuptable->tupdesc;
			bool		isnull;

			dead[i].id = number(t, d, 1);
			dead[i].relid = DatumGetObjectId(SPI_getbinval(t, d, 2, &isnull));
			dead[i].generation = number(t, d, 3);
			dead[i].deleted = DatumGetBool(SPI_getbinval(t, d, 6, &isnull));
			ItemPointerSet(&dead[i].tid, (BlockNumber) number(t, d, 4), (OffsetNumber) number(t, d, 5));
		}
		SPI_finish();
		for (i = 0; i < n; i++)
		{
			if (!dead[i].deleted)
			{
				Relation	r = table_open(dead[i].relid, RowExclusiveLock);
				TM_FailureData failure;
				TM_Result	result;

				if ((int64) r->rd_locator.relNumber != dead[i].generation)
					elog(ERROR, "branch GC storage generation mismatch");
				result = heap_delete(r, &dead[i].tid, GetCurrentCommandId(true),
									 TABLE_DELETE_KEEP_TOAST | TABLE_DELETE_NO_LOGICAL, InvalidSnapshot, true, &failure);
				if (result != TM_Ok)
					elog(ERROR, "branch GC physical delete failed: %d", result);
				table_close(r, NoLock);
			}
			SPI_connect();
			query(psprintf("DELETE FROM branch_native.versions WHERE id=" INT64_FORMAT, dead[i].id), GetLatestSnapshot(), false);
			SPI_finish();
			count++;
		}
		BranchCatalogBypass(false);
	}
	PG_CATCH();
	{
		BranchCatalogBypass(false);
		PG_RE_THROW();
	}
	PG_END_TRY();
	pfree(dead);
	PG_RETURN_INT64(count);
}

Datum
branch_maintain(PG_FUNCTION_ARGS)
{
	Oid *relations;
	uint64 i,n;
	MemoryContext caller=CurrentMemoryContext;
	VacuumParams params={0};

	BranchMaintenanceAdmission();
	BranchCatalogPrepare();
	BranchCatalogBypass(true);
	PG_TRY();
	{
		SPI_connect();
		query("SELECT oid FROM pg_class WHERE relkind IN ('r','t') ORDER BY oid",GetLatestSnapshot(),true);
		n=SPI_processed;
		relations=MemoryContextAlloc(caller,sizeof(Oid)*Max(n,1));
		for(i=0;i<n;i++)
		{
			bool isnull;
			relations[i]=DatumGetObjectId(SPI_getbinval(SPI_tuptable->vals[i],SPI_tuptable->tupdesc,1,&isnull));
		}
		SPI_finish();
		params.options=VACOPT_VACUUM|VACOPT_PROCESS_MAIN;
		params.index_cleanup=VACOPTVALUE_ENABLED;
		params.truncate=VACOPTVALUE_DISABLED;
		params.nworkers=-1;
		params.log_vacuum_min_duration=-1;
		params.log_analyze_min_duration=-1;
		for(i=0;i<n;i++)
		{
			Relation r=table_open(relations[i],ShareUpdateExclusiveLock);
			heap_vacuum_rel(r,&params,NULL);
			table_close(r,NoLock);
		}
		pfree(relations);
		vac_update_datfrozenxid();
		BranchCatalogBypass(false);
	}
	PG_CATCH();
	{
		BranchCatalogBypass(false);
		PG_RE_THROW();
	}
	PG_END_TRY();
	PG_RETURN_VOID();
}

void
BranchMaintenanceAdmission(void)
{
	if (!superuser())
		ereport(ERROR, (errcode(ERRCODE_INSUFFICIENT_PRIVILEGE), errmsg("branch maintenance requires a superuser")));
	if (XactReadOnly || RecoveryInProgress())
		ereport(ERROR,(errcode(ERRCODE_READ_ONLY_SQL_TRANSACTION),errmsg("native maintenance requires a writable primary")));
	if (!BranchCatalogEnabled || strcmp(BranchName,"main")!=0) unsupported("maintenance outside catalog-enabled main");
	admission(ExclusiveLock);
}

/*
 * DDL may upgrade its own admission lock without waiting behind another
 * upgrader. A conflict aborts with 40001 rather than creating a lock cycle.
 */
void
BranchLifecycleAdmission(void)
{
	LOCKTAG		tag;

	if (XactReadOnly || RecoveryInProgress())
		ereport(ERROR, (errcode(ERRCODE_READ_ONLY_SQL_TRANSACTION), errmsg("native lifecycle requires a writable primary")));
	SET_LOCKTAG_ADVISORY(tag, MyDatabaseId, 0x42524e43, 0, 1);
	tag.locktag_lockmethodid = NATIVE_LOCKMETHOD;
	if (LockHeldByMe(&tag, ShareLock, false) && !LockHeldByMe(&tag, ExclusiveLock, false))
	{
		if (LockAcquire(&tag, ExclusiveLock, false, true) == LOCKACQUIRE_NOT_AVAIL)
			ereport(ERROR, (errcode(ERRCODE_T_R_SERIALIZATION_FAILURE), errmsg("retry transaction to obtain native lifecycle admission")));
	}
	else
		admission(ExclusiveLock);
}
