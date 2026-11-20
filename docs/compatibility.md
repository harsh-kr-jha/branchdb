# Compatibility and limits

This is a developer beta of a PostgreSQL 20devel fork. Use disposable development
data. Claims refer to committed fixtures and recorded runs, not arbitrary workloads.

## Platforms

| Runtime | Boundary |
| --- | --- |
| macOS ARM64 | Apple Silicon only. Check `minimum_macos` in the archive manifest. The earlier local package required 15.2 and was exercised on 26.7.1. Binaries are ad-hoc signed, not notarized. |
| Linux x86_64 | Host glibc required; `build_libc` in the manifest records the build requirement. No Linux ARM64 or musl package. |
| Container | Linux x86_64 image built from the Linux archive. Build helper and Compose configuration are included; the release process does not publish a registry image. |

The archive bundles the engine, non-OS libraries, CPython 3.13.9, bootstrap SQL,
CLI, dependency notices, and per-file hashes. Applications still supply their own
framework dependencies and drivers.

## Supported and exercised

- Public permanent tables, built-in column types, audited defaults, independent
  serial/identity sequences, and ordinary immediate B-tree indexes.
- Branch-local CRUD, upsert, immediate unique constraints, CHECK and foreign-key
  validation, deferred foreign-key checks, and cascades in the tested subset.
- Tested column additions, renames, defaults, NOT NULL changes, removals,
  FK-aware DROP/TRUNCATE, and transactional reset. Some operations copy the
  table's visible rows and rebuild indexes.
- Pinned Prisma 6.19.0 and Django 5.2.8 migrations, CRUD, parallel applications,
  and reset/reverse migrations in committed fixtures.
- SCRAM roles, branch deletion/name reuse, and WAL crash recovery in acceptance.

Prisma `migrate dev` needs a separate disposable ordinary shadow database and
credentials. Branch migration roles lack CREATEDB. See the
[framework setup and exact coverage](../contrib/branch_mvcc/native_m8/README.md).

## Current limits

- READ COMMITTED only; no nested branching or merging.
- No general support for type-changing rewrites, enums/custom types, generated
  columns, user triggers, custom schemas, partitioning, MERGE, or NOWAIT/SKIP LOCKED.
- Expression, partial, concurrent, and deferred indexes are outside the subset.
- Large scans and first writes can require O(version-count) metadata work.
  Visibility/writer maps use O(visible-row-count) memory without spilling;
  concurrent large relations and connections multiply this cost.
- Maintenance and destructive storage changes use coarse admission locks.
  Full raw 32-bit XID-cycle validation and production certification remain open.
- Branch deletion does not promise immediate disk compaction.
- State is bound to the exact engine build. There is no upgrade, import, or
  adoption workflow; replacing binaries does not upgrade an existing instance.

See [runtime operations](../contrib/branch_mvcc/native_m10/README.md) and
[measurements](benchmarks.md).
