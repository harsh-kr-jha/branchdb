# Native MVCC branching implementation plan

Developer-beta continuation: [approved checkpoints](DEVELOPER_BETA_PLAN.md).
[Milestone 6](MILESTONE_6.md) adds authenticated branch-bound roles;
[milestone 7](MILESTONE_7.md) adds safe deletion and storage lifecycle.
Milestones 8–10 remain planned.

Progress: [milestone 0](MILESTONE_0.md), [milestone 1](MILESTONE_1.md),
[milestone 2](MILESTONE_2.md), [milestone 3](MILESTONE_3.md),
[milestone 4](MILESTONE_4.md) and [milestone 5](MILESTONE_5.md) are complete within
their documented prototype limits. Milestone 4 includes maintenance, actual CLOG segment retirement and
controlled epoch simulation; exhaustive wraparound/fault testing remains open.
Current native build/test/demo instructions: [native_m5](native_m5/README.md).
Connection and schema setup: [native_m3](native_m3/README.md).

## Product contract

One PostgreSQL instance, one logical database, persistent branches of both schema
and data. Developers/agents connect to a selected branch and use ordinary SQL and
migration tools. A fork inherits the parent's committed state at one fixed
boundary. Parent and children remain writable. Unchanged user rows are shared;
no whole-table or whole-database copy is performed at fork. Branch deletion and
merging are separate from ordinary SQL DROP TABLE.

This replaces the earlier recommendation to build the product on sealed SQL
views. Those files remain a behavioral reference only. The implementation target
is this PostgreSQL 20devel source tree, with core changes allowed.

## Initial boundaries

Two sibling branches plus writable main; one fork generation. A short write/DDL
barrier at fork is acceptable initially: drain writers and reject prepared
transactions, then establish and publish a committed boundary atomically. Reads
may continue where safe. This is a documented quiescent fork, not an arbitrary
historical snapshot or a nonblocking fork.

Start with ordinary permanent heap tables, primary keys and READ COMMITTED.
A connection selects its branch at startup and cannot switch branches during its
lifetime in the first prototype. The exact connection parameter/API is decided
in milestone 0; SQL examples below describe behavior rather than existing syntax.
Database-local application objects are branchable. Roles, tablespaces and branch
administration remain global. Permissions do not automatically follow from
visibility isolation and need explicit checks.

First migration target: transactional CREATE TABLE, nullable ADD COLUMN without
a default, primary-key index creation, and an ordinary migration-history table.
Then add nonunique indexes, constant defaults, sequences/identity, supported
constraints, DROP and TRUNCATE. Explicitly reject unsupported operations on
branchable objects; never silently execute a globally destructive equivalent.
Rewriting ALTER TABLE, arbitrary extensions, foreign tables, partitioning,
logical replication, 2PC, nested forks and merge are outside the initial MVP.
Choose a real app/ORM migration fixture before broadening this list; the first
vertical slice does not claim arbitrary ORM compatibility.

## Proposed engine model: decisions to validate

- A durable branch catalog identifies ancestry and fork boundaries. Transaction
  branch membership must survive abort, subtransactions and recovery, with a
  design for surviving ordinary transaction-status truncation.
- Branch-aware snapshots evaluate local changes at the ordinary transaction
  snapshot and ancestors at their respective fork boundaries. Own writes and
  command IDs retain PostgreSQL semantics.
- Shared inherited tuples cannot have their xmax/t_ctid overwritten by sibling
  writers. Prototype durable branch-local version edges/tombstones, identifying
  a predecessor by a stable version identity. Define how reads select the
  applicable successor and how branch-local locks protect writes. Choose a side
  structure versus a tuple-layout change after the first spike, not by assumption.
- A physical TID alone is not a permanent identity once slots can be reused.
  Either guarantee retained referenced versions cannot be recycled, or use a
  durable logical/version identifier. Table rewrites and DROP require a separate
  storage-generation identity and lifetime rules.
- Catalog rows need the same branch semantics, but visibility changes are not
  sufficient: catalog uniqueness, syscache, relcache, invalidations, dependency
  walks and physical storage deletion must all agree. Bootstrap/shared catalogs
  and the branch catalog cannot blindly use an application fork snapshot.
- Initially allocate OIDs globally. Catalog versions of inherited objects can
  share logical identity while differing by branch; caches must resolve that
  identity in branch context. New sibling objects receive distinct OIDs even
  when their SQL names are identical.

## Milestones and exit gates

### 0. Build baseline and settle the representation

Build/assert-enabled PostgreSQL from this checkout into an isolated prefix and
run baseline regression tests. Create a native test cluster; never reuse a
production data directory. Write the branch visibility truth table: local,
ancestor-before-fork, ancestor-after-fork, sibling, aborted, in-progress, own
command, subtransaction. Define lock ordering and the fork publication protocol.

Trace heap read/update/delete, dirty-snapshot uniqueness, catalog mutation/cache,
relation file unlink, and vacuum/pruning paths. Make a small executable
representation spike proving two successors of one predecessor without changing
its global xmax. Resolve stable IDs, metadata lookup without recursive catalog
visibility, and WAL atomicity. Record the chosen representation and rejected
alternatives before integrating it into all paths.

Exit: reproducible native build, baseline results, version/visibility design and
explicit invariants. A visibility-only patch does not pass this gate.

### 1. Persistent branch context and native row branching

Add branch catalog/lifecycle creation and startup selection. Enforce immutable
connection branch and propagate branch context into parallel workers or reject
parallel execution initially. Implement the fork write barrier and atomic
publication. Add native sequential reads, INSERT, UPDATE, DELETE, own-write
visibility, statement rollback and transaction rollback on fixed-schema tables.
Use durable branch-local successor/tombstone records and branch-scoped conflict
locking. Parent writes must preserve the versions children inherited.

Disable/reject unimplemented index/HOT/bitmap/TID/index-only paths for these
experimental relations. Put any temporary vacuum/freeze restrictions behind an
explicit experimental mode: this milestone is not a retention-safe MVP.

Exit: two real concurrent connections update/delete the same inherited row
independently; sibling inserts remain invisible; parent changes after fork stay
in main; uncommitted data never leaks; reconnect and crash replay retain answers.
No SQL views or INSTEAD OF triggers implement these semantics.

### 2. Indexes and same-branch concurrency

Implement branch-visible primary-key checks, local/ancestor index fetches and
shadow suppression. Update Self/Dirty/Update visibility and speculative insertion
where enabled. Handle changed indexed values: the old key must stop returning a
row even if its replacement is filtered out of the new query. Keep unsupported
scan paths disabled until tested. Correct same-branch EvalPlanQual/retry behavior;
ordinary cross-branch writes must not hold conflicting logical row locks.

Exit: same key inserts succeed across siblings, conflict within one branch;
concurrent update/delete/reinsert has no lost writes; forced supported index and
sequential scans agree. Test parent writes against children too.

### 3. Branch-local catalogs and first parallel migration

Extend relevant database-local catalogs and their unique checks. Make cache
population and invalidation branch-correct, including negative entries and
catalog snapshots. Retain conservative cross-branch DDL locks initially if needed
for correctness; measure and document blocking. Shared physical relation schema
must remain interpretable under divergent tuple descriptors.

Implement CREATE TABLE and ADD COLUMN without default first. New tables and
indexes get distinct storage. Protect referenced inherited files from unlink or
rewrite; reject DROP/TRUNCATE/rewrites until their lifecycle is implemented.
Include pg_class, pg_attribute, pg_type, pg_index, pg_constraint and pg_depend
paths as needed; do not assume a handpicked list covers every DDL dependency.

Exit: A adds avatar_url to users, B adds subscription_plan; each writes and reads
its own columns, main retains its original definition. Both can CREATE TABLE
with the same SQL name and different definitions. pg_catalog and
information_schema agree with each connection's SQL behavior. Migration-history
records, transactional DDL rollback, reconnect and prepared-plan invalidation are
isolated. Exercise a third connection querying main throughout.

### 4. Durable retention, VACUUM and WAL correctness

Implement branch reachability for pruning, vacuum and index cleanup. Decide when
ordinary XID status can be discarded and how freezing preserves branch ownership
and historical visibility. A permanent xmin pin is not an acceptable solution.
Audit HOT chains, visibility maps, TOAST lifetime and physical file reclamation.
All branch/version mutations must be transactional and redoable; recovery must
not depend on a session branch setting.

Exit: VACUUM FREEZE and aggressive pruning preserve every branch's schema/data;
repeated crash injection around fork and mutations yields either committed state
or a clean abort; long-horizon/wraparound tests establish a safe policy. Aborted
forks and rolled-back writes do not leak reachable metadata or orphan storage.
This gate is required before calling the native prototype persistent and usable.

### 5. Web-app MVP and measured limits

Select one actual app migration suite and implement its required subset, including
sequences/identity if used. Define inherited sequence behavior explicitly; ordinary
sequence updates are not MVCC versions. Implement safe branch-local DROP/TRUNCATE
and FK/unique/default behaviors only as needed and test their dependencies.
Expose branch create/list/connect with connection URLs, clear unsupported-feature
errors, and a disposable native build/test package. No merge is required.

Exit: two app processes run divergent migrations and CRUD concurrently against
A and B, with main serving queries; migration rollback/retry works; schema/data
isolation survives maintenance and restart. Fork stores metadata, not copies of
all user tuples. Report fork latency, write blocking, metadata growth, physical
storage growth, read/write cost and tested operation coverage. Timing thresholds
come from measurements, not invented promises.

## Acceptance fixture

Seed users(id bigint primary key, name text) plus a larger unchanged table.
Create A and B from main. Concurrently:

- A: ADD COLUMN avatar_url text; update user 1; create a new table and insert rows.
- B: ADD COLUMN subscription_plan text; update user 1 differently; create a table
  with the same name as A's but a different definition; insert the same PK as A.
- Main: update user 1 again and create a new table after the fork.

Each connection must see exactly its own additions and its inherited baseline.
Then test same-branch competing writers, rollback, abort during fork, catalog
queries, index probes, VACUUM FREEZE, server restart and crash recovery. Verify
shared baseline storage and no table copy using physical allocation/instrumented
version counts, accounting separately for indexes, WAL and branch metadata.

Use PostgreSQL isolation tests for controlled interleavings and TAP tests for
multi-connection lifecycle/recovery. Add SQL regression tests for syntax and
visibility cases. Keep expected results independent of implementation helpers.
Run ordinary PostgreSQL regression tests after each subsystem integration.

## Primary source map

- Snapshot/transaction: src/include/utils/snapshot.h,
  src/backend/utils/time/snapmgr.c, src/backend/access/transam/xact.c,
  src/backend/storage/ipc/procarray.c.
- Heap: src/backend/access/heap/heapam.c, heapam_visibility.c, heapam_handler.c,
  pruneheap.c, vacuumlazy.c, heapam_xlog.c, src/include/access/htup_details.h.
- Index: src/backend/access/nbtree/nbtinsert.c,
  src/backend/access/index/indexam.c, src/backend/executor/nodeIndexonlyscan.c.
- Catalog/cache/storage: src/backend/catalog/, src/backend/commands/tablecmds.c,
  src/backend/utils/cache/catcache.c, relcache.c, inval.c,
  src/backend/catalog/storage.c.

## Immediate next deliverable

Milestones 0–5 are implemented and tested within their documented limits. The
next work should target a selected application's actual migration requirements,
or improve maintenance concurrency and wraparound validation. The current task
app fixture and measured overhead are recorded in [milestone 5](MILESTONE_5.md).
