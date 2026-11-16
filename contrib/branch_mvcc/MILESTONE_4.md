# Milestone 4 — durable retention, VACUUM and recovery

Milestone 4 is complete within the existing native prototype's operation set.
Run [native_m4/run.sh](native_m4/run.sh) to reproduce the build and acceptance
tests. The schema/data branching mechanism remains native `branch_heap` plus
WAL-logged ownership/version catalogs in one PostgreSQL database and instance.

## Retention and freezing

For each stable logical row root, retain the version selected by every live
branch: its latest local version, or its main version at/before the fork
frontier. Selected tombstones are retained too. Main participates in this graph,
so a future fork always has the current main state available. Root identities
are numbers and survive collection of the original birth record. There is no
permanently pinned xmin, saved exported snapshot, or historical XID lookup.

`branch_gc()` holds exclusive native admission, computes this graph at a fresh
snapshot, deletes unreachable version records and marks their physical tuples
deleted in the same transaction. These are ordinary heap/WAL changes, with
normal rollback and crash replay. Metadata ownership survives ordinary tuple
freezing; it is independent of the creating transaction's CLOG status. A stale
write whose version was retired receives SQLSTATE 40001 for retry.

The new internal `TABLE_DELETE_KEEP_TOAST` flag avoids prematurely deleting
shared external values and avoids interpreting an obsolete tuple using main's
potentially different tuple descriptor. TOAST GC scans retained physical tuples
using each tuple's birth branch descriptor. Nullable appended columns preserve
that descriptor's layout. It records canonical TOAST relation/value IDs, then
normally MVCC-deletes chunks outside that graph. Old snapshots retain PostgreSQL's
ordinary protection before physical reclamation. Native UPDATE appends through
heap INSERT; that path copies external values into new chunks, so a delayed
INSERT SELECT cannot resurrect a chunk ID already retired by collection.
Rewrites and other pointer-preserving operations remain unsupported.

## Maintenance protocol

SQL VACUUM and autovacuum workers use a coarse group protocol. Logical version
and TOAST deletions commit first. A second transaction reacquires the exclusive
database-local barrier, refreshes metadata and snapshots, collects unreferenced
files, then sweeps all ordinary/TOAST heaps, including system/shared catalogs and
every catalog alias. Visiting an aliased physical file repeatedly is conservative
and safe under the barrier. It avoids treating a hidden sibling tuple as dead.
VACUUM runs with freeze ages zero, index cleanup enabled, no parallel workers and
no file truncation. Standard PostgreSQL vacuum code manages visibility/free-space
maps, index entry removal and freezing. Native UPDATE still creates no HOT chain.

`vac_update_datfrozenxid()` advances database bounds and invokes PostgreSQL's
normal CLOG/subtransaction truncation policy. Native autovacuum workers stay
visible to snapshot horizons during their metadata scans; they do not opt into
the ordinary per-relation PROC_IN_VACUUM path. A guard prevents an older ordinary
vacuum from using that path if native mode activates after it started scanning.
Standalone VACUUM drops its portal command snapshot before the two-phase sweep,
as ordinary VACUUM does. Maintenance requires a writable primary and main.

File GC enumerates actual live storage locators, including mapped system files,
under bypassed catalog visibility. Only unreferenced permanent numeric file
families in the current database's default storage directory are unlinked through
smgr. The collector requires a fresh standalone transaction and exclusive native
admission so it cannot unlink files belonging to an in-progress supported CREATE
or fork. Main-fork unlink follows smgr's checkpoint deferral. Logical correctness
does not rely on orphan files disappearing transactionally: after a crash they
may be collected again. No committed referenced file is deliberately unlinked.

## Executed acceptance checks

- Assertion/debug build and all 243 core SQL regressions.
- Original milestone-1, milestone-2 and milestone-3 acceptance suites.
- Independent simultaneous schema migrations and branch CRUD with shared heap,
  B-tree and inherited TOAST files; normal catalog queries and prepared plans.
- Version and TOAST rollback; collection of 18 obsolete versions in this fixture;
  retained branch answers/tombstones; all-frozen user pages and further indexed
  writes; SQL VACUUM and real launcher/worker autovacuum collection.
- Safe orphan file unlink and checkpoint removal, with a nonrelation marker
  preserved and live branches still usable.
- Crash recovery during uncommitted GC, fork creation and row/TOAST mutation,
  plus replay of committed schema/data and repeated maintenance.
- 1,100,000 real subtransaction IDs, database freezing and actual old `pg_xact`
  segment retirement without losing old branch values.
- Controlled epoch simulation on a fully frozen disposable cluster, followed
  by reads, indexed writes, fork creation, VACUUM and another crash/recovery.

The epoch test deliberately uses pg_resetwal on a disposable cluster. It does
not execute a full 32-bit XID cycle and does not justify using that tool on real
data. Multixact exhaustion, forced raw-XID wrap, replicas/standby feedback and
exhaustive WAL fault injection have not been tested. No new WAL record format is
needed for this prototype's ordinary heap/catalog changes.

## Remaining boundaries

This is a maintenance-capable experimental prototype, not production readiness.
Supported schema operations remain CREATE TABLE with the existing constraints
and nullable ADD COLUMN without defaults. ANALYZE, arbitrary VACUUM options,
VACUUM FULL, file truncation, DROP/TRUNCATE/rewrites, branch deletion, nested
branches, logical decoding and non-superuser isolation remain outside scope.
Maintenance pauses native transactions, scans all branches and repeats aliased
file sweeps; performance is unmeasured. The next milestone is an actual app/ORM
migration fixture and measured costs, with required SQL features added explicitly.
