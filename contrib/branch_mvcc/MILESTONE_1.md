# Milestone 1: persistent native row branching

## Implemented

A new built-in `branch_heap` table access method integrates the milestone-0
version-edge representation with real PostgreSQL transactions and native tables.
The engine owns branch filtering and native INSERT/UPDATE/DELETE behavior. There
are no views, SQL DML triggers or synthetic transaction-status inputs in this
implementation. Normal heap MVCC and WAL operate on both physical versions and
append-only side metadata; the AM composes those visibility decisions.

Connections select `branch` at startup. Main and children remain independently
writable. A fork copies one branch descriptor, not user tuples or version rows.
Multiple child forks of main are supported, including later fork boundaries;
nested ancestry is deferred. SQL branch administration is `branch_create(text)`.
The setup script initializes the experimental metadata store in an empty test DB.

## Design refinement since milestone 0

The accepted short fork barrier lets us store a committed version frontier instead
of an arbitrary durable historical snapshot. It drains all transactions accessing
branch tables, then records the largest committed metadata ID under a fresh
snapshot. Branches prefer local records and otherwise select main records at/below
that frontier. All records required by a branch remain live ordinary heap tuples;
ordinary vacuum is not being asked to preserve dead historical xmin/xmax versions.
Ownership and predecessor/root identity survive in durable side records rather
than disappearing with a frozen tuple's insertion XID.

This refines the milestone-0 synthetic-snapshot representation without changing
the requirement: forks preserve fixed committed state while main keeps writing.
It does not yet establish safe general maintenance, indefinite wraparound
behavior or garbage collection. Those remain milestone 4. The current AM rejects
VACUUM and the test disables autovacuum on branch tables. Committed versions are
retained indefinitely in this prototype; there is no permanent xmin pin.

Stable version IDs, relation IDs and storage generations identify versions.
Physical TIDs are resolved only while committed versions are retained and
rewrites/destructive schema operations are blocked. Shared predecessor headers
are never updated to point to a sibling successor.

## Native paths

- `src/backend/access/heap/branchheap.c`: core AM scan/fetch filtering, append-only
  replacements and tombstones, fresh-state write validation, branch locks,
  fork barrier and administrative function, conservative utility guard.
- `src/include/access/branchheap.h`: startup selector/guard declarations.
- `src/include/catalog/pg_am.dat`, `pg_proc.dat`: built-in handler and admin API
  using local prototype OIDs 8910–8912. This changes initdb catalog contents and
  requires fresh test clusters; it is not an in-place server upgrade artifact.
- `src/backend/utils/misc/guc_parameters.dat`, `guc_tables.c`: startup-only branch.
- `src/backend/tcop/utility.c`: reject unsupported schema/maintenance utilities in
  the dedicated initialized experiment DB.
- `src/backend/executor/nodeModifyTable.c`: reject ON CONFLICT/MERGE even if an
  executor optimization would bypass speculative-insert AM callbacks.
- Both Make and Meson source lists include the new AM. The Make build is tested;
  Meson has not been executed in this environment.
- `src/test/regress/expected/create_am.out` and `psql.out`: expected listings
  include the new built-in access method. Other regression expectations are unchanged.

## Transaction and concurrency behavior

Branch-table access holds a shared database-local admission lock to transaction
end. Fork obtains it exclusively. This drains readers too and rejects lock
upgrades after the same transaction already accessed a branch table. Prepared
transactions and stronger isolation levels are rejected for the experimental
feature. Metadata uses ordinary qualified heap relations to avoid recursive
branch catalog visibility.

Writers serialize within each branch using distinct transactional advisory-lock
keys. Different branches, including main, can update the same inherited row
without blocking on a shared tuple header. Stale same-branch updates raise 40001
and require transaction retry. No claim of full heap EvalPlanQual equivalence is
made. Read mapping uses the outer statement's MVCC snapshot; writer validation
uses a fresh snapshot after waiting for the writer lock.

Both physical tuple insertion and metadata edges commit/abort together. Aborted
metadata is excluded by ordinary MVCC; the ordinary heap can identify aborted
physical inserts. Sequence gaps do not affect the frontier. Large values use
ordinary heap TOAST, whose references remain reachable from retained versions.

## Limits and next gate

This milestone has no indexes/primary keys, native schema branching, row locks,
COPY FROM, parallel queries, sampling, stronger isolation, nested forks, merge,
branch dropping, version reclamation or tenant permission model. Only explicitly
created permanent branch_heap tables participate; ordinary heap tables remain
unbranched. Unknown branch names fail at first branch-table access rather than
at authentication. Setup and all experimental operations require a trusted
superuser. Unsupported schema/maintenance commands are blocked conservatively
at database scope once initialized; this database must be dedicated to tests.

Next is milestone 2: branch-aware primary keys/index reads and less conservative
same-branch conflict handling. Schema migrations remain milestone 3. Stop after
this milestone and wait for the user's go-ahead.

## Reproduction

See native_m1/README.md and native_m1/run.sh. The Python standard-library suite
uses isolated Unix sockets and disposable clusters and runs actual concurrent
psql connections with database wait-event synchronization. It verifies:

- 1,000 base rows with identical ctid/xmin/xmax across forks and unchanged version
  counts after fork (no user-row/version copies).
- Isolated native INSERT/UPDATE/DELETE, writable main after fork, later forks,
  statement/transaction rollback, savepoints and own writes/RETURNING.
- Large TOAST value isolation, including after crash recovery.
- Independent simultaneous A/B/main writes and uncommitted-row invisibility.
- Same-branch stale-write rejection rather than a lost update.
- Transactional fork rollback and a fork waiting for an active main writer.
- Startup selector immutability, unknown names and unsupported-operation errors.
- Immediate-stop recovery of committed branch data and metadata.

The baseline also caught and led to fixing an aborted-PREPARE utility-guard
assertion. The native suite caught an ON CONFLICT executor optimization bypass;
its rejection now occurs before that optimization. Final test results follow.

### Final executed results

- Assertion-enabled PostgreSQL 20devel Make build and isolated installation:
  passed; compiler log contains no warnings/errors for the final build.
- Core `make check`: all 243 tests passed. Only expected access-method listings
  in create_am/psql were adjusted to include the new built-in AM.
- Native integration suite: passed all six groups above, including 128KB TOAST
  successor isolation and crash recovery. The suite uses actual native tables
  and concurrent backend transactions, not the milestone-0 synthetic fixture.
- Shell runner syntax and Python AST checks: passed. `git diff --check`: passed.
- Test clusters stopped and integration temporary cluster removed. System
  PostgreSQL and its data directories were not changed.
- Logs: /tmp/branch-mvcc-m0/m1-{build,install,check,native-test}.log.

Milestone 1 is complete within the supported/rejected scope above. No milestone-2
indexes or milestone-3 catalog/schema branching have been implemented. Work stops
here until the user's next go-ahead.
