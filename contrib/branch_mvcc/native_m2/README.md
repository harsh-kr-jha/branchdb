# Milestone 2: native branch-aware indexes and uniqueness

Build and test the modified PostgreSQL server in an isolated directory:

```sh
contrib/branch_mvcc/native_m2/run.sh /tmp/branch-mvcc-m2
```

The script configures an assertion/debug build if needed, installs independently
of system PostgreSQL, runs the core regression suite, then both milestone-1 and
milestone-2 native integration suites. Use a dedicated work path without spaces
and reuse it only with this checkout/configuration. Build prerequisites are the
same as milestone 1 (C compiler, Make, Bison/Flex, zlib and Python 3). Logs are
retained; integration clusters are stopped and removed. To rerun only this suite:

```sh
python3 contrib/branch_mvcc/native_m2/test.py --bindir /tmp/branch-mvcc-m2/install/bin
```

## Manual use

In an empty disposable database using the modified server, initialize the
metadata with `../native_m1/setup.sql`. This is still a trusted-superuser test.
Create supported tables and indexes before making any forks:

```sql
CREATE TABLE users (
    id bigint PRIMARY KEY,
    handle text UNIQUE,
    name text
) USING branch_heap WITH (autovacuum_enabled=false);
CREATE INDEX users_name ON users(name);
INSERT INTO users VALUES (1, 'harsh', 'Harsh');
SELECT branch_create('a');
SELECT branch_create('b');
```

Connect with `PGOPTIONS='-c branch=a'` or `PGOPTIONS='-c branch=b'`:

```sql
-- A: inherited keys conflict locally.
INSERT INTO users VALUES (1, 'another-handle', 'Conflict'); -- 23505

-- A: changing a key shadows the ancestor's old index entry.
UPDATE users SET handle='harsh-a' WHERE id=1;
SELECT * FROM users WHERE handle='harsh'; -- empty in A; inherited row in B

-- A and B can each insert this same primary/unique key independently.
INSERT INTO users VALUES (2, 'same-handle', 'Local');
```

DELETE permits reuse of the deleted key in that branch. Parent changes after
fork remain excluded from children even when the query uses an index. Startup
branch selection, native data storage and fork barriers are unchanged.

## Supported scope

- Immediate B-tree primary keys and unique indexes, including composite keys.
- Ordinary B-tree column indexes and plain index scans, including ordered,
  backward, IN-array and parameterized rescan paths covered by the tests.
- Standard NULL-distinct unique keys and `NULLS NOT DISTINCT` behavior.
- Native key changes, delete/reinsert, transaction/savepoint rollback and recovery.
- Index creation on populated main tables before any forks. All retained committed
  versions receive entries; only current main versions participate in build-time
  uniqueness. Initial primary/unique constraints can be declared in CREATE TABLE.
- The planner avoids index-only, bitmap and parallel branch scans. Reads fall back
  to supported serial scans; worker access remains rejected defensively.

No index creation after a fork, concurrent/parallel index builds, partial or
expression indexes, non-B-tree indexes, deferred/exclusion constraints, foreign
keys, ON CONFLICT, MERGE, row locks, stronger isolation, branch-local schema DDL,
VACUUM/ANALYZE or garbage collection is enabled. Generic ALTER TABLE migrations
remain milestone 3. The only permitted initial ALTER utility form is the internal
AT_AddIndex used for initial index constraints. Ordinary heap tables remain
unbranched. Do not use this experimental database for unrelated workloads.

Writers still serialize within a branch; this is a correctness implementation,
not a claim of per-key write parallelism. Same-branch stale UPDATE/DELETE raises
40001 and requires whole-transaction retry; full EvalPlanQual semantics are
intentionally deferred. All committed versions/index references are retained.

## Why shared indexes are safe in this prototype

Each branch resolves a set of physical TIDs from statement-snapshot metadata.
Plain index scans enumerate normal B-tree candidates and fetch only those
selected TIDs through ordinary heap MVCC. A changed key's predecessor is excluded
regardless of whether its replacement matches the query predicate. A locally
hidden candidate never sets `kill_prior_tuple` or a globally-dead result.

Before an INSERT/UPDATE enters B-tree insertion, the AM prepares a fresh immutable
set of branch-visible TIDs while holding that branch's write lock. Native B-tree
uniqueness checks call the AM's Dirty/Self lookup against this prepared set,
without SPI/catalog work or lock waits while B-tree buffers are locked. Sibling
versions are excluded; inherited live keys conflict; shadowed/tombstoned keys do
not. Updates insert every index entry for their new physical version (no HOT).

The prepared set is transaction-owned and invalidated on transaction and
subtransaction events. Same-branch writers cannot race it because the branch-wide
writer lock lasts to transaction end. That lock is a required invariant of this
implementation; replacing it with per-key locks requires a new concurrency proof.

At build time B-tree's ordinary secondary spool stores superseded versions
without testing them as current unique conflicts. Retaining these entries keeps
old statement snapshots usable; index-only reads remain disabled because ordinary
visibility-map bits do not express branch shadowing. Version reclamation and
maintenance still need milestone 4.
