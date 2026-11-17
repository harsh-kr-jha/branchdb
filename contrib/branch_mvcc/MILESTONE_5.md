# Milestone 5 — executable web-app MVP and measured costs

The web-app MVP is complete for the documented native SQL subset. Branch Tasks
provides a runnable browser app, JSON API, branch-bound connection pools, a
transactional migration runner, branch create/list/URL commands, and an
end-to-end test with actual independent app/migration processes.

Start with [native_m5/README.md](native_m5/README.md). The one-command demo starts
a disposable modified PostgreSQL server and three app processes, prints their
local browser URLs, and cleans everything up on Ctrl-C. It uses one database;
branches share unchanged user heaps and indexes. No engine behavior is
implemented through app views, triggers or per-branch database copies.

## What passed

- All 243 core PostgreSQL regressions in the fresh assertion/debug build.
- Original native milestone-1/2/3 suites and the complete milestone-4 suite,
  including real autovacuum, actual CLOG retirement and epoch simulation.
- Live A/B migrations overlap after DDL while main continues HTTP queries.
  A adds an assignee, B a priority; same-named note tables have different schemas.
  Existing app pools observe each branch's own schema and migration history.
- Independent CRUD, same UUID inserts across siblings, local uniqueness,
  concurrent same-branch updates and isolated deletes. SQL-looking text remains
  a parameter value. App notes use each branch's divergent table definition.
- Transactional migration DDL/DML rollback, corrected migration retry, serialized
  same-branch runners, checksum enforcement and clear unsupported-feature errors.
- CLI create/list/url and URL startup selection verified directly with psql.
- Identical API/schema/history results after VACUUM FREEZE and immediate-stop
  recovery; already-running app pools reconnect and retain branch identity.
- A real demo smoke test serves all three pages/APIs and cleans up on Ctrl-C.

## Observed costs

An executed local macOS ARM64 assertion/debug-build run is captured in
[sample_measurements.json](native_m5/sample_measurements.json). There are 1,000
unchanged baseline rows. Point-operation measurements use 60 rows and one
persistent libpq connection with autocommit per statement. This is a small local
fixture with autovacuum disabled for reproducible measurement, not a scalability
test. The interactive demo enables autovacuum.

| Measurement | Result |
|---|---:|
| Initial fork A / B | 21.445 / 18.964 ms |
| Versions before / after both initial forks | 1,002 / 1,002 |
| Unchanged baseline heap bytes before / after forks | 688,128 / 688,128 |
| Database bytes added by both initial forks | 65,536 |
| Branch metadata bytes added by both initial forks | 8,192 |
| WAL bytes for both initial forks | 240,296 |
| Main observer samples during held parallel migrations | 18 |
| Main observer median / p95 (two HTTP reads per sample) | 16.754 / 59.292 ms |
| Full native maintenance sweep | 49.741 ms |
| Fork with deliberate 200 ms active-transaction hold | 275.772 ms |

The same physical baseline heap and primary-key index paths are verified across
main/A/B. Database growth also includes PostgreSQL system catalogs and allocation
effects; the metadata measurement only counts branches/objects/versions and
their indexes/TOAST. File allocation is not the logical live row size. Vacuum may
allocate maps and grow files while reclaiming reusable space; end truncation is
disabled, so database bytes need not fall after collection.

| Operation | Ordinary heap median / p95 | Native A median / p95 |
|---|---:|---:|
| INSERT | 0.099 / 0.133 ms | 1.428 / 1.899 ms |
| Point SELECT | 0.072 / 0.100 ms | 1.269 / 1.738 ms |
| UPDATE | 0.111 / 0.201 ms | 1.918 / 2.515 ms |

The native prototype is substantially slower in this fixture. Metadata/SPI
lookups and append-only version bookkeeping are measured costs, not optimized
away or hidden by the app. Benchmark WAL is 18,864 bytes for ordinary heap and
107,592 for native A; inserts/updates are committed individually. The report
separates fork, app/CLI-fork, maintenance and benchmark WAL intervals and does
not attribute ordinary benchmark database creation to native operation costs.
WAL positions describe the whole isolated cluster; these are measured intervals,
not exact per-record accounting.

## Explicit product boundary

This is a working local developer MVP for a specific app's migration suite.
UUIDs and ordinary field values are supplied by the app; no sequence/default/FK
compatibility is claimed. Failed-migration rollback works; general down-migrations
requiring DROP/rewrites do not. This is a custom SQL-file migration runner,
not certification of an external ORM. All milestone-4 limits remain: trusted
superuser, READ COMMITTED, maintenance admission pauses, no branch drop/merge,
and no exhaustive raw-XID wrap or production fault coverage. Further engine
features should be driven by the chosen real application's migrations.
