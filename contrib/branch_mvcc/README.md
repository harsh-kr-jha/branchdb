# Persistent branch experiment

Latest engine checkpoint: [milestone 7](native_m7/README.md), with authenticated
branch connections, safe branch deletion, retained storage and DROP/TRUNCATE.
The runnable app fixture below remains useful for the earlier SQL subset.

Native milestone 5 provides a runnable web-app MVP on the modified
server: branch-local public tables, nullable column additions, primary/unique
keys, and isolated data with writable main and shared unchanged storage.
It includes a browser task app with independent main/A/B processes, transactional
SQL-file migrations, branch CLI/connection URLs, maintenance/recovery acceptance
and measured costs. Start with [native_m5/README.md](native_m5/README.md) for
builds, tests, demo and limits; [native_m3](native_m3/README.md) covers native setup.

The remainder of this page describes the earlier SQL-only reference prototype.

This is an executable **SQL overlay prototype**, plus an investigation of a
native implementation in [DESIGN.md](DESIGN.md). It does not patch heap MVCC or
implement arbitrary database branching. The checkout is PostgreSQL 20devel;
the integration suite runs against an explicitly selected installed server.

Two or more persistent, independently writable branches share one sealed base
heap. Branches store only inserted/changed rows and deletion markers. Standard
PostgreSQL MVCC handles transactions within each overlay; a branch never changes
the shared base's `xmax`. All storage is ordinary, WAL-logged PostgreSQL tables.
There is no permanently open transaction, exported snapshot, or vacuum pin.

## Run

Use a **disposable database**, as its owner. The setup creates schema
`branch_mvcc`; the demo creates `branch_a` and `branch_b`.

```sh
psql -X -v ON_ERROR_STOP=1 -d branch_demo -f contrib/branch_mvcc/branch_mvcc.sql
psql -X -v ON_ERROR_STOP=1 -d branch_demo -f contrib/branch_mvcc/demo.sql
```

Or run the integration suite (Python standard library only). It initializes its
own temporary cluster, disables TCP listening, runs the checks, simulates a
crash, restarts, and stops/removes the cluster:

```sh
python3 contrib/branch_mvcc/test.py --bindir /path/to/postgresql/bin
```

For a fresh database, load the base before creating branches:

```sql
INSERT INTO branch_mvcc.base_items VALUES (1, '{"message":"hello"}');
SELECT branch_mvcc.create_branch('branch_a');
SELECT branch_mvcc.create_branch('branch_b');
```

The first fork takes an exclusive base-table lock and installs an unconditional
write/truncate rejection trigger. It waits for existing base writers/readers;
subsequent forks inherit the **same** immutable baseline, not another branch's
current contents. `fork_snapshot` records the creation snapshot for diagnostics;
it is not consulted for visibility or a durable snapshot-retention mechanism.
The physical sealing is what preserves the baseline, including after freezing.
Creation requires READ COMMITTED and is transactional, including its DDL.

Connections select their branch once:

```sql
SET search_path = branch_a, public;
SELECT * FROM items;
INSERT INTO items VALUES (2, '{"message":"local"}') RETURNING *;
UPDATE items SET payload = '{"message":"changed"}' WHERE id = 1;
DELETE FROM items WHERE id = 2;
```

A connection can set this through the usual startup `options` parameter or a
role/database default. For example `PGOPTIONS='-c search_path=branch_a,public'`.
Connection pools must set/reset it when lending connections. This is name
resolution, not authentication or an isolation/security boundary. No elevated
`SECURITY DEFINER` functions are used; this experiment runs as a trusted owner.

## Storage and concurrency

`base_items(id bigint PRIMARY KEY, payload jsonb NOT NULL)` is shared once.
`delta(branch,id,payload,deleted)` has a composite primary key. Each branch view
unions its live delta with base rows for which it has no delta. Tombstones must
remain even after VACUUM: removing one would resurrect a base row. PostgreSQL
can reclaim superseded physical versions of delta rows normally.

The view's INSTEAD OF trigger locks/updates the branch's metadata row before
checking uniqueness and changing its delta. All writes in one branch serialize
until transaction end; different branches lock distinct metadata rows. A stale
UPDATE/DELETE raises `40001` when its OLD value differs from the current value;
a stale repeatable-read writer fails on the metadata update. Clients must retry
the entire transaction on serialization failure. Concurrent INSERTs recheck the
merged base/delta key after acquiring the lock. Equal-value intervening writes
are treated as equivalent; this is not full PostgreSQL heap row-lock/EvalPlanQual
behavior. Multi-branch transactions can deadlock and need ordinary retries.

## Deliberate limits

- One fixed table shape, stable bigint primary keys, shared schema. No generic
  table attachment, new fork points, nested forks, merges, or writable parent.
- Unqualified SELECT/INSERT/UPDATE/DELETE and RETURNING work. COPY FROM views,
  ON CONFLICT on views, TRUNCATE, row locks, foreign keys, arbitrary unique
  constraints, branch-local DDL, sequences and catalog isolation are not provided.
- No complete PostgreSQL isolation-level/concurrency equivalence is claimed.
  The tests cover selected conflicting writes and repeatable-read behavior.
- Superusers/owners can bypass triggers or directly change internal tables;
  neither the views nor schema selection enforce tenant security.
- There is no branch-drop API, storage quota policy, physical sharing benchmark,
  custom index AM, extension packaging, or integration into the core build.

Tests check zero delta rows immediately after forks, 1,000 shared base rows,
branch DML and tombstones, name-based session routing, rollback, constraint
errors, transactional fork creation, base sealing from an old transaction,
concurrent same-branch updates/inserts, independent branch progress,
repeatable-read snapshots, VACUUM FREEZE, and committed data after an immediate
server stop/restart. They do not establish general crash safety of future core
changes. See DESIGN.md for that boundary and the next implementation steps.

Native work now follows [NATIVE_PLAN.md](NATIVE_PLAN.md). The completed debug
build and representation-design gate is documented in
[MILESTONE_0.md](MILESTONE_0.md); its reproducible backend C test lives in
[native_spike/](native_spike/README.md).
