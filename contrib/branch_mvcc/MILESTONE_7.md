# Milestone 7 — safe deletion and storage lifecycle

Milestone 7 is implemented for the current native SQL subset. It adds durable
storage identities/references, retained tuple descriptors, a cleanup queue,
connection-fenced branch deletion, and branch-local DROP/TRUNCATE. Build and SQL
usage are in [native_m7/README.md](native_m7/README.md).

## Engine behavior

- Storage families have durable bigint IDs separate from logical table/index
  OIDs, with generation and descriptor references in WAL-logged metadata.
- Logical retirement hides a branch's catalog graph while preserving descriptors
  needed by retained versions and sibling aliases. Cleanup is transactional,
  retryable and uses core dependency deletion and commit-time unlink. A storage
  hook prevents unlink of files still named by another descriptor.
- TRUNCATE keeps visible table/index OIDs and ACLs. Core file-swap logic installs
  empty storage, retains old generations, repairs TOAST dependencies, and keeps
  the old file's correct frozen-XID/multixact bounds. Old user pages are shared,
  not copied. Canonical version metadata retargeting is proportional to the
  retained metadata size and is not yet optimized into direct storage-ID lookups.
- Startup enrolls regular sessions against an immutable branch ID. A private lock
  method protects admission, writer/DDL serialization and connection fences from
  SQL advisory-unlock/reset operations. Session locks survive transaction errors
  and are released during backend shutdown; startup failures release partial
  enrollment correctly.
- Default branch deletion refuses connected sessions. Explicit force fences
  connections, terminates target sessions and waits for rollback. Lock waits have
  a 10-second budget. Startup rechecks branch existence after waiting on the gate.
  Deletion and credential unbinding are transactional; termination is not undone
  by rollback. Name reuse allocates a new ID and never revives old credentials.
- GC evaluates reachability across all live branch descriptors, independent of
  the maintenance connection's catalog visibility. Dropped tables/deleted branches
  stop retaining versions. Descriptor cleanup follows committed logical GC;
  orphan-file collection precedes catalog deletion to preserve rollback safety.

Metadata format 2 is installed atomically in a fresh database. Earlier fixtures
continue to use their earlier formats. Existing sessions must reconnect after
initialization, and there is no automatic prototype data-directory upgrade.

## Validation

Executed with a clean assertion/debug PostgreSQL build:

- All 243 PostgreSQL core SQL regressions passed.
- Original milestone 1–6 acceptance suites passed, including the app fixture,
  autovacuum, real CLOG retirement, controlled epoch simulation and recovery.
- New lifecycle acceptance passed: default refusal with idle/active sessions;
  force rollback of uncommitted writes; session fences surviving errors, DISCARD
  and advisory unlock; private engine locks isolated from public advisory locks.
- Connection startup raced against deletion commit and rollback. Committed
  deletion rejected waiting startup; rollback admitted it. Recreated names used
  new IDs, and stale credentials stayed rejected.
- DROP of child and canonical main tables preserved sibling data/TOAST through
  VACUUM. Private branch files were reclaimed. Standalone index DROP retained
  sibling indexes; direct constraint-index DROP and active-cursor DDL were denied.
- TRUNCATE retained table/index OIDs and old storage IDs, isolated new main/child
  generations, preserved external values, and rolled back correctly.
- Crashes during uncommitted branch deletion, TRUNCATE and GC/catalog cleanup
  restored committed state. Pending queue work remained retryable and files were
  reclaimed safely afterward. Committed generation changes survived recovery.
- A real format-2 autovacuum worker processed the durable queue and reclaimed
  a deleted branch's private file; repeated TRUNCATE preserved multiple inherited
  generations concurrently.
- Non-superuser migration roles exercised inherited ownership and transactional
  DROP after writes; CRUD-only application roles were denied DROP/TRUNCATE.
- A blocking sibling transaction caused bounded deletion admission to return
  55P03; retry succeeded once the blocker ended. CLI deletion was exercised.

Build logs remain under /tmp/branch-mvcc-m5/m7-*.log. The reproducible full runner
is native_m7/run.sh. Meson sources are registered but not exercised on this host.

## Remaining limits

This is a developer-beta checkpoint, not production certification. Reclamation
is conservative; live references and retained tuple layouts intentionally keep
some archives. Maintenance and lifecycle work still pause native transactions.
Only the existing public-table/index dependency graph is supported. Foreign keys,
sequences/defaults, broader schema evolution and real framework validation remain
milestone 8; performance work and packaged runtimes remain milestones 9 and 10.
Full raw-XID wrap and exhaustive fault injection remain untested. Session lifecycle
locks currently reject branch IDs above 2^32-1. No branch merge or nested forks.
