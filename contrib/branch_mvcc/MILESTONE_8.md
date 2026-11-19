# Milestone 8 — pinned framework compatibility

Milestone 8 is implemented for the tested Prisma 6.19.0 and Django 5.2.8
fixtures. Reproduction, dependencies and explicit SQL boundaries are in
[native_m8/README.md](native_m8/README.md).

## Result

Applications can run ordinary ORM queries and migrations against independent,
authenticated branches in one PostgreSQL instance and database. Forks share
unchanged heap/index/TOAST storage; sequences have independent physical state.
Prisma's disposable shadow database lives in the same instance and replays only
migration history. No separate application database server is used per branch.

The new metadata format 3 enables serial/identity sequences, audited defaults,
NOT NULL and CHECK validation, foreign keys including deferred checks and
cascades, speculative insertion/upsert, additional schema evolution and logical
schema reset. Earlier formats retain their previous guarded behavior.

New indexes use private index storage over shared heap data. Builds filter out
sibling versions before decoding potentially different tuple layouts and retain
branch history for older statement snapshots. A column removal or removal of an
indexed constraint installs a private table generation, copies its selected
rows, rebuilds indexes and retains the previous descriptors/files for siblings.
This preserves table OIDs and rollback/recovery, but can copy an entire affected
table. Nullable additions, fast defaults and index additions retain shared heaps.

Sequence access participates in fork admission, even without table writes.
Fork captures sequence configuration and reserved high-water state, including
renamed identity sequences. Defaults retain their attribute missing values and
physical dropped-column positions. Foreign-key triggers follow their owning
constraint's visibility, including triggers installed on a surviving parent.
DROP/TRUNCATE check incoming references, implement CASCADE and transactional
RESTART IDENTITY. Standalone sequence deletion uses core dependency handling.

Native speculative inserts use core heap completion/WAL and transactional side
metadata. FK row locking uses the existing branch writer lock without changing
a shared tuple's xmax. Sequence files, metadata and generation swaps use existing
PostgreSQL WAL and transaction protocols; no new WAL record format was needed.

## Executed validation

On macOS ARM64 with an assertion/debug PostgreSQL 20devel build:

- All **243 core SQL regressions** passed.
- All **milestone 1–7 acceptance suites** passed, including previous application,
  authenticated-role, deletion-race, maintenance, autovacuum and recovery checks.
- Real Prisma client generation, migrate deploy, db pull, migrate dev with a
  separate shadow database, and reset passed. Nested writes, defaults, upsert,
  invalid-FK rejection and cascading deletion passed. Application and migration
  roles were bound to their branches without superuser privileges; two concurrent
  application processes independently wrote the same unique key. Reset preserved
  main and exercised the restricted migration role's table-based fallback.
- Real Django initial/incremental migrations and complete reversal passed.
  Two parallel branch processes exercised inherited ORM reads, update-or-create,
  deferred FK rejection and cascade deletion. Reversing an added defaulted field
  and index in one branch preserved the main schema and data.
- Native acceptance passed: shared heaps after fork/index creation, divergent
  sibling tuple layouts, unique-index validation, independent serial/identity
  counters, renamed identity sequences, default preservation, existing-row
  NOT NULL/CHECK validation, deferred FK/key cascades and eight concurrent upserts.
- FK-aware DROP/TRUNCATE, identity-restart rollback, retired child triggers,
  standalone/owned sequence dependencies, schema rollback, forks after dropped
  columns/fast defaults, sequence-only fork fencing, numeric-OID sequence-default
  denial, logical reset, crash recovery and retained TOAST after VACUUM passed.
- Build completed without compiler warnings; `git diff --check` passed.

The fixture runtime was Node 23.6.1, Python 3.14, Django 5.2.8 and psycopg 3.2.12.
Make builds were exercised; Meson source registration is present but unexecuted.

## Remaining boundary

This certifies these committed fixtures and SQL patterns, not general framework
project compatibility. Type-changing rewrites, enums, generated columns,
user-defined triggers, custom schemas, advanced indexes and broader transaction
isolation remain unsupported. Maintenance and destructive storage transitions
still use coarse admission locks. A full raw-XID wrap cycle remains untested.

Milestone 9 is profiling/optimization; milestone 10 is runtime packaging and
clean-machine cross-platform validation. Neither is included in this checkpoint.
