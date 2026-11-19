# Milestone 8: framework migrations and application workflows

This checkpoint validates Prisma **6.19.0** and Django **5.2.8**, with
psycopg **3.2.12**, against the modified PostgreSQL engine. Both fixtures use
users, projects, tasks and comments. Applications use their ordinary ORM and SQL;
connections select an immutable branch at startup.

Branches still live in one database and one server. Forks duplicate catalog
metadata and create independent sequence files, while sharing unchanged heap,
index and TOAST files. Prisma's shadow database is a separate, ordinary database
**in that same disposable server**, populated from migrations; it receives no
application rows.

## Reproduce

Build the modified engine and check the preceding milestones:

```sh
contrib/branch_mvcc/native_m7/run.sh /tmp/branch-mvcc-m8
```

Install the pinned fixture dependencies in an isolated environment:

```sh
python3 -m venv /tmp/branch-mvcc-m8-venv
/tmp/branch-mvcc-m8-venv/bin/pip install -r contrib/branch_mvcc/native_m8/requirements.txt
npm --prefix contrib/branch_mvcc/native_m8/prisma ci
```

Then run native acceptance and the actual frameworks:

```sh
/tmp/branch-mvcc-m8-venv/bin/python -B contrib/branch_mvcc/native_m8/test.py --bindir /tmp/branch-mvcc-m8/install/bin
/tmp/branch-mvcc-m8-venv/bin/python -B contrib/branch_mvcc/native_m8/probe.py --bindir /tmp/branch-mvcc-m8/install/bin
```

The framework harness generates the Prisma client, starts its own temporary
cluster, runs the CLIs and applications, and removes the cluster on exit.
Development migrations run in a pseudo-terminal because Prisma requires an
interactive terminal for that command. Generated migrations live in a temporary
project copy; committed fixtures remain unchanged. The tests need Unix sockets,
Node/npm, Python with Django support, and permission to start local processes.
The exercised platform is macOS ARM64, Node 23.6.1 and Python 3.14. Cross-platform
runtime certification belongs to milestone 10.

For manual experiments, initialize a **fresh disposable database** with this
modified server, load `setup.sql` once as administrator, and reconnect. It
atomically installs metadata format **3**, preserving earlier formats for their
existing acceptance suites. There is no automatic upgrade of prototype data.
Use separate authenticated branch-bound migration and application roles as in
[milestone 6](../native_m6/README.md).

## What is covered

- Real Prisma `migrate deploy`, introspection (`db pull`), `migrate dev` with a
  shadow database, and `migrate reset`; client nested writes, reads, upsert,
  invalid-FK rejection and database cascades. Two simultaneous application
  processes write the same unique key on different branches using separate
  bound roles. Development migrations and reset use a migration role without
  superuser privileges, exercising Prisma's table-based reset fallback.
- Real Django initial and incremental migrations, a generated defaulted field
  and index, ORM create/update-or-create/read/delete, deferred FK rejection,
  cascades, parallel branch applications, branch-local reverse migration and
  complete migration reversal.
- Independent serial/identity state, including renamed identity sequences;
  preserved defaults and missing-attribute metadata at fork; column addition,
  rename, defaults, NOT NULL, CHECK, FK validation and immediate unique indexes.
- Deferred FK checks and cascading key updates/deletes; same-branch concurrent
  upserts; FK-aware DROP/TRUNCATE and transactional RESTART IDENTITY. Retired FK
  triggers disappear with their owning constraint even on a surviving parent.
- New private indexes over a shared heap, including inherited defaulted columns.
  Builds filter sibling TIDs before decoding tuples, retain this branch's
  history for older statement snapshots, and validate only its selected rows.
- Transactional schema rollback, forks after column removal or fast defaults,
  sequence-only writers blocking fork, isolated reset, immediate-stop recovery,
  and retained external values surviving VACUUM and branch cleanup.

## Storage and cost

Normal DML remains append-only: shared versions are never updated through a
shared `xmax`. Native private writer locks serialize writers within a branch;
foreign-key row locks use that serialization rather than modifying a shared
row's lock fields. Speculative insertion and its completion follow core heap
and WAL protocols, with transactional branch-version bookkeeping.

Creating an index builds a private index and keeps the shared heap. Adding
nullable columns and fast defaults changes metadata. Removing a column or an
indexed constraint copies **that table's branch-visible rows** into a private
storage generation and rebuilds its indexes, preserving logical OIDs and old
storage for siblings. Thus destructive schema changes can copy an entire table;
there is no promise that every migration is metadata-only. Existing WAL,
transactional file swaps and retained descriptors provide recovery and cleanup.

`DROP SCHEMA public CASCADE` performs a branch-local object reset and keeps the
shared namespace. Core schema-owner checks still apply; a less privileged
migration role can use Prisma's ordinary table-based fallback.

## Boundaries

This certifies the committed fixture workflows, not arbitrary Prisma/Django
projects. Public permanent tables, built-in column types, audited defaults and
ordinary immediate B-tree indexes are the supported subset. Type-changing
rewrites, enums/custom types, generated columns, user triggers, custom schemas,
expression/partial/concurrent/deferred indexes, partitioning, MERGE and
NOWAIT/SKIP LOCKED remain unsupported. Transactions remain READ COMMITTED.
Sequences retain PostgreSQL's nontransactional allocation and cache gaps; fork
copies the reserved sequence high-water state, not another connection's currval.

Maintenance and destructive storage transitions still take coarse admission
locks. Milestone 9 covers profiling and metadata/index lookup optimization;
10 covers distributable runtimes. Branch merging, concurrent maintenance and a
full raw 32-bit XID cycle remain outside this checkpoint.
