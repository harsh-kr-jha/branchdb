/* Milestone 0 representation test only; not a table AM or visibility patch. */
#include "postgres.h"
#include "access/htup_details.h"
#include "catalog/pg_type_d.h"
#include "fmgr.h"
#include "utils/builtins.h"
#include "utils/snapmgr.h"

PG_MODULE_MAGIC;
PG_FUNCTION_INFO_V1(branch_version_spike);

typedef enum Status { COMMITTED, ABORTED, RUNNING } Status;
typedef struct Version
{
	uint64 id;                 /* stable version ID, never a recyclable TID */
	uint64 predecessor;
	int branch;
	TransactionId xid;
	CommandId cmin;
	Status status;             /* synthetic commit-status input for this test */
	bool tombstone;
} Version;

/* One ancestry level. Caller supplies the appropriate branch/fork snapshot. */
static bool
visible(const Version *v, int branch, Snapshot local, Snapshot fork,
		TransactionId own)
{
	Snapshot s;

	if (v->branch != branch && v->branch != 0)
		return false;
	s = v->branch == branch ? local : fork;
	if (v->status == ABORTED)
		return false;
	if (v->branch == branch && v->xid == own)
		return v->cmin < local->curcid;
	return v->status == COMMITTED && !XidInMVCCSnapshot(v->xid, s);
}

/* A visible edge shadows its predecessor even when it is a tombstone. */
static uint64
resolve(const Version *versions, int count, int branch, Snapshot local,
		Snapshot fork, TransactionId own)
{
	uint64 selected = 1;
	bool deleted = false;
	int i;

	for (i = 0; i < count; i++)
	{
		const Version *v = &versions[i];
		if (v->predecessor == selected && visible(v, branch, local, fork, own))
		{
			selected = v->id;
			deleted = v->tombstone;
		}
	}
	return deleted ? 0 : selected;
}

static void
check(bool condition, const char *label)
{
	if (!condition)
		elog(ERROR, "branch representation test failed: %s", label);
}

Datum
branch_version_spike(PG_FUNCTION_ARGS)
{
	SnapshotData fork = {0}, local = {0};
	TransactionId fork_active[] = {150};
	TransactionId local_active[] = {350};
	TupleDesc desc;
	HeapTuple base;
	Datum values[] = {Int64GetDatum(1)};
	bool nulls[] = {false};
	char *header;
	Version siblings[] = {
		{2, 1, 1, 210, 0, COMMITTED, false},
		{3, 1, 2, 220, 0, COMMITTED, false}
	};
	Version v;

	fork.snapshot_type = local.snapshot_type = SNAPSHOT_MVCC;
	fork.xmin = local.xmin = 100;
	fork.xmax = 200;
	fork.xip = fork_active;
	fork.xcnt = 1;
	local.xmax = 500;
	local.xip = local_active;
	local.xcnt = 1;
	local.curcid = 3;

	desc = CreateTemplateTupleDesc(1);
	TupleDescInitEntry(desc, 1, "id", INT8OID, -1, 0);
	base = heap_form_tuple(desc, values, nulls);
	HeapTupleHeaderSetXmin(base->t_data, 110);
	HeapTupleHeaderSetXmax(base->t_data, InvalidTransactionId);
	ItemPointerSet(&base->t_data->t_ctid, 7, 1);
	header = palloc(base->t_len);
	memcpy(header, base->t_data, base->t_len);

	check(resolve(siblings, 2, 1, &local, &fork, 400) == 2, "A successor");
	check(resolve(siblings, 2, 2, &local, &fork, 400) == 3, "B successor");
	check(resolve(siblings, 2, 0, &local, &fork, 400) == 1, "main unaffected");
	check(memcmp(header, base->t_data, base->t_len) == 0, "shared heap header unchanged");

	v = (Version) {4, 1, 0, 120, 0, COMMITTED, false};
	check(visible(&v, 1, &local, &fork, 400), "ancestor before fork");
	v.xid = 210;
	check(!visible(&v, 1, &local, &fork, 400), "ancestor after fork");
	check(visible(&v, 0, &local, &fork, 400), "main sees its later change");
	v.xid = 150;
	check(!visible(&v, 1, &local, &fork, 400), "active at fork stays excluded after commit");
	v.branch = 1;
	v.xid = 350;
	check(!visible(&v, 1, &local, &fork, 400), "active in local snapshot");
	v.xid = 510;
	check(!visible(&v, 1, &local, &fork, 400), "future local xid");
	v.xid = 230;
	v.status = ABORTED;
	check(!visible(&v, 1, &local, &fork, 400), "aborted write");
	v.status = RUNNING;
	check(!visible(&v, 1, &local, &fork, 400), "uncommitted other write");
	v.xid = 400;
	v.cmin = 2;
	check(visible(&v, 1, &local, &fork, 400), "own earlier command");
	v.cmin = 3;
	check(!visible(&v, 1, &local, &fork, 400), "own current command");
	/* Aborted child xid must be rejected even when considered our own. */
	v.status = ABORTED;
	v.cmin = 2;
	check(!visible(&v, 1, &local, &fork, 400), "aborted subtransaction");
	v = (Version) {5, 2, 1, 240, 0, COMMITTED, true};
	{
		Version chain[] = {siblings[0], siblings[1], v};
		check(resolve(chain, 3, 1, &local, &fork, 400) == 0, "A tombstone");
		check(resolve(chain, 3, 2, &local, &fork, 400) == 3, "B survives A deletion");
	}
	check(memcmp(header, base->t_data, base->t_len) == 0, "predecessor remains byte-identical");
	pfree(header);
	heap_freetuple(base);
	FreeTupleDesc(desc);
	PG_RETURN_TEXT_P(cstring_to_text("PASS: 18 branch representation checks"));
}
