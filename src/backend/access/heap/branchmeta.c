/*
 * Typed, indexed native metadata access. These relations always use ordinary
 * heap MVCC and indexes. This API is backend-private: it does not expose a SQL
 * privilege bypass. All lookups occur outside user B-tree content locks.
 */
#include "postgres.h"

#include "access/branchheap.h"
#include "access/branchmeta.h"
#include "access/genam.h"
#include "access/heapam.h"
#include "access/xact.h"
#include "catalog/indexing.h"
#include "catalog/namespace.h"
#include "commands/sequence.h"
#include "executor/executor.h"
#include "utils/builtins.h"
#include "utils/fmgroids.h"
#include "utils/lsyscache.h"
#include "utils/snapmgr.h"

/* Attribute positions are shared by metadata formats 1 through 3. */
enum
{
	V_ID = 1, V_ROOT, V_PREDECESSOR, V_BRANCH, V_RELATION, V_GENERATION,
V_BLOCK, V_OFFSET, V_DELETED, V_NATTS = V_DELETED};

static Oid
metadata(const char *name)
{
	Oid			id = get_relname_relid(name, get_namespace_oid("branch_native", false));

	if (!OidIsValid(id))
		elog(ERROR, "missing native metadata relation %s", name);
	return id;
}

static Datum
attribute(HeapTuple tuple, TupleDesc desc, int column)
{
	bool		isnull;
	Datum		result = heap_getattr(tuple, column, desc, &isnull);

	if (isnull)
		elog(ERROR, "null native metadata attribute %d", column);
	return result;
}

static BranchVersion
decode(HeapTuple tuple, TupleDesc desc)
{
	BranchVersion v;

	MemSet(&v, 0, sizeof(v));
	v.id = DatumGetInt64(attribute(tuple, desc, V_ID));
	v.root = DatumGetInt64(attribute(tuple, desc, V_ROOT));
	v.branch = DatumGetInt64(attribute(tuple, desc, V_BRANCH));
	v.deleted = DatumGetBool(attribute(tuple, desc, V_DELETED));
	if (!v.deleted)
		ItemPointerSet(&v.tid, (BlockNumber) DatumGetInt64(attribute(tuple, desc, V_BLOCK)),
					   (OffsetNumber) DatumGetInt64(attribute(tuple, desc, V_OFFSET)));
	return v;
}

static bool
visible(BranchVersion *v, BranchContext b)
{
	return v->branch == b.id || (v->branch == 0 && b.id != 0 && v->id <= b.cutoff);
}

BranchContext
BranchMetaContext(void)
{
	Snapshot	snapshot = RegisterSnapshot(GetLatestSnapshot());
	Relation	rel = table_open(metadata("branches"), AccessShareLock);
	ScanKeyData key;
	SysScanDesc scan;
	HeapTuple	tuple;
	BranchContext b;

	ScanKeyInit(&key, 2, BTEqualStrategyNumber, F_TEXTEQ, CStringGetTextDatum(BranchName));
	/* ScanKeyInit defaults to C ordering; the index follows this column. */
	key.sk_collation = TupleDescAttr(RelationGetDescr(rel), 1)->attcollation;
	scan = systable_beginscan(rel, metadata("branches_name_key"), true, snapshot, 1, &key);
	tuple = systable_getnext(scan);
	if (!HeapTupleIsValid(tuple))
		ereport(ERROR, (errcode(ERRCODE_UNDEFINED_OBJECT), errmsg("unknown native branch \"%s\"", BranchName)));
	b.id = DatumGetInt64(attribute(tuple, RelationGetDescr(rel), 1));
	b.cutoff = DatumGetInt64(attribute(tuple, RelationGetDescr(rel), 3));
	systable_endscan(scan);
	table_close(rel, AccessShareLock);
	UnregisterSnapshot(snapshot);
	return b;
}

int64
BranchMetaReserve(void)
{
	return nextval_internal(metadata("version_ids"), false);
}

static void
relation_keys(Relation rel, ScanKeyData *keys)
{
	ScanKeyInit(&keys[0], V_RELATION, BTEqualStrategyNumber, F_OIDEQ, ObjectIdGetDatum(BranchStorageRelation(RelationGetRelid(rel))));
	ScanKeyInit(&keys[1], V_GENERATION, BTEqualStrategyNumber, F_INT8EQ, Int64GetDatum((int64) rel->rd_locator.relNumber));
}

void
BranchMetaVisit(Relation rel, Snapshot snapshot, BranchContext b, int64 root,
				BranchVersionVisitor visitor, void *arg)
{
	Relation	meta;
	ScanKeyData keys[3];
	SysScanDesc scan;
	HeapTuple	tuple;

	snapshot = RegisterSnapshot(snapshot);
	meta = table_open(metadata("versions"), AccessShareLock);
	relation_keys(rel, keys);
	if (root)
		ScanKeyInit(&keys[2], V_ROOT, BTEqualStrategyNumber, F_INT8EQ, Int64GetDatum(root));
	scan = systable_beginscan(meta, metadata("versions_root"), true, snapshot, root ? 3 : 2, keys);
	while (HeapTupleIsValid(tuple = systable_getnext(scan)))
	{
		BranchVersion v = decode(tuple, RelationGetDescr(meta));

		if (visible(&v, b))
			visitor(&v, arg);
	}
	systable_endscan(scan);
	table_close(meta, AccessShareLock);
	UnregisterSnapshot(snapshot);
}

typedef struct LatestVersion
{
	BranchContext branch;
	BranchVersion version;
	bool		found;
} LatestVersion;

static void
latest(const BranchVersion *v, void *arg)
{
	LatestVersion *selected = arg;
	BranchVersion *old = &selected->version;

	if (!selected->found ||
		(v->branch == selected->branch.id && old->branch != selected->branch.id) ||
		(v->branch == old->branch && v->id > old->id))
	{
		selected->version = *v;
		selected->found = true;
	}
}

bool
BranchMetaVisibleTid(Relation rel, Snapshot snapshot, BranchContext b,
					 ItemPointer tid, BranchVersion *version)
{
	Relation	meta;
	ScanKeyData keys[4];
	SysScanDesc scan;
	HeapTuple	tuple;
	BranchVersion v;
	bool		found = false;
	LatestVersion selected = {0};

	snapshot = RegisterSnapshot(snapshot);
	meta = table_open(metadata("versions"), AccessShareLock);
	relation_keys(rel, keys);
	ScanKeyInit(&keys[2], V_BLOCK, BTEqualStrategyNumber, F_INT8EQ, Int64GetDatum(ItemPointerGetBlockNumber(tid)));
	ScanKeyInit(&keys[3], V_OFFSET, BTEqualStrategyNumber, F_INT8EQ, Int64GetDatum(ItemPointerGetOffsetNumber(tid)));
	scan = systable_beginscan(meta, metadata("versions_tid"), true, snapshot, 4, keys);
	tuple = systable_getnext(scan);
	if (HeapTupleIsValid(tuple))
	{
		v = decode(tuple, RelationGetDescr(meta));
		found = visible(&v, b);
	}
	systable_endscan(scan);
	table_close(meta, AccessShareLock);
	if (!found)
	{
		UnregisterSnapshot(snapshot);
		return false;
	}
	selected.branch = b;
	BranchMetaVisit(rel, snapshot, b, v.root, latest, &selected);
	UnregisterSnapshot(snapshot);
	if (!selected.found || selected.version.deleted || selected.version.id != v.id)
		return false;
	if (version)
		*version = v;
	return true;
}

void
BranchMetaInsert(Relation rel, BranchContext b, int64 id, int64 root,
				 int64 predecessor, ItemPointer tid)
{
	Relation	meta = table_open(metadata("versions"), RowExclusiveLock);
	Datum		values[V_NATTS];
	bool		nulls[V_NATTS] = {false};
	HeapTuple	tuple;
	EState	   *estate;
	ResultRelInfo info;
	TupleTableSlot *slot;

	values[V_ID - 1] = Int64GetDatum(id);
	values[V_ROOT - 1] = Int64GetDatum(root);
	values[V_PREDECESSOR - 1] = Int64GetDatum(predecessor);
	values[V_BRANCH - 1] = Int64GetDatum(b.id);
	values[V_RELATION - 1] = ObjectIdGetDatum(BranchStorageRelation(RelationGetRelid(rel)));
	values[V_GENERATION - 1] = Int64GetDatum((int64) rel->rd_locator.relNumber);
	values[V_BLOCK - 1] = Int64GetDatum(tid ? ItemPointerGetBlockNumber(tid) : 0);
	values[V_OFFSET - 1] = Int64GetDatum(tid ? ItemPointerGetOffsetNumber(tid) : 0);
	values[V_DELETED - 1] = BoolGetDatum(tid == NULL);
	tuple = heap_form_tuple(RelationGetDescr(meta), values, nulls);

	/*
	 * This is a user heap with a partial TID index, not a system catalog. Use
	 * normal executor index maintenance so its predicate is respected.
	 */
	estate = CreateExecutorState();
	InitResultRelInfo(&info, meta, 0, NULL, 0);
	ExecOpenIndices(&info, false);
	heap_insert(meta, tuple, GetCurrentCommandId(true), 0, NULL);
	slot = MakeSingleTupleTableSlot(RelationGetDescr(meta), &TTSOpsHeapTuple);
	ExecStoreHeapTuple(tuple, slot, false);
	slot->tts_tid = tuple->t_self;
	slot->tts_tableOid = RelationGetRelid(meta);
	ExecInsertIndexTuples(&info, estate, 0, slot, NIL, NULL);
	ExecDropSingleTupleTableSlot(slot);
	ExecCloseIndices(&info);
	FreeExecutorState(estate);
	heap_freetuple(tuple);
	table_close(meta, RowExclusiveLock);
	CommandCounterIncrement();
}

void
BranchMetaRemove(Relation rel, ItemPointer tid)
{
	Snapshot	snapshot = RegisterSnapshot(GetLatestSnapshot());
	Relation	meta = table_open(metadata("versions"), RowExclusiveLock);
	ScanKeyData keys[4];
	SysScanDesc scan;
	HeapTuple	tuple;

	relation_keys(rel, keys);
	ScanKeyInit(&keys[2], V_BLOCK, BTEqualStrategyNumber, F_INT8EQ, Int64GetDatum(ItemPointerGetBlockNumber(tid)));
	ScanKeyInit(&keys[3], V_OFFSET, BTEqualStrategyNumber, F_INT8EQ, Int64GetDatum(ItemPointerGetOffsetNumber(tid)));
	scan = systable_beginscan(meta, metadata("versions_tid"), true, snapshot, 4, keys);
	tuple = systable_getnext(scan);
	if (HeapTupleIsValid(tuple))
		CatalogTupleDelete(meta, &tuple->t_self);
	systable_endscan(scan);
	table_close(meta, RowExclusiveLock);
	UnregisterSnapshot(snapshot);
	CommandCounterIncrement();
}
