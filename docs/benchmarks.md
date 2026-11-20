# Measurements

These are engineering checkpoints, not production performance guarantees.
The comparison is against an earlier BranchDB build, not stock PostgreSQL.

The recorded optimized build compared independent `-O2` binaries without
assertions/debug on macOS ARM64, with 32 MB shared buffers. The matrix covered
1/10/100 total branches and 1K/100K/1M rows with one active benchmark client.
Branch-count tests use a shared base and small updates, not 100 active writers.

At one million rows and 100 total branches:

| Operation | Earlier BranchDB checkpoint | Optimized checkpoint |
| --- | ---: | ---: |
| Indexed point read | 931.082 ms | 0.127 ms |
| Full count scan | Exceeded 3000 ms | 1040.888 ms |
| First write of an autocommit transaction | 1702.645 ms | 608.560 ms |

The earlier build is commit `bad1d8b40a`. These results show improvement in
BranchDB metadata handling; they do not remove the scan/write costs.
With a one-million-row cursor open, recorded backend allocations increased from
25.0 MiB to 66.2 MiB. This is held allocation, not peak RSS; map construction
temporarily also allocates a root-selection hash.

Ordinary unbranched controls retained 101.5%, 99.1%, and 100.8% of the earlier
build's median TPS for scalar queries, heap point reads, and heap updates.
These compare two fork builds; they do not establish parity with upstream.

- [Complete matrix, memory costs, caveats](../contrib/branch_mvcc/native_m9/MEASUREMENTS.md)
- [Build and reproduction commands](../contrib/branch_mvcc/native_m9/README.md)
- [Raw baseline, repeat, candidate, and control results](../contrib/branch_mvcc/native_m9/results/)

Linux runtime acceptance used a Debian 12 VM under CPU emulation on ARM64.
It establishes recorded functional acceptance, not Linux performance.
