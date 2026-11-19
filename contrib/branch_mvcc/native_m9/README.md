# Milestone 9: measured native-engine optimization

The engine now avoids repeated ownership reloads, SQL planning in its row
visibility/write paths, and linear searches through selected tuple IDs.
[MEASUREMENTS.md](MEASUREMENTS.md) publishes the full release comparison,
heap-control overhead, regression gate and memory costs. Raw runs, configuration
and binary hashes are in [results/](results/).

## Implementation

- Ownership and retirement maps persist until shared relcache invalidation or
  transaction/subtransaction rollback makes them stale. Callbacks only mark an
  epoch; rebuilding still happens at the existing safe entry points. A heap
  invalidation hook publishes changes to the protected ownership, branch and
  retirement tables, which are ordinary user heaps and would otherwise produce
  no shared catalog invalidation. Row-version writes do not invalidate ownership.
- `branchmeta.c` provides typed access through the existing branch-name, version
  family/root and physical-TID indexes. Point reads check only the candidate TID
  and its root history, using the statement snapshot. They no longer construct a
  whole-table map. Inserts reserve a sequence ID directly and maintain metadata
  with normal heap/WAL and executor index routines, including the partial TID
  index. Branch-name index keys use the actual metadata column collation, including
  mixed-case names under non-C ordering. There is no SQL-callable privileged
  metadata API.
- Sequential scans select roots into a hash and test physical TIDs using another
  hash. Index builds also filter sibling layouts using hashed TIDs. This removes
  the previous quadratic membership work and SQL DISTINCT/sort step.
- Writer maps are retained per relation/generation for the transaction and
  updated incrementally. Private branch writer locks make them stable against
  other writers. Savepoint/error callbacks discard the maps. A generation or
  canonical-storage change rebuilds them. The B-tree uniqueness callback only
  reads an already prepared hash; it performs no metadata queries under a user
  index's content lock. Normal index reads perform typed lookups after the user
  index has released that lock.

No metadata format or WAL record format changed. Formats 1–3 remain exercised.
Admission, branch authentication, retention, frozen-XID policy and supported SQL
remain as in milestone 8. Administrative fork/DDL/maintenance queries still use
SPI; cold ownership refresh also retains SPI. Per-tuple metadata planning is
removed from the hot read/write paths.

## Measured scope and remaining cost

The matrix uses one shared synthetic base, 1/10/100 total branches including main,
1K/100K/1M rows, persistent libpq connections and one active benchmark client.
Branch-count measurements are not a 100-active-writer throughput claim. Two
baseline matrices and three candidate matrices were run. Five measured queries
follow one warmup, with a 3-second per-query limit. Interrupted attempts are
reported as timeouts, never as completed medians.

For one million rows and 100 branches, the final release run measured roughly
931 ms → 0.127 ms for an indexed lookup and 1703 ms → 609 ms for the first write
of an autocommit transaction. The full scan completed in 1041 ms; the old scan
failed the 3-second limit. Improvements repeated across the matrix.

This remains substantially slower than an ordinary heap for large scans and
first writes. Scans and the first write to a relation in a transaction still
load O(version-count) metadata. Maps are transaction/statement scoped, not
persistent data duplication. A one-million-row cursor held approximately 66 MiB
of backend allocations versus 25 MiB before; constructing the map temporarily
also holds a root-selection hash. Multiple concurrent large scans or writer
relations multiply that memory. There is no spill-to-disk map implementation.

Five alternating two-second pgbench trials on ordinary databases without native
metadata passed the 10% regression gate for scalar queries, indexed reads and
heap updates. Observed median TPS ratios were 1.015, 0.991 and 1.008; these are
local measurements, not a claim of general PostgreSQL performance parity.

## Reproduce release builds

Freeze the milestone-8 source before modifying it. The recorded baseline is
commit `bad1d8b40a`; full identity is in `results/manifest.json`. Use independent
source/build/install directories and the same compiler and flags:

```sh
/path/to/source/configure --prefix=/tmp/branch-release-install \
  --without-icu --without-readline CFLAGS='-O2'
make -j8
make install
```

Run configure from a separate, already-created build directory. These release
binaries have assertions and debug disabled. The original assertion/debug build
is used separately for correctness acceptance.

With the pinned milestone-8 dependencies installed, run the same matrix against
both installed binaries, sequentially while other test/build workloads are idle:

```sh
python3 -B contrib/branch_mvcc/native_m9/bench.py \
  --bindir /tmp/baseline-install/bin --output /tmp/baseline.json
python3 -B contrib/branch_mvcc/native_m9/bench.py \
  --bindir /tmp/candidate-install/bin --seed-wal-mb 128 --output /tmp/optimized-final.json
python3 -B contrib/branch_mvcc/native_m9/control.py \
  --baseline /tmp/baseline-install/bin --candidate /tmp/candidate-install/bin \
  --output /tmp/control.json
```

The harness creates disposable clusters with Unix sockets, no TCP listener,
32 MB shared buffers and autovacuum off. Its trusted bootstrap loads normal heap
rows before installing native metadata, attaches matching version/TID records,
and then changes the benchmark table's access method. This is a **benchmark-only
seed shortcut**, not an application migration or supported import API. It avoids
spending hours on the old quadratic INSERT path. Native multi-row insertion is
validated separately by the acceptance suite.

A final artifact refresh exhausted local scratch disk. The diagnostic is kept in
`results/resource-refresh.log`; incomplete attempts are excluded. The final
matrix and profile use `--seed-wal-mb 128` to bound WAL during fixture loading,
then restore the default WAL/checkpoint settings and issue CHECKPOINT before
measurements. Recorded runtime settings remain 1 GB max WAL, 80 MB min WAL,
checkpoint completion target 0.9 and 32 MB shared buffers. Earlier optimized
iterations also appear in the raw results; the manifest distinguishes them from
the final binary.

`profile.py --bindir ... --rows 1000000 --output /tmp/profile.json` records
backend memory with a cursor held open and, on macOS, a one-second `sample` stack
trace. Both recorded samples succeeded. The baseline sample spent its observed
phase in SPI/Unique/Sort visibility preparation; the candidate used typed indexed
metadata traversal and root hashing. Samples cover a scan phase, not every
possible workload. Builds and measurements were run on macOS ARM64; Linux and
Meson runtime certification remain for milestone 10.

To check the saved performance gates and regenerate the comparison:

```sh
python3 contrib/branch_mvcc/native_m9/report.py \
  contrib/branch_mvcc/native_m9/results \
  --output contrib/branch_mvcc/native_m9/MEASUREMENTS.md
```

## Correctness acceptance

Use an assertion/debug build of this checkout. All nine native suites and the
pinned Prisma/Django workflows can be run with the milestone-8 virtualenv:

```sh
/tmp/branch-mvcc-m8-venv/bin/python -B contrib/branch_mvcc/native_m9/verify.py \
  --bindir /path/to/debug/install/bin --logs /tmp/branch-m9-acceptance
make -C /path/to/debug/build/src/test/regress check
```

Milestone 9's new suite explicitly exercises warm-cache remote DDL/retirement,
transaction and savepoint aborts, sequential and indexed cursor snapshots during
concurrent commits, 10K native inserts, multiple writer maps, unique violations,
both ON CONFLICT actions, and typed branch-name lookup with a non-C libc
collation. Earlier suites include authentication, concurrent
branches, migration isolation, GC, autovacuum, actual pg_xact retirement,
controlled XID epoch transition, lifecycle races and crash recovery. Full raw
32-bit XID-cycle validation and concurrent maintenance remain later work.
