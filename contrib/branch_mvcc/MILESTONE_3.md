# Milestone 3: the first native parallel migration flow

## Delivered behavior

Main, A and B can independently CREATE TABLE and add nullable columns, then query
and mutate their own schema/data in one logical PostgreSQL database. The same
SQL name can represent different branch-local table definitions. Explicit public
qualification also works. Branch-local migration-history tables and both SQL and
extended-protocol prepared queries are covered. Main remains writable and later
forks capture its current schema/data consistently.

This is a native catalog-visibility implementation on top of the existing native
branch heap/version edges. It does not create one database per branch, use
search_path namespaces to emulate branches, or implement SQL views/triggers.
It does deliberately duplicate supported schema metadata while retaining shared
physical user data/index files.

## Design decision

Give each branch distinct logical object OIDs and ordinary pg_class, pg_type,
pg_attribute, pg_index, pg_constraint and dependency identities. This avoids
asking a single OID's relcache entry to represent incompatible tuple descriptors.
The new branchcatalog.c layer records object ownership before catalog index
insertion, filters managed catalog MVCC/Self/Dirty visibility, and resolves
canonical user-storage/version roots through durable metadata.

At fork, reconstruct the supported table/index definitions with ordinary catalog
machinery in an internal branch context, then point their descriptors at retained
source files. New empty destination files are scheduled for deletion; source
files are never scheduled by the alias operation. The operation is transactional
and rollback has been tested. Storage generations and canonical-root metadata let
milestone-1/2 reads/writes/index checks share the same physical versions even
though SQL objects have different OIDs. Existing index key layouts stay unchanged
under appended nullable columns.

The supported schema consists of public permanent branch_heap tables, built-in
column types, initial immediate primary/unique keys and ordinary NOT NULL
properties. Forks copy metadata proportional to schema size and briefly create
empty files before repointing. No user rows or existing index pages are copied.
Dropping/rebuilding shared descriptors/files is blocked until a reference-aware
storage lifecycle exists.

## Caches, locks and snapshots

Ownership maps are prepared outside buffer locks before simple/extended parse
analysis and before prepared-plan binding. They include global system/metadata/
TOAST descriptors and branch-owned public identities. Unknown public identities
are hidden, so a newly committed sibling object cannot be treated as global.
Ownership-cache replacement is atomic on error. Distinct OIDs keep normal
relcache, row-type and prepared-plan invalidation branch-local. Internal context
switches during fork invalidate local caches and restore context on exceptions.

Same-branch DDL is serialized with a separate advisory lock, followed by a fresh
ownership view after waiting. Different branches have independent DDL/relation
locks. Data writers retain their earlier branch-wide serialization and 40001
retry behavior. Mixed same-branch DDL/DML still uses PostgreSQL's ordinary lock
and deadlock/retry machinery; no general lock-free DDL claim is made.

Fork holds the database-local admission barrier exclusively. Parent DDL/data
writers hold it shared until transaction end. After a wait the fork refreshes
invalidation messages, ownership and catalog-query snapshots as well as the data
frontier. The test specifically starts fork before a parent's migration commits
and checks that the new column, table and data are all inherited together.

Storage extension locks are keyed by canonical storage roots for all heap, index
and TOAST aliases. Ordinary relation OID locks would be distinct while extending
the same file, so this is required in addition to shared buffer content locks.
The acceptance test concurrently grows the shared heap and splits its shared
B-tree while inserting the same primary keys independently in A/B/main.

Catalog heap page/all-visible and sampling shortcuts are gated; catalog index
reads/uniqueness cannot kill entries merely hidden in one branch. Planner
index-only/parallel paths are disabled for scoped catalogs. Ordinary globally
non-vacuumable checks remain unfiltered: branch invisibility does not mean a
catalog tuple is globally dead. System/TOAST storage descriptors remain shared,
while public application catalog visibility is isolated.

## Source changes

- access/heap/branchcatalog.c and access/branchheap.h: ownership preparation,
  native catalog predicates, guarded schema subset, transactional schema fork and
  storage aliases.
- access/heap/branchheap.c: canonical storage roots, catalog refresh at fork,
  branch-local DDL admission and guarded new-table index construction.
- catalog/indexing.c: capture ownership after physical catalog insertion and
  before catalog index insertion (including bulk paths).
- access/heap/heapam_visibility.c: compose catalog ownership with MVCC, Self and
  Dirty visibility.
- access/heap/heapam.c, heapam_handler.c and heapam_indexscan.c: gate visibility
  shortcuts and global-death hints for scoped catalog rows.
- storage/lmgr/lmgr.c: canonical lock identity for shared-file extension,
  conditional extension, waiter counts and unlock.
- tcop/postgres.c: prepare ownership in simple and extended protocol paths.
- optimizer/path/allpaths.c and optimizer/util/plancat.c: safe serial/catalog
  scan planning and index-only restrictions.
- Make/Meson source registration; native_m3 setup/runner/tests/documentation.

The new objects metadata table opts a fresh empty experimental database into
catalog branching. Earlier setup scripts retain their earlier row-only modes,
allowing the milestone-1/2 suites to remain independent regression fixtures.
Neither an in-place upgrade of their databases nor a Meson build is tested.

## Tests and limits

See native_m3/README.md and test.py for the executable acceptance fixture. It
covers simultaneous divergent migrations and concurrent shared-file extension, same-name tables, catalog introspection,
transaction and savepoint rollback, aborted fork file safety, same-branch DDL
commit/abort conflicts, prepared-plan invalidation, actual libpq extended protocol,
later forks, atomic migration/fork boundaries, TOAST and schema/data crash recovery.

Migrations remain a deliberately restricted subset. DROP/TRUNCATE/rewrites,
sequences/defaults/identity, foreign keys, broader constraints and schemas, user
permissions, general ORM compatibility and native maintenance are not complete.
New indexes on inherited shared heaps are rejected. All retained versions and
referenced files remain allocated. The next gate is milestone 4: native retention,
VACUUM/freezing, file reachability, wraparound policy and deeper crash injection.
Stop after this milestone and wait for the next user go-ahead.

## Reproduction and final results

Run native_m3/run.sh with a dedicated work directory. Final executed results are
recorded below once all checks complete.

Final executed results on the assertion-enabled PostgreSQL 20devel Make build:

- Native build/install passed; final compiler log contains no warnings/errors.
- All 243 core regression tests passed; no expected-output changes were needed.
- Milestone-1 and milestone-2 native integration suites passed during integration.
- All eight milestone-3 acceptance groups passed, including distinct typed
  migrations, same-name tables, prepared SQL/libpq protocol, atomic migration/fork
  boundary, concurrent file extension, rollback, TOAST and immediate-stop recovery.
- Server-log assertions found no catalog/tuple/buffer reference-leak warnings,
  failed assertions or segmentation faults in the final acceptance run.
- Shell/Python syntax and tracked/new C whitespace checks passed.
- Logs: /tmp/branch-mvcc-m0/m3-{build,install,check,native-test,m1-regression,m2-regression}.log.
- Disposable test clusters were stopped/removed. System PostgreSQL was unchanged.

Milestone 3 is complete within the documented public-schema migration subset.
Milestone 4 awaits the user's go-ahead. Changes remain local on the existing Git
feature branch; no new commit or push was performed in this milestone.
