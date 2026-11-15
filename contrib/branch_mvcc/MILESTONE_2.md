# Milestone 2: native branch-aware indexes and conflict checks

## Implementation

The core branch_heap AM now supports ordinary serial B-tree index scans and
immediate primary/unique constraints, including composite keys and NULL variants.
A native shared index contains entries for retained physical versions across
main and its children. The AM filters candidates using a statement-snapshot
branch mapping before fetching the heap tuple. Changed keys and tombstones shadow
ancestor entries independently of the index predicate. Hidden entries are never
marked globally dead or killed just because one branch cannot see them.

Native B-tree uniqueness checks use the AM's fetch_tid path with SnapshotDirty
and SnapshotSelf. A branch-visible TID set is prepared before entering index
code. This callback performs no SPI, metadata query or lock acquisition while a
B-tree page lock is held. It returns false for sibling/hidden tuples while
keeping all_dead false. The underlying heap still evaluates physical tuple
transaction status for selected candidates.

The branch-wide writer lock remains the concurrency invariant: a waiting writer
prepares its view after the previous writer commits/aborts, so a key deleted by
that writer can be reused and an inserted key conflicts. Sibling/main writers
use different locks and may insert identical keys. Same-branch stale updates and
deletes raise 40001 rather than mutating a replacement row. Per-key locking and
full EvalPlanQual equivalence are deferred; correctness tests do not claim those
optimizations have been implemented.

UPDATE now reports TU_All so every replacement physical version gets all its
index entries even when its key did not change. No cross-version HOT chains are
created. This avoids both missing unchanged-key entries and shared predecessor
mutation.

## Index creation and planning

Supported indexes must be created before the first fork, including initial
primary/unique constraints in CREATE TABLE. The serial native build indexes all
retained committed physical versions. Current main versions enter the ordinary
unique-checking spool; superseded versions enter B-tree's non-unique secondary
spool. Retaining the old entries preserves older statement snapshots and does
not declare their heap tuples globally reclaimable. Native heap visibility
excludes aborted physical versions. Build snapshots are explicitly registered
for the full scan lifetime.

Concurrent/parallel builds, expression/partial indexes, non-B-tree methods and
deferred/exclusion constraints are rejected. Branch schema/index creation after
fork is still blocked until catalog branching. Planner paths disable index-only
and parallel branch access; bitmap and TID-range fast paths remain unavailable.
Ordinary indexed reads use the serial branch-filtered heap fetch path.

## Changed source paths

- src/backend/access/heap/branchheap.c: native index scan lifecycle; prepared
  Dirty/Self membership; cache cleanup on transaction/subtransaction events;
  serial index construction; update index-entry requirements; initial index
  utility allowances.
- src/backend/catalog/index.c: serial index builds for branch_heap.
- src/backend/optimizer/util/plancat.c: disable index-only and parallel index
  capabilities for branch_heap relations.
- src/backend/optimizer/path/allpaths.c: keep branch relation scans out of workers.
- contrib/branch_mvcc/native_m2/: reproducible build/check runner, integration
  tests and supported/rejected operation documentation.

No core regression expected-output changes are required in this milestone.
The milestone-1 metadata format and connection API are unchanged.

## Test coverage

The milestone-2 suite runs actual native tables and concurrent connections in a
fresh disposable cluster. It checks primary/composite/secondary index creation,
unique builds after earlier updates/deletes, duplicate-build failures, inherited
and local uniqueness, identical sibling/main keys, NULL variants, changed indexed
values, unchanged-key updates, tombstone/key reuse, secondary-unique failure
rollback, savepoints, multirow statement failure, backward/array/parameterized
index scans, forced index/seq agreement and serial planner fallback.

Controlled concurrent transactions cover committed and aborted insert conflicts,
independent sibling/main insertion while A is uncommitted, stale update after
delete/reinsert, stale delete after update, and insertion after a waiting delete
commits. Repeated hidden-key probes must not damage another branch's shared index
entries. Later forks, immediate-stop recovery, uniqueness and further indexed
updates after restart are also checked. Milestone-1 integration and core regression
suites are rerun to detect regressions.

## Remaining limits

This is still a READ COMMITTED, trusted-superuser, fixed-schema native prototype.
No ON CONFLICT/MERGE, foreign keys, row-lock API, stronger isolation, nested forks,
schema migration isolation, maintenance/garbage collection, replication semantics
or production performance guarantee is provided. Branch-wide serialization and
linear metadata membership checks are explicit performance limitations.

Milestone 3 targets independent catalog/schema visibility and parallel migrations.
Work must stop after milestone 2 until the user gives the next go-ahead.

## Reproduction and executed results

See native_m2/README.md and run.sh. Results are recorded after the final checks.

Final results on the assertion-enabled PostgreSQL 20devel Make build:

- Native build/install: passed, with no compiler warnings/errors in final log.
- Core `make check`: all 243 regression tests passed with no new expected-output
  changes for milestone 2.
- Milestone-1 native integration suite: passed, including physical sharing,
  concurrent writes, rollback, TOAST and crash recovery.
- Milestone-2 native index/uniqueness suite: all four test groups passed, including
  the final secondary-unique-error rollback and concurrent-delete key reuse cases.
- Shell/Python syntax checks and `git diff --check`: passed.
- Logs: /tmp/branch-mvcc-m0/m2-{build,install,check,native-test,m1-regression}.log.
- Integration clusters were stopped and removed. System PostgreSQL was unchanged.
  Meson source registration remains present from milestone 1, but this execution
  tested the Make build only.

Milestone 2 is complete within the explicit scope above. Milestone 3 is paused
until the user's go-ahead. These changes are local on the existing feature branch;
this milestone did not create another Git commit or push.
