# Milestone 9 — measured performance checkpoint

Milestone 9 is implemented. The changes and reproduction commands are in
[native_m9/README.md](native_m9/README.md); the complete matrix, control gate and
memory costs are in [MEASUREMENTS.md](native_m9/MEASUREMENTS.md). Raw baseline,
repeat and candidate artifacts, release configuration and binary hashes are
preserved in [native_m9/results/](native_m9/results/).

## Engine changes

- Ownership/retirement maps are cached until catalog or protected metadata
  invalidation, including remote DDL and transaction/savepoint aborts. Callbacks
  mark an epoch and never rebuild maps under another subsystem's locks. Changes
  to ordinary-heap ownership tables now publish shared relcache invalidation.
- A backend-private typed metadata module uses existing name, family/root and
  TID indexes. Indexed reads check candidate TIDs and their root history using
  the statement snapshot, without rebuilding a whole-table visibility map.
- Sequential visibility and index-build history use hash TID membership,
  replacing quadratic linear searches. Root selection uses typed scans and a
  hash instead of a dynamic SPI DISTINCT/sort query.
- Writer maps are maintained incrementally per relation/storage generation for
  the transaction. Private writer locks preserve their validity; rollback and
  generation changes discard/rebuild them. Uniqueness checks access only a
  precomputed hash while a user B-tree content lock may be held.
- Version-ID allocation and version insertion/deletion use typed backend APIs,
  ordinary heap WAL, and normal executor index maintenance for the partial TID
  index. No SQL privilege bypass API, new WAL record type, metadata format change
  or new maintenance policy was introduced. Cold ownership refresh and
  administrative paths retain SPI.

## Release results

Compared independent `-O2` builds without assertions/debug, using the same
compiler and 32 MB shared buffers. The baseline is milestone-8 commit
`bad1d8b40a`. Measurements cover all combinations of 1/10/100 total branches and
1K/100K/1M rows, with one active benchmark client. Two baseline matrices and three
candidate matrices confirm the indexed-read improvement; incomplete attempts
are recorded as timeouts rather than fabricated medians.

Representative final measurements at **one million rows, 100 branches**:

| Operation | Milestone 8 | Milestone 9 |
| --- | ---: | ---: |
| Indexed point read | 931.082 ms | 0.127 ms |
| Full count scan | Failed 3000 ms limit | 1040.888 ms |
| First write of an autocommit transaction | 1702.645 ms | 608.560 ms |

Five alternating two-second prepared-protocol pgbench trials per binary and
workload passed the required unaffected-workload gate. Ordinary databases with
no native metadata retained **101.5%, 99.1%, and 100.8%** of baseline median TPS
for scalar queries, heap point reads and heap updates respectively. No measured
control regressed beyond 10%; these small gains are not claimed as general
performance improvements over upstream PostgreSQL.

Both release stack samples succeeded. The sampled baseline phase ran in the
SPI visibility query, Unique/Sort and heap traversal; the candidate used typed
metadata traversal and hashing. With a one-million-row cursor open, backend
allocations rose from **25.0 MiB to 66.2 MiB**. This is held allocated memory,
not peak RSS; map construction temporarily also holds a root-selection hash.

## Validation

Executed against the final assertion/debug build:

- All **243 PostgreSQL core SQL regressions** passed.
- All native **milestone 1–9 suites** passed, including authentication, concurrent
  branch writes/migrations, real autovacuum, pg_xact segment retirement,
  controlled XID epoch transition, lifecycle races and crash recovery.
- The real pinned **Prisma 6.19.0 and Django 5.2.8** application/migration suites
  passed, including branch-bound roles, parallel applications, shadow database,
  reset and reverse migrations.
- New tests passed for remote warm-cache DDL/drop, transaction and savepoint DDL
  rollback, sequential and indexed cursor snapshots across concurrent commits,
  10K native inserts, multiple writer maps, unique errors both conflict actions, and a reproduced mixed-case branch-name lookup under
  a libc en_US metadata collation.
- Assertion/debug and release builds completed without compiler warnings;
  `git diff --check` passed. Make was exercised; Meson registration is included
  but has not been executed.

## Remaining costs and scope

Large scans and the first write to a relation in a transaction still read
O(version-count) metadata and remain slower than ordinary heap operations.
Writer/statement maps use O(visible-row-count) memory with no spill mechanism;
multiple large active relations/connections multiply this allocation. Hashing
trades memory for speed. Heap-control overhead is published for every case.

The bootstrap bulk load is a benchmark-only trusted fixture, not a public import
API. A final refresh exhausted local scratch disk; the failed attempt was excluded.
The final run bounded bootstrap WAL and restored defaults before timing queries. Branch-count measurements use a shared base and small updates, not 100
simultaneous active writers. Results are local macOS ARM64 measurements, with
other host activity uncontrolled. Broader SQL support, concurrent maintenance,
full raw-XID wrap testing and production certification remain outside this
checkpoint.

Milestone 10, distributable runtime packaging and clean-machine platform
validation, is next and has not been started.
