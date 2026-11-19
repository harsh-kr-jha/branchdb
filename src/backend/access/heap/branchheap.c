/*-------------------------------------------------------------------------
 * Experimental native MVCC branches: append-only physical versions and
 * WAL-logged branch/version metadata. Not intended for production use.
 * All SQL reads use statement-snapshot metadata; writes validate against a
 * fresh snapshot after branch-wide serialization. No predecessor is modified.
 *-------------------------------------------------------------------------
 */
#include "postgres.h"
#include "access/branchheap.h"
#include "access/branchmeta.h"
#include "utils/hsearch.h"
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
#include "catalog/pg_namespace_d.h"
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

char	   *BranchName = NULL;
static const TableAmRoutine *heap;
static TableAmRoutine methods;

typedef struct BranchTidEntry
{
	ItemPointerData tid;
	BranchVersion version;
} BranchTidEntry;

typedef struct BranchRootEntry
{
	int64		root;
	BranchVersion version;
} BranchRootEntry;

typedef struct BranchScan
{
	TableScanDescData base;
	TableScanDesc inner;
	MemoryContext map_context;
	HTAB	   *tids;
} BranchScan;

typedef struct BranchIndex
{
	BranchContext branch;
} BranchIndex;

typedef struct DirtyMap
{
	Oid			relid;
	RelFileNumber generation;
	Oid			root;
	MemoryContext context;
	HTAB	   *tids;
} DirtyMap;

/* Prepared outside user B-tree locks. No metadata access in fetch_tid. */
static MemoryContext dirty_context;
static HTAB *dirty_maps;

static void
reset_dirty(void)
{
	dirty_context = NULL;
	dirty_maps = NULL;
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
	if (dirty_context)
		MemoryContextDelete(dirty_context);
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
	LOCKTAG		tag;

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

/*
 * Explicit snapshots prevent SPI from accidentally replacing an outer scan's
 * statement snapshot. The metadata tables themselves always use normal heap.
 */
static void
query(const char *sql, Snapshot snapshot, bool readonly)
{
	BranchInternalQuery(sql, snapshot, readonly);
}

static int64
number(HeapTuple tuple, TupleDesc desc, int column)
{
	bool		isnull;
	Datum		d = SPI_getbinval(tuple, desc, column, &isnull);

	if (isnull)
		elog(ERROR, "null branch metadata");
	return DatumGetInt64(d);
}

static BranchContext
context(void)
{
	return BranchMetaContext();
}

static void
check_relation(Relation rel)
{
	if (rel->rd_rel->relpersistence != RELPERSISTENCE_PERMANENT ||
		(rel->trigdesc != NULL && !BranchSchemaEnabled))
		unsupported("temporary/unlogged tables or triggers");
	if (rel->trigdesc)
	{
		int			i;

		for (i = 0; i < rel->trigdesc->numtriggers; i++)
			if (!rel->trigdesc->triggers[i].tgisinternal)
				unsupported("user-defined triggers");
	}
}

static void
writer_lock(BranchContext b)
{
	LOCKTAG		tag;

	SET_LOCKTAG_ADVISORY(tag, MyDatabaseId, 0x42524e43, (uint32) b.id, 2);
	tag.locktag_lockmethodid = NATIVE_LOCKMETHOD;
	if (!LockHeldByMe(&tag, ExclusiveLock, false))
		LockAcquire(&tag, ExclusiveLock, false, false);
}

static HTAB *
tid_map(MemoryContext context)
{
	HASHCTL		ctl = {0};

	ctl.keysize = sizeof(ItemPointerData);
	ctl.entrysize = sizeof(BranchTidEntry);
	ctl.hcxt = context;
	return hash_create("native selected TIDs", 1024, &ctl, HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
}

static bool
selected(HTAB *map, ItemPointer tid)
{
	return hash_search(map, tid, HASH_FIND, NULL) != NULL;
}

static void
add_tid(const BranchVersion *version, void *arg)
{
	HTAB	   *map = arg;
	BranchTidEntry *entry;

	if (version->deleted)
		return;
	entry = hash_search(map, &version->tid, HASH_ENTER, NULL);
	entry->version = *version;
}

typedef struct SelectRoots
{
	HTAB	   *roots;
	BranchContext branch;
} SelectRoots;

static void
select_root(const BranchVersion *version, void *arg)
{
	SelectRoots *selection = arg;
	bool		found;
	BranchRootEntry *entry = hash_search(selection->roots, &version->root, HASH_ENTER, &found);

	if (!found || (version->branch == selection->branch.id && entry->version.branch != selection->branch.id) ||
		(version->branch == entry->version.branch && version->id > entry->version.id))
		entry->version = *version;
}

static HTAB *
load_tids(Relation rel, Snapshot snapshot, MemoryContext context)
{
	HASHCTL		ctl = {0};
	SelectRoots selection;
	HASH_SEQ_STATUS sequence;
	BranchRootEntry *entry;
	HTAB	   *tids = tid_map(context);

	ctl.keysize = sizeof(int64);
	ctl.entrysize = sizeof(BranchRootEntry);
	ctl.hcxt = context;
	selection.roots = hash_create("native selected roots", 1024, &ctl, HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
	selection.branch = BranchMetaContext();
	BranchMetaVisit(rel, snapshot, selection.branch, 0, select_root, &selection);
	hash_seq_init(&sequence, selection.roots);
	while ((entry = hash_seq_search(&sequence)) != NULL)
		add_tid(&entry->version, tids);
	hash_destroy(selection.roots);
	return tids;
}

/*
 * Statement maps use the caller's snapshot and cannot change as a writer
 * commits. Indexed reads use targeted metadata lookups instead of this map.
 */
static TableScanDesc
branch_begin(Relation rel, Snapshot snapshot, int nkeys, ScanKeyData *key,
			 ParallelTableScanDesc parallel, uint32 flags)
{
	BranchScan *scan;

	admission(ShareLock);
	check_relation(rel);
	if (parallel || !IsMVCCSnapshot(snapshot) || !(flags & SO_TYPE_SEQSCAN))
		unsupported("nonsequential or non-MVCC scans");
	scan = palloc0(sizeof(*scan));
	scan->map_context = AllocSetContextCreate(CurrentMemoryContext, "native statement visibility", ALLOCSET_DEFAULT_SIZES);
	scan->tids = load_tids(rel, snapshot, scan->map_context);
	scan->inner = heap->scan_begin(rel, snapshot, nkeys, key, NULL, flags);
	scan->base = *scan->inner;
	return &scan->base;
}

static bool
branch_next(TableScanDesc desc, ScanDirection direction, TupleTableSlot *slot)
{
	BranchScan *scan = (BranchScan *) desc;

	scan->inner->rs_instrument = desc->rs_instrument;
	while (heap->scan_getnextslot(scan->inner, direction, slot))
		if (selected(scan->tids, &slot->tts_tid))
			return true;
	return false;
}

static void
branch_end(TableScanDesc desc)
{
	BranchScan *scan = (BranchScan *) desc;

	heap->scan_end(scan->inner);
	MemoryContextDelete(scan->map_context);
	pfree(scan);
}

static void
branch_rescan(TableScanDesc desc, ScanKeyData *key, bool set,
			  bool strat, bool sync, bool pagemode)
{
	BranchScan *scan = (BranchScan *) desc;

	heap->scan_rescan(scan->inner, key, set, strat, sync, pagemode);
}

static DirtyMap *
prepare_dirty(Relation rel)
{
	DirtyMap   *map;
	Oid			id = RelationGetRelid(rel);
	Oid			root = BranchStorageRelation(id);
	bool		found;
	BranchContext b;

	/*
	 * A cached map was built under the private writer lock, which remains
	 * held until transaction end. Only our own version changes can affect it.
	 */
	map = dirty_maps ? hash_search(dirty_maps, &id, HASH_FIND, NULL) : NULL;
	if (map && map->generation == rel->rd_locator.relNumber && map->root == root)
		return map;
	b = context();
	writer_lock(b);
	if (!dirty_maps)
	{
		HASHCTL		ctl = {0};

		dirty_context = AllocSetContextCreate(TopTransactionContext, "native writer visibility", ALLOCSET_DEFAULT_SIZES);
		ctl.keysize = sizeof(Oid);
		ctl.entrysize = sizeof(DirtyMap);
		ctl.hcxt = dirty_context;
		dirty_maps = hash_create("native writer relations", 16, &ctl, HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
	}
	map = hash_search(dirty_maps, &id, HASH_ENTER, &found);
	if (!found || map->generation != rel->rd_locator.relNumber || map->root != root)
	{
		if (found)
			MemoryContextDelete(map->context);
		map->context = AllocSetContextCreate(dirty_context, "native writer relation", ALLOCSET_DEFAULT_SIZES);
		map->generation = rel->rd_locator.relNumber;
		map->root = root;
		map->tids = load_tids(rel, GetLatestSnapshot(), map->context);
	}
	return map;
}

static void
forget_tid(Relation rel, ItemPointer tid)
{
	DirtyMap   *map = prepare_dirty(rel);

	hash_search(map->tids, tid, HASH_REMOVE, NULL);
}

static bool
branch_fetch_tid(Relation rel, ItemPointer tid, Snapshot snapshot, bool *all_dead)
{
	Oid			id = RelationGetRelid(rel);
	DirtyMap   *map = dirty_maps ? hash_search(dirty_maps, &id, HASH_FIND, NULL) : NULL;

	if (all_dead)
		*all_dead = false;
	if ((snapshot->snapshot_type != SNAPSHOT_DIRTY && snapshot != SnapshotSelf) || !map ||
		map->generation != rel->rd_locator.relNumber)
		unsupported("constraint lookup without a prepared branch snapshot");
	if (snapshot->snapshot_type == SNAPSHOT_DIRTY)
	{
		snapshot->xmin = snapshot->xmax = InvalidTransactionId;
		snapshot->speculativeToken = 0;
	}
	if (!selected(map->tids, tid))
		return false;
	return heap->fetch_tid(rel, tid, snapshot, NULL);
}

static int64
reserve_id(void)
{
	return BranchMetaReserve();
}

static void
record_version(Relation rel, BranchContext b, int64 id, int64 root,
			   int64 predecessor, ItemPointer tid)
{
	DirtyMap   *map;
	Oid			relid = RelationGetRelid(rel);

	BranchMetaInsert(rel, b, id, root, predecessor, tid);
	map = dirty_maps ? hash_search(dirty_maps, &relid, HASH_FIND, NULL) : NULL;
	if (map && tid)
	{
		BranchVersion v = {0};

		v.id = id;
		v.root = root;
		v.branch = b.id;
		v.tid = *tid;
		add_tid(&v, map->tids);
	}
}

static BranchVersion
validate_old(Relation rel, BranchContext b, ItemPointer tid)
{
	BranchVersion version;

	if (!BranchMetaVisibleTid(rel, GetLatestSnapshot(), b, tid, &version))
		ereport(ERROR, (errcode(ERRCODE_T_R_SERIALIZATION_FAILURE), errmsg("native branch row changed or was retired; retry transaction")));
	return version;
}

static void
branch_insert(Relation rel, TupleTableSlot *slot, CommandId cid, uint32 options,
			  BulkInsertState bistate)
{
	BranchContext b;
	int64		id;

	admission(ShareLock);
	check_relation(rel);

	b = context();
	writer_lock(b);
	prepare_dirty(rel);
	id = reserve_id();
	heap->tuple_insert(rel, slot, cid, options, bistate);
	record_version(rel, b, id, id, 0, &slot->tts_tid);

}

static TM_Result
branch_update(Relation rel, ItemPointer old, TupleTableSlot *slot, CommandId cid,
			  uint32 options, Snapshot snapshot, Snapshot crosscheck, bool wait,
			  TM_FailureData *failure, LockTupleMode *mode, TU_UpdateIndexes *indexes)
{
	BranchContext b;
	BranchVersion v;
	int64		id;

	admission(ShareLock);
	check_relation(rel);

	b = context();
	writer_lock(b);
	prepare_dirty(rel);
	v = validate_old(rel, b, old);
	forget_tid(rel, old);
	id = reserve_id();
	heap->tuple_insert(rel, slot, cid, 0, NULL);
	record_version(rel, b, id, v.root, v.id, &slot->tts_tid);

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
	int64		id;

	admission(ShareLock);
	check_relation(rel);

	b = context();
	writer_lock(b);
	prepare_dirty(rel);
	v = validate_old(rel, b, tid);
	forget_tid(rel, tid);
	id = reserve_id();
	record_version(rel, b, id, v.root, v.id, NULL);

	return TM_Ok;
}

static void
branch_multi(Relation rel, TupleTableSlot **slots, int count, CommandId cid,
			 uint32 options, BulkInsertState bistate)
{
	int			i;

	if (!BranchSchemaEnabled)
		unsupported("COPY FROM / multi_insert");
	for (i = 0; i < count; i++)
		branch_insert(rel, slots[i], cid, options, bistate);
}

static void
branch_speculative(Relation rel, TupleTableSlot *slot, CommandId cid,
				   uint32 options, BulkInsertState bistate, uint32 token)
{
	BranchContext b;
	int64		id;

	if (!BranchSchemaEnabled)
		unsupported("ON CONFLICT / speculative insert");
	admission(ShareLock);
	check_relation(rel);

	b = context();
	writer_lock(b);
	prepare_dirty(rel);
	id = reserve_id();
	heap->tuple_insert_speculative(rel, slot, cid, options, bistate, token);
	record_version(rel, b, id, id, 0, &slot->tts_tid);

}

static void
branch_complete_speculative(Relation rel, TupleTableSlot *slot, uint32 token, bool succeeded)
{
	heap->tuple_complete_speculative(rel, slot, token, succeeded);
	if (!succeeded)
	{

		BranchMetaRemove(rel, &slot->tts_tid);
		forget_tid(rel, &slot->tts_tid);

	}
}

void
BranchPrepareWrite(Relation rel)
{
	BranchContext b;

	admission(ShareLock);
	check_relation(rel);

	b = context();
	writer_lock(b);

	prepare_dirty(rel);
}

static bool
branch_index_next(IndexScanDesc scan, ScanDirection direction, TupleTableSlot *slot)
{
	BranchIndex *state = scan->xs_table_opaque;
	Snapshot	snapshot = scan->xs_snapshot;
	Snapshot	metadata_snapshot = snapshot->snapshot_type == SNAPSHOT_DIRTY ? GetLatestSnapshot() : snapshot;

	while (tableam_index_getnext_tid(scan, direction))
	{
		/*
		 * The user index has released its content lock before returning a
		 * TID. A pin may remain; typed metadata lookups take no user-index
		 * locks.
		 */
		if (BranchMetaVisibleTid(scan->heapRelation, metadata_snapshot, state->branch, &scan->xs_heaptid, NULL) &&
			heap->tuple_fetch_row_version(scan->heapRelation, &scan->xs_heaptid, snapshot, slot))
			return true;
	}
	ExecClearTuple(slot);
	return false;
}

static void
branch_index(IndexScanDesc scan, uint32 flags)
{
	BranchIndex *state;

	if (scan->xs_want_itup || scan->indexRelation->rd_rel->relam != BTREE_AM_OID)
		unsupported("index-only or non-B-tree scans");
	admission(ShareLock);
	check_relation(scan->heapRelation);
	state = palloc(sizeof(*state));
	state->branch = context();
	scan->xs_table_opaque = state;
	scan->xs_getnext_slot = branch_index_next;
}

static void
branch_index_reset(IndexScanDesc scan)
{
	/* Index rescans retain the original snapshot and branch context. */
}

static void
branch_index_end(IndexScanDesc scan)
{
	pfree(scan->xs_table_opaque);
}

static TM_Result
branch_lock(Relation rel, ItemPointer tid, Snapshot snapshot, TupleTableSlot *slot,
			CommandId cid, LockTupleMode mode, LockWaitPolicy policy, uint8 flags,
			TM_FailureData *failure)
{
	BranchContext b;

	if (!BranchSchemaEnabled)
		unsupported("row locking / EvalPlanQual");
	admission(ShareLock);
	check_relation(rel);

	b = context();
	if (policy != LockWaitBlock)
		unsupported("NOWAIT/SKIP LOCKED row locking");
	writer_lock(b);
	(void) validate_old(rel, b, tid);

	MemSet(failure, 0, sizeof(*failure));
	if (!heap->tuple_fetch_row_version(rel, tid, SnapshotAny, slot))
		ereport(ERROR, (errcode(ERRCODE_T_R_SERIALIZATION_FAILURE), errmsg("native row was retired; retry transaction")));
	return TM_Ok;
}

static bool
branch_satisfies_snapshot(Relation rel, TupleTableSlot *slot, Snapshot snapshot)
{
	if (BranchSchemaEnabled && snapshot == SnapshotSelf)
	{
		DirtyMap   *map;

		CommandCounterIncrement();
		map = prepare_dirty(rel);
		if (!selected(map->tids, &slot->tts_tid))
			return false;
	}
	return heap->tuple_satisfies_snapshot(rel, slot, snapshot);
}

static bool
branch_fetch(Relation rel, ItemPointer tid, Snapshot snapshot, TupleTableSlot *slot)
{
	Snapshot	visible;
	BranchContext b;

	if (BranchSchemaEnabled && snapshot == SnapshotAny)
		return heap->tuple_fetch_row_version(rel, tid, snapshot, slot);
	admission(ShareLock);
	check_relation(rel);
	b = context();
	visible = IsMVCCSnapshot(snapshot) ? snapshot : GetActiveSnapshot();
	if (!BranchMetaVisibleTid(rel, visible, b, tid, NULL))
		return false;
	return heap->tuple_fetch_row_version(rel, tid, snapshot, slot);
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
	EState	   *estate;
	Snapshot	snapshot;
	double		count = 0;
	HTAB	   *history = NULL;

	/*
	 * Index every retained committed version so old statement snapshots
	 * remain usable. Only the current main view participates in build-time
	 * uniqueness; B-tree's secondary spool retains shadowed entries without
	 * unique checks.
	 */
	if (scan || anyvisible || start != 0 || info->ii_Concurrent ||
		index->rd_rel->relam != BTREE_AM_OID || !index->rd_index->indimmediate ||
		info->ii_Expressions != NIL || info->ii_Predicate != NIL ||
		info->ii_ExclusionOps != NULL)
		unsupported("parallel/concurrent, expression, partial, deferred or non-B-tree index builds");
	admission(ShareLock);
	SPI_connect();
	query("SELECT 1 FROM branch_native.branches WHERE id <> 0 LIMIT 1", GetLatestSnapshot(), true);
	if (SPI_processed && !BranchCatalogInternal && !BranchSchemaEnabled)
	{
		if (!BranchCatalogEnabled || BranchStorageRelation(RelationGetRelid(rel)) != RelationGetRelid(rel))
			unsupported("index creation on shared inherited storage after fork");
		query(psprintf("SELECT 1 FROM branch_native.objects WHERE source=%u AND object<>%u LIMIT 1", RelationGetRelid(rel), RelationGetRelid(rel)), GetLatestSnapshot(), true);
		if (SPI_processed)
			unsupported("index creation on shared inherited storage after fork");
	}
	SPI_finish();
	slot = table_slot_create(rel, NULL);
	estate = CreateExecutorState();
	snapshot = RegisterSnapshot(GetLatestSnapshot());
	ownscan = branch_begin(rel, snapshot, 0, NULL, NULL, SO_TYPE_SEQSCAN);
	if (BranchSchemaEnabled)
	{
		history = tid_map(CurrentMemoryContext);
		BranchMetaVisit(rel, snapshot, context(), 0, add_tid, history);
	}

	while (heap->scan_getnextslot(((BranchScan *) ownscan)->inner, ForwardScanDirection, slot))
	{
		Datum		values[INDEX_MAX_KEYS];
		bool		isnull[INDEX_MAX_KEYS];

		/*
		 * Never deform a sibling's potentially divergent tuple layout. Retain
		 * all this branch's historical versions for older statement
		 * snapshots; only the selected current view participates in unique
		 * checks.
		 */
		if (BranchSchemaEnabled && !selected(history, &slot->tts_tid))
		{
			ExecClearTuple(slot);
			continue;
		}
		FormIndexDatum(info, slot, estate, values, isnull);
		callback(index, &slot->tts_tid, values, isnull,
				 selected(((BranchScan *) ownscan)->tids, &slot->tts_tid), state);
		count++;
		ExecClearTuple(slot);
	}
	if (history)
		hash_destroy(history);
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
		methods.tuple_complete_speculative = branch_complete_speculative;
		methods.tuple_satisfies_snapshot = branch_satisfies_snapshot;
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
	if (context().id != 0)
		elog(ERROR, "invalid native main branch identity");
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
	if (BranchSchemaEnabled && BranchSchemaGuard(node)) return;
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

	MemoryContext caller=CurrentMemoryContext;

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

/* A sequence-only transaction participates in fork admission too. Each branch
 * owns distinct sequence storage; no sibling writes a shared sequence page. */
void
BranchSchemaAdmission(void)
{
	BranchContext b;

	admission(ShareLock);

	b = context();
	writer_lock(b);

}

void
BranchSequenceAccess(Oid relation)
{
	if (!BranchSchemaEnabled || get_rel_namespace(relation) != PG_PUBLIC_NAMESPACE)
		return;
	BranchSchemaAdmission();
}
