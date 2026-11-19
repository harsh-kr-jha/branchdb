# Release measurements

Times are milliseconds; one warmup followed by five measured queries per case. Timeout entries are failed attempts, not completed medians.

| Rows | Branches | Old point | New point | Old scan | New scan | Old first write | New first write |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 1 | 1.597 | 0.049 | 2.657 | 0.321 | 2.254 | 0.299 |
| 1,000 | 10 | 1.632 | 0.072 | 2.690 | 0.315 | 2.418 | 0.325 |
| 1,000 | 100 | 2.093 | 0.088 | 3.166 | 0.320 | 2.927 | 0.363 |
| 100,000 | 1 | 179.341 | 0.066 | timeout (3000 ms) | 37.584 | 152.044 | 28.745 |
| 100,000 | 10 | 82.560 | 0.111 | timeout (3000 ms) | 38.311 | 136.852 | 33.174 |
| 100,000 | 100 | 68.655 | 0.096 | timeout (3000 ms) | 38.209 | 147.454 | 28.585 |
| 1,000,000 | 1 | 885.704 | 0.064 | timeout (3000 ms) | 1129.204 | 1764.399 | 607.800 |
| 1,000,000 | 10 | 846.303 | 0.106 | timeout (3000 ms) | 1107.132 | 1661.920 | 603.552 |
| 1,000,000 | 100 | 931.082 | 0.127 | timeout (3000 ms) | 1040.888 | 1702.645 | 608.560 |

## Heap-control overhead

These controls are ordinary heap tables in the same branch-enabled instance, using the same client and dataset.

| Rows | Branches | New point / heap point | New scan / heap scan |
| ---: | ---: | ---: | ---: |
| 1,000 | 1 | 1.09× | 4.30× |
| 1,000 | 10 | 1.74× | 4.54× |
| 1,000 | 100 | 2.12× | 4.37× |
| 100,000 | 1 | 1.46× | 11.72× |
| 100,000 | 10 | 2.62× | 12.01× |
| 100,000 | 100 | 2.30× | 12.05× |
| 1,000,000 | 1 | 1.23× | 68.14× |
| 1,000,000 | 10 | 2.61× | 61.95× |
| 1,000,000 | 100 | 3.25× | 58.20× |

## Unchanged-workload gate

Separate databases contain no native metadata. Five alternating two-second pgbench runs per binary and workload; prepared protocol, one client. Each median must retain at least 90% of baseline TPS.

| Workload | Baseline TPS | Candidate TPS | Ratio |
| --- | ---: | ---: | ---: |
| scalar | 53968 | 54792 | 1.015 |
| heap_point | 48142 | 47724 | 0.991 |
| heap_update | 19562 | 19719 | 1.008 |

## Memory and sampling

A one-million-row cursor held 25.0 MiB of backend memory before and 66.2 MiB afterward, as reported by pg_backend_memory_contexts. This is allocated memory with the cursor open, not peak RSS. Hash-map construction temporarily also retains a root-selection map.

macOS sample profiles were captured for both release binaries. See the profile JSON and stack artifacts for the measured scan phase.

All measured gates passed. Absolute results are specific to this machine; the full scan and the first write still perform O(table versions) work.
