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
#include "access/xact.h"
#include "catalog/namespace.h"
#include "catalog/pg_am_d.h"
#include "catalog/pg_class.h"
#include "executor/spi.h"
#include "fmgr.h"
#include "miscadmin.h"
#include "nodes/parsenodes.h"
#include "storage/lock.h"
#include "utils/builtins.h"
#include "utils/memutils.h"
#include "utils/lsyscache.h"
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

static void
unsupported(const char *operation)
{
	ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
					errmsg("branch_heap milestone 1 does not support %s", operation)));
}

static void
admission(LOCKMODE mode)
{
	LOCKTAG tag;

	if (!superuser())
		ereport(ERROR, (errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
						errmsg("experimental native branches require a superuser")));
	if (IsParallelWorker() || IsolationUsesXactSnapshot())
		unsupported("parallel execution or isolation above READ COMMITTED");
	SET_LOCKTAG_ADVISORY(tag, MyDatabaseId, 0x42524e43, 0, 1);
	if (mode == ExclusiveLock && LockHeldByMe(&tag, ShareLock, false))
		unsupported("fork after accessing branch tables in the same transaction");
	if (!LockHeldByMe(&tag, mode, false))
		LockAcquire(&tag, mode, false, false);
}

/* Explicit snapshots prevent SPI from accidentally replacing an outer scan's
 * statement snapshot. The metadata tables themselves always use normal heap. */
static void
query(const char *sql, Snapshot snapshot, bool readonly)
{
	SPIPlanPtr plan = SPI_prepare(sql, 0, NULL);
	int result;

	if (plan == NULL)
		elog(ERROR, "could not prepare branch metadata query");
	result = SPI_execute_snapshot(plan, NULL, NULL, snapshot,
								  InvalidSnapshot, readonly, true, 0);
	if (result < 0)
		elog(ERROR, "branch metadata SPI error %d", result);
	SPI_freeplan(plan);
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
		rel->rd_rel->relhasindex || rel->trigdesc != NULL)
		unsupported("temporary/unlogged tables, indexes or triggers");
}

static char *
relation_filter(Relation rel)
{
	return psprintf("relid = %u AND generation = " UINT64_FORMAT,
					RelationGetRelid(rel), (uint64) rel->rd_locator.relNumber);
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
				   id, root, predecessor, b.id, RelationGetRelid(rel),
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
		elog(ERROR, "native branch version mapping missing");
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
	*mode = LockTupleExclusive;
	*indexes = TU_None;
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

static void
branch_index(IndexScanDesc scan, uint32 flags)
{
	unsupported("index scans");
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
	unsupported("indexes / primary keys (milestone 2)");
	return 0;
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
	admission(ExclusiveLock);
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

	switch (nodeTag(node))
	{
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
	if (forked || (ready && !IsA(node, CreateStmt)))
		unsupported("schema/maintenance utilities (catalog branching is milestone 3)");
}
