# Milestone 1: native row branching

The modified PostgreSQL server now includes a core `branch_heap` table access
method and a startup-only `branch` parameter. SQL reads/writes operate directly
on native tables; there are no views or DML triggers. This is an experimental
row-branching prototype, not arbitrary database/schema branching.

## Reproduce

Use a dedicated work directory without spaces, with normal PostgreSQL build
prerequisites and Python 3 installed:

```sh
contrib/branch_mvcc/native_m1/run.sh /tmp/branch-mvcc-m1
```

This builds and installs the modified checkout with assertions and debug symbols,
runs core `make check`, then initializes/stops/removes a separate cluster for the
Python integration suite. Nothing is installed into system PostgreSQL. The
script reuses an existing configured build directory, so only reuse it for this
checkout and these configuration flags. Build/core/native logs are retained.

To rerun only integration tests against an already built native server:

```sh
python3 contrib/branch_mvcc/native_m1/test.py --bindir /tmp/branch-mvcc-m1/install/bin
```

## Manual use

Initialize a fresh cluster with the modified server, create a disposable empty
database and connect as its superuser. Run `setup.sql` once in that database.
It creates normal WAL-logged metadata tables/sequences in `branch_native`.
Prepare all test tables before the first fork:

```sql
CREATE TABLE users(id bigint, name text)
  USING branch_heap WITH (autovacuum_enabled=false);
INSERT INTO users VALUES (1, 'Harsh');
SELECT branch_create('a');
SELECT branch_create('b');
```

Set branch selection at connection startup, keeping host/port/database as usual:

```sh
PGOPTIONS='-c branch=a' /path/to/native/bin/psql -d branch_demo
PGOPTIONS='-c branch=b' /path/to/native/bin/psql -d branch_demo
```

Each connection can issue ordinary INSERT/UPDATE/DELETE/SELECT/RETURNING:

```sql
-- Connection A
BEGIN;
UPDATE users SET name='A' WHERE id=1;
-- Keep open while B and main update the same inherited row, then COMMIT.
```

Main is the default branch. `SET branch` is rejected. Unknown names are rejected
on the first branch-table operation, not at connection authentication. Branch
names are resolved afresh, so reconnects do not depend on backend memory state.
Later `branch_create` calls in main capture main's then-current contents. Nested
forks are rejected. A transaction that has already accessed branch tables must
commit/rollback before forking; it cannot upgrade the fork admission lock.

## Representation

Every INSERT creates a stable version ID and an ordinary heap tuple. An UPDATE
appends another physical tuple and a metadata edge to its predecessor/root;
DELETE appends a tombstone. Shared predecessors are never heap-updated/deleted.
The scan reads metadata at the statement's actual MVCC snapshot, selects the
newest local version per root (or inherited main version at the fork cutoff),
then filters an ordinary heap MVCC scan to the selected physical TIDs. Executor
old-row fetches apply the same branch filtering. Rescans retain the statement's
mapping. SQL predicates are evaluated on the resulting branch-visible rows.

Forks take a database-local admission lock exclusively; branch-table accesses
hold it shared until transaction end. This first implementation drains readers
as well as writers. After admission, a fresh snapshot establishes a committed
version-ID frontier. All live main metadata at or below it is inherited; later
main versions are excluded. This quiescent protocol removes the need to persist
an arbitrary historical in-progress-XID list in milestone 1. Ordinary MVCC on
metadata still determines transaction/command visibility and rolls back errors
and savepoints. Sequence holes from aborted transactions are harmless.

Metadata identifies versions by relation OID, physical storage generation and
monotonic version ID. TIDs are lookup locations, not public permanent IDs.
Committed physical versions and metadata are retained; destructive storage
changes are blocked. The temporary mapping is safe only under that retention
rule. Garbage collection and full maintenance support require milestone 4.

The initial branch-wide write lock serializes writers within one branch;
different branches lock independently. A writer rechecks the latest branch state
after waiting; stale writes raise 40001 and require whole-transaction retry.
This is deliberately conservative, not full heap EvalPlanQual equivalence.

## Supported and rejected scope

- Only permanent, explicitly created `USING branch_heap` tables participate.
  Ordinary heap tables are global/unbranched. Use a dedicated empty test DB.
- READ COMMITTED, sequential scans, regular DML, RETURNING, transactional rollback
  and savepoints are covered. This is a trusted-superuser prototype.
- No primary keys, unique constraints, indexes, ON CONFLICT, row locking,
  parallel execution, sampling, COPY FROM or stronger isolation levels.
  Unsupported scan/write paths fail rather than return unfiltered heap data.
- Schema/maintenance utilities are conservatively blocked in an initialized
  experimental database, except initial CREATE TABLE before forks. This includes
  VACUUM/ANALYZE, rewrites, ALTER, DROP, TRUNCATE and extension management.
  Autovacuum must be disabled for branch tables; the AM rejects its vacuum path.
- Prepared transactions are rejected. Forks also reject pre-existing prepared
  transactions in this database. Reserved advisory-lock keys use database ID and
  0x42524e43; do not deliberately reuse them in the experiment.
- Metadata access deliberately uses ordinary heap visibility and fixed qualified
  object names, avoiding recursive branch catalog resolution. Internal metadata
  can be changed by its superuser owner; it is not a tenant-security boundary.
- Native catalog isolation/migrations are milestone 3. No merge/drop-branch API,
  version reclamation, indefinite XID-wraparound proof, replication semantics or
  performance promise is included here. Scans currently load per-relation
  metadata and linearly match TIDs; this is intentionally unoptimized.

## Tests

The integration suite uses concurrent psql processes with pg_stat_activity wait
points, not timing-only guesses. It verifies shared TIDs and unchanged inherited
xmin/xmax, writable main after fork, sibling DML, own writes, statement/transaction
rollback, aborted savepoints, 64KB+ TOAST values, independent simultaneous writes,
same-branch stale conflicts, transactional forks, a later fork, a fork waiting
for a writer, unsupported-operation errors, and immediate-stop crash recovery.

Crash recovery is tested for committed data in this representation. There is no
claim yet of exhaustive crash injection or safe maintenance/garbage collection.
