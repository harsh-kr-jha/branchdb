# Milestone 3: native schema and data branches

The modified PostgreSQL server supports a small native migration subset in one
logical database: branch-local CREATE TABLE, initial primary/unique constraints,
and nullable ADD COLUMN without defaults. A/B/main use ordinary SQL and startup
connection selection, including schema-qualified `public.users`. Managed public
catalog rows are filtered natively; this is not search_path isolation or views.

## Build and test

```sh
contrib/branch_mvcc/native_m3/run.sh /tmp/branch-mvcc-m3
```

Use a dedicated work path without spaces. The script builds this checkout with
assertions/debug symbols into its own prefix, runs core regressions, reruns the
milestone-1/2 suites and runs milestone 3. Integration clusters use private Unix
sockets with TCP disabled and are stopped/removed. Logs remain in the build work
directory. It requires the normal C/Make/Bison/Flex/zlib build tools and Python 3.
The extended-protocol test loads libpq from that install with Python ctypes
(macOS dylib or Linux lib/lib64 shared library). Meson has not been run here.

To rerun only the new suite:

```sh
python3 contrib/branch_mvcc/native_m3/test.py --bindir /tmp/branch-mvcc-m3/install/bin
```

## Manual use

Start a fresh cluster using the modified server and create an empty disposable
database as its trusted superuser. Run this directory's setup.sql once. It adds
`branch_native.objects` to enable catalog branching; older milestone setup files
retain their earlier row-only behavior and do not enable this mode. Do not add
this setup to an existing populated database or an existing milestone-1/2 DB.

```sh
/path/to/native/bin/psql -X -v ON_ERROR_STOP=1 -d branch_demo \
  -f contrib/branch_mvcc/native_m3/setup.sql
```

In main:

```sql
CREATE TABLE users(id bigint PRIMARY KEY, name text)
  WITH (autovacuum_enabled=false);
CREATE TABLE migrations(version text PRIMARY KEY)
  WITH (autovacuum_enabled=false);
INSERT INTO users VALUES (1, 'Harsh');
SELECT branch_create('a');
SELECT branch_create('b');
```

In this opt-in database, ordinary CREATE TABLE defaults to branch_heap and
explicitly targets public. Other application schemas/access methods are rejected. New tables automatically
set autovacuum_enabled=false; the integration cluster additionally disables
autovacuum globally because native maintenance is outside this milestone.
Connect two clients using `PGOPTIONS='-c branch=a'` and `PGOPTIONS='-c branch=b'`:

```sql
-- A: can run while B's transaction is open.
BEGIN;
ALTER TABLE public.users ADD COLUMN avatar_url text;
UPDATE users SET avatar_url='avatar.png', name='A' WHERE id=1;
CREATE TABLE notes(id bigint PRIMARY KEY, body text);
INSERT INTO notes VALUES(1,'A note');
INSERT INTO migrations VALUES('a-001');
COMMIT;
```

```sql
-- B
BEGIN;
ALTER TABLE public.users ADD COLUMN subscription_plan integer;
UPDATE users SET subscription_plan=42, name='B' WHERE id=1;
CREATE TABLE notes(id bigint PRIMARY KEY, priority integer);
INSERT INTO notes VALUES(1,7);
INSERT INTO migrations VALUES('b-001');
COMMIT;
```

A sees avatar_url/body; B sees subscription_plan/priority; main retains its own
schema/data. Main can add its own columns and tables after fork. Later forks of
main inherit its then-current schema/data, not earlier siblings' schemas.

## Representation and consistency

At fork the engine creates distinct table/type/index/constraint identities and
ordinary dependency records for the supported schema. It reuses retained heap,
TOAST and index files by repointing the new descriptors; only their newly created
empty files are scheduled for deletion. Catalog metadata is duplicated, user
rows and existing index pages are not copied. The fork's work scales with schema
size; this is not a constant-time or zero-I/O fork.

The durable objects table assigns branch ownership to managed catalog identities
and maps branch table descriptors to their canonical storage/version root. Reads
compose catalog ownership with PostgreSQL's ordinary tuple MVCC. Capturing
ownership before catalog index insertion makes same-name catalog uniqueness
branch-aware. Distinct OIDs let PostgreSQL's normal relcache/typecache/plan
invalidation work on branch-local objects. Temporary internal fork context changes
flush local caches and restore the caller's branch on success/error.

Ownership is prepared before simple/extended query analysis and prepared-plan
binding. It never queries metadata under a catalog heap/index buffer lock.
Unknown public identities are hidden rather than exposed as global. Schema DDL
serializes within one branch; migrations in different branches use distinct
relation identities and locks. The shared fork barrier drains branch-table
accesses and DDL, then refreshes both ownership and schema snapshots before
publishing the catalog copies and data frontier atomically. Heap, index and TOAST
aliases also share extension locks through their canonical storage roots; their
distinct logical OIDs must not allow concurrent extension of the same file.

Public pg_class, pg_type, pg_attribute, pg_index, pg_constraint, pg_depend and
related descriptions/default metadata reflect the selected branch. System,
metadata and TOAST storage descriptors remain global; this is not a tenant
security boundary. Ordinary visibility-map/index-only shortcuts cannot skip the
catalog ownership checks. Public catalog/branch scans are kept out of workers.

## Deliberate scope

Supported: permanent public tables with built-in column types, immediate initial
primary/unique constraints, ordinary unnamed NOT NULL declarations at CREATE,
nullable ADD COLUMN without defaults, transactional DDL rollback/savepoints,
ordinary DML and inherited indexes, reconnects, SQL and extended-protocol prepared
queries, and the tested crash-recovery cases. Use autovacuum_enabled=false on
experimental tables; VACUUM/ANALYZE and maintenance support remain milestone 4.

Rejected: DROP/TRUNCATE/rewrites; changing existing column types; defaults,
sequences/SERIAL/identity/generated columns; foreign keys/CHECK/deferred
constraints; custom-named NOT NULL constraints; custom types/collations/storage
options/tablespaces; application schemas beyond public; partitioning/inheritance;
ON CONFLICT/MERGE; row-lock API; stronger isolation; nested forks/merge; branch
permissions and logical replication semantics. No full ORM migration suite is
claimed yet. This is the first migration subset, not a production-compatible fork.

Existing indexes on inherited tables are shared and usable. New indexes on
shared inherited storage remain rejected; initial indexes on newly created
branch tables are permitted. Writers still serialize within a branch. Old user
versions and referenced files are retained; version/file reclamation is not
implemented. Direct superuser changes to metadata/catalogs can bypass invariants.

## Acceptance coverage

The suite proves distinct catalog OIDs sharing a relfilenode with unchanged user
version counts, divergent column types, same-name/different-shape tables, catalog
and information_schema introspection, parallel A/B migrations while main serves
queries, migration-history isolation, DDL/fork rollback, savepoints, prepared-plan
invalidation and actual libpq Parse/Bind/Execute, same-branch DDL commit/abort
conflicts, writable main, and a fork waiting for schema/data changes to commit.

It also checks inherited/shared TOAST, new TOAST storage created by ADD COLUMN,
inherited key conflicts, sibling key independence, rejected destructive commands,
reconnect and immediate-stop recovery of every branch's schema/data. Concurrent
inserts force shared heap growth and index page splits with branch-local keys. Server logs
are checked for assertion failures and reference-leak warnings. Exhaustive crash
injection, maintenance and wraparound testing belong to milestone 4.
