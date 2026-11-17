# Milestone 7: branch deletion and retained storage generations

The native engine supports safe branch deletion and branch-local DROP TABLE,
DROP INDEX and TRUNCATE for its existing supported schema subset. One instance
and database still share unchanged heaps/indexes/TOAST across branches. Retired
catalog descriptors preserve tuple layouts and physical access while other
branches need them; a durable queue reclaims descriptors and files afterward.

## Build and initialize

```sh
contrib/branch_mvcc/native_m7/run.sh /tmp/branch-mvcc-m7
```

This performs a clean assertion/debug build, core regressions, milestones 1–6,
and the new lifecycle acceptance suite. A clean build matters because PGPROC's
shared-memory layout and the lock-method table changed. Make source registration
is tested; Meson registration is included but has not been executed here.

For manual use, create a fresh empty disposable database with the modified
server and load this directory's `setup.sql` once as administrator. The setup
transaction installs metadata format 2 atomically. Reconnect afterward before
using native data; connections predating lifecycle initialization are rejected
when entering the native executor. This is not an in-place upgrade path for
existing prototype data directories. Format 1 remains available for earlier
acceptance fixtures, with its earlier feature limits.

To rerun only lifecycle tests against an installed build:

```sh
python3 -B contrib/branch_mvcc/native_m7/test.py --bindir /path/to/native/bin
```

## Delete a branch

From an administrative connection to main:

```sql
SELECT branch_drop('feature_a');        -- refuse connected branch sessions
SELECT branch_drop('feature_a', true);  -- fence, terminate sessions and delete
```

Main cannot be deleted. Creation still forks main only. Default deletion returns
SQLSTATE 55006 while any enrolled session is connected, including idle sessions.
Force sends SIGTERM only to the target branch's enrolled processes and waits for
them to release their locks and roll back. Connection-fence, drain and native
admission waits share a 10-second budget; timeout returns 55P03. A caller's
shorter statement_timeout can cancel sooner. Metadata operations and catalog
locking remain subject to normal PostgreSQL timeouts.

New connections wait behind deletion's connection gate. After deletion commits,
startup rechecks the immutable branch ID at a fresh snapshot and rejects the old
branch. If deletion rolls back, waiting connections may proceed. SQL advisory
unlock functions and DISCARD ALL cannot remove internal engine locks.

Logical deletion, role-binding revocation and queued catalog retirement are
transactional. Forced disconnections cannot be undone by rollback. A recreated
name receives a new branch ID; old credentials are unbound and cannot attach to
it without an explicit administrator binding. Deleted branch records are removed
once their versions and role references are gone. IDs are never reset by cleanup.

The existing developer CLI exposes the same operation:

```sh
python3 -B contrib/branch_mvcc/native_m5/branches.py \
  --bindir /path/to/native/bin --dsn "$DATABASE_URL" drop feature_a
python3 -B contrib/branch_mvcc/native_m5/branches.py \
  --bindir /path/to/native/bin --dsn "$DATABASE_URL" drop feature_a --force
```

`list` shows active branches. The CLI does not remove PostgreSQL login roles;
role provisioning and credential lifecycle remain administrative operations.

## DROP and TRUNCATE

Ordinary SQL runs against the connection's branch:

```sql
DROP TABLE task_notes;
DROP INDEX task_lookup;
TRUNCATE tasks;
```

Table/index owners may DROP them. TRUNCATE uses normal TRUNCATE privileges.
Applications granted only CRUD cannot perform these operations. A constraint's
index cannot be dropped directly (2BP01). Objects outside supported public native
tables/indexes, concurrent DROP INDEX, and TRUNCATE RESTART IDENTITY are rejected.
Foreign keys, sequences and additional migration features remain milestone 8.
RESTRICT/CASCADE operate within the currently supported graph of tables, types,
constraints, indexes and TOAST; this is not support for unsupported dependent
object types or extension-defined dependencies.

DROP hides the logical object immediately in that branch. Physical deletion is
deferred. TRUNCATE retains the same table, index and constraint OIDs, creates an
empty generation, and preserves inherited generations for siblings. Permissions
and the visible schema remain attached to the original logical objects. Shared
old heap/index pages are not copied. On a canonical-generation change, retained
version metadata is retargeted transactionally to its archive descriptor; this
can require work proportional to the retained metadata size.

DDL resolves and locks logical relations before acquiring exclusive native
admission. This avoids a reader/DDL lock cycle. If a transaction already holds
shared admission and cannot upgrade immediately, it gets 40001 and must retry
its whole transaction. Active cursors/queries in the same session prevent DROP
or TRUNCATE (55006). Transaction rollback restores the original schema, data and
storage mappings.

## Reclaim storage

Prefer the complete maintenance protocol:

```sql
VACUUM FREEZE;
```

It commits logical version/TOAST collection, reacquires admission, sweeps old
unreferenced files, processes the catalog cleanup queue, then vacuums/freezes.
All live branch descriptors participate in reachability, including hidden sibling
catalog entries. Selected tombstones and inherited main versions remain retained.

An administrator can explicitly retry queued descriptor cleanup:

```sql
SELECT branch_cleanup(); -- number of queued root descriptors reclaimed
```

This is transactional and idempotent. It conservatively leaves descriptors needed
by retained versions or live storage aliases. It does not replace logical GC;
calling it before GC can legitimately reclaim nothing. Core dependency deletion
and commit-time storage unlink perform reclamation. The storage hook refuses
unlink while any other catalog descriptor names the same physical file. Final
main-fork removal may wait for a checkpoint. The nontransactional orphan-file
collector runs before catalog cleanup so it cannot unlink a file whose catalog
retirement might still roll back.

Administrative diagnostics:

```sql
SELECT * FROM branch_native.storage;       -- durable ID, anchor, generation
SELECT * FROM branch_native.storage_refs;  -- descriptor-to-storage references
SELECT * FROM branch_native.cleanup;       -- pending descriptor retirement
```

Storage IDs remain stable when TRUNCATE moves old physical storage to a retained
anchor; the visible table keeps its OID and receives a new storage identity.
Descriptors retain the correct freeze bounds and birth layouts. No permanent
xmin pin or separate server is introduced.

## Tested boundaries

The acceptance suite covers session refusal/force/unlock resistance, startup
races with commit/rollback, stale credentials and name reuse, file reclamation,
canonical main DROP with sibling reads, stable-OID TRUNCATE and retained TOAST,
standalone indexes, dependencies, active cursors, non-superuser permissions,
bounded admission waits, and crashes during uncommitted deletion, TRUNCATE and
cleanup. A real format-2 autovacuum worker also processes the cleanup queue and reclaims
private files. Earlier suites cover real CLOG retirement and epoch simulation.

This remains a developer prototype. Maintenance and lifecycle operations take a
coarse database-wide native admission barrier. A retained archive/queue entry
may be necessary rather than a leak. The session lock key supports branch IDs up
to 2^32-1 and rejects larger IDs. Full raw-XID wrap, exhaustive crash injection,
replicas, nested forks, merge and general framework compatibility remain outside
this checkpoint. See [MILESTONE_7.md](../MILESTONE_7.md).
