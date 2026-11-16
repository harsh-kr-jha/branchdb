# Milestone 4: branch retention and storage maintenance

This completes the maintenance milestone for the restricted native prototype.
It extends milestone 3's shared physical heaps/indexes and branch-local catalogs;
it does not create another server or copy unchanged user data at fork.
See [MILESTONE_4.md](../MILESTONE_4.md) for invariants and tested limits.

## Build and test

```sh
contrib/branch_mvcc/native_m4/run.sh /tmp/branch-mvcc-m4
```

Use a work path without spaces. This builds an assertion/debug server, installs
it into that work directory, runs all core regressions and earlier native suites,
and runs the expanded milestone-4 suite. Requirements are the usual PostgreSQL
C/Make/Bison/Flex/zlib tools and Python 3. The stress helper uses the installed
PGXS headers to build a temporary C module; it is never installed in PostgreSQL's
library directory. Integration tests initialize their own clusters, disable TCP,
and stop/remove them. Meson source registration is included but untested here.

To test an existing build:

```sh
python3 contrib/branch_mvcc/native_m4/test.py --bindir /path/to/native/bin
```

Fresh clusters are required because this milestone adds built-in functions.
Initialize native mode with [native_m3/setup.sql](../native_m3/setup.sql) in a
fresh empty disposable database. Follow [native_m3](../native_m3/README.md) for
branch creation and startup connection selection (`PGOPTIONS='-c branch=a'`).
Those schema/migration restrictions still apply. Maintenance below replaces the
earlier restriction that VACUUM/autovacuum were unsupported.

## Maintenance commands

Connect to **main as the trusted superuser**, using READ COMMITTED:

```sql
VACUUM FREEZE;
```

Ordinary `VACUUM`, `VACUUM FREEZE`, and `VACUUM (FREEZE) users` run the same
conservative database-wide protocol. A table name is checked for existence but
does not narrow the sweep. This blocks native reads, writes and migrations
while each phase holds the admission barrier. Autovacuum workers use this same
protocol automatically in native databases, including for anti-wraparound work.

1. Under exclusive native admission, mark globally obsolete versions deleted
   and collect unreachable external TOAST values, then commit.
2. Reacquire admission with fresh metadata/snapshots, collect unreferenced local
   relation files, vacuum/freeze all heaps and catalogs, clean indexes, and
   update the database frozen-ID bounds through PostgreSQL's normal machinery.

Diagnostic building blocks are also exposed:

```sql
SELECT branch_gc();        -- obsolete logical version count; transactional
SELECT branch_gc_toast();  -- unreachable external chunk count; transactional
SELECT branch_gc_files();  -- unreferenced local file-family count
SELECT branch_maintain();  -- physical heap/index freeze sweep
```

Prefer standalone `VACUUM` for the complete protocol. `branch_maintain()` alone
does not perform logical version or TOAST collection. File GC requires a fresh
standalone transaction; unlink is nontransactional and final main-fork removal
may wait for a checkpoint. It only scans the current database's default storage
directory, protects live aliases and mapped catalog files, and ignores temp and
nonnumeric names. It does not collect shared/global or arbitrary tablespaces.

## Scope and validation

The suite covers parallel migrations, retained tombstones and branch values,
indexed writes after collection, divergent tuple descriptors, external TOAST,
GC/TOAST rollback, standard VACUUM, a real autovacuum worker, orphan unlink, and
crashes during uncommitted fork/GC/writes. It allocates **1,100,000 real
subtransaction IDs** and verifies actual retirement of an old `pg_xact` segment
while old branches remain readable. It then uses `pg_resetwal` **only on its
fully frozen disposable cluster** to simulate an XID epoch change and checks
subsequent writes, forks, maintenance and crash recovery. This is controlled
epoch simulation, not a run of 2^32 transactions or exhaustive wraparound proof.

This remains an experimental trusted-superuser implementation. ANALYZE,
VACUUM FULL, parallel maintenance, file truncation, arbitrary VACUUM options,
DROP/TRUNCATE/rewrites, branch deletion, nested branches, replicas and logical
decoding are outside this milestone. The sweep is coarse and scans metadata for
all branches; its performance and full production crash coverage are unproven.
