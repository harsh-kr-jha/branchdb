# Milestone 10 — distributable development runtime

The implementation, commands and reproduction steps are in
[native_m10/README.md](native_m10/README.md). Platform lifecycle, framework and container acceptance passed. The final targeted
credential-recovery regression also passed on both platforms.

## Delivered behavior

`branchdb` initializes and manages a private instance without requiring installed
PostgreSQL or Python. It creates, lists and drops branches, returns distinct
migration/application connection URLs, reports status and logs, and verifies the
installed payload. Results are JSON. PostgreSQL, its non-OS libraries, CPython
3.13.9 and bootstrap SQL travel together in the relocatable archive. Per-file
manifests, engine/source hashes, archive checksums and dependency notices are
included. The Linux container consumes that same native archive.

Each branch remains inside one PostgreSQL cluster and shares inherited unchanged
row storage. Branch catalogs, indexes and changed-row metadata still have their
own costs. Each application connects with standard PostgreSQL drivers and a
branch-bound login; there is no per-branch server or row-copy import in this
milestone. Main remains writable and future children inherit its state at their
own creation time.

The runtime generates private SCRAM credentials and separate non-superuser roles
for migration and app access. Files are restricted to the owner. Only the
migration role owns the branch's supported public objects; automatic future
table and sequence grants keep app CRUD working after migrations. The private
administrator remains a trusted superuser. Interrupted branch provisioning is
retryable, and retired credentials cannot revive when a branch name is reused.

State management uses an exclusive control lock, atomic durable JSON writes and
an engine-build gate. Start, stop and status verify process ownership, executable,
data path and authenticated cluster identity. Successful initialization and
start/stop are idempotent. Unrelated clusters are never adopted; interrupted
initialization retains its files and requires a fresh directory.

The container runs UID/GID 10001 with a persistent named volume, read-only runtime,
writable `/tmp`, health checks, no capabilities and SIGINT fast shutdown.
Credentials and data persist across replacement containers using the same build.

## Engine changes

Only two engine paths change in this checkpoint:

- The trusted administrator may issue ALTER DEFAULT PRIVILEGES in a native
  database, enabling scoped future grants for each unique migration role.
  This adds no application privilege bypass.
- Forced branch deletion finds enrolled sessions through PGPROC under
  ProcArrayLock, including active-proc membership validation. The former virtual
  transaction conflict list omitted idle sessions with no valid VXID, causing
  force deletion to time out. The new lifecycle regression holds an idle app
  connection, verifies ordinary deletion refuses it, and verifies forced deletion
  fences it before reusing the name.

There is no tuple format, WAL record, metadata version, visibility hot-path or
maintenance policy change. The metadata format remains version 3.

## Validation

All 243 core PostgreSQL regressions and native milestone 1–9 suites passed on the
updated macOS release engine, including the pinned earlier Prisma and Django
acceptance. The standalone macOS archive also passed relocation, unavailable
build inputs, hostile inherited PG settings, corrupted payload/mode detection,
concurrent initialization, SCRAM, ACLs, parallel writes, post-fork migrations,
idle-session force deletion, name reuse, PID spoof refusal and crash recovery.
Real Prisma 6.19.0 and Django 5.2.8 workflows passed through packaged SCRAM roles
with the original PostgreSQL and Python prefixes hidden.

Linux lifecycle, Prisma/Django and non-root persistent-container acceptance
also passed. Reports are in [native_m10/results/](native_m10/results/).
The CI workflow repeats core/earlier suites, packaging, dependency-isolated
lifecycle/framework acceptance and the Linux container gate. Remote CI has not
been executed in this session.

## Boundaries

These are developer-beta artifacts. macOS is ARM64; the locally built archive
records minimum macOS 15.2 and was exercised on macOS 26.7.1. Linux is x86_64 with
the build's glibc requirement recorded in its manifest. The Linux acceptance VM
is a real Debian 12 guest under QEMU CPU emulation on the ARM64 host, not a Linux
performance measurement. macOS binaries are ad-hoc signed, not notarized.

Only the tested schema and framework subset is supported. Prisma migrate dev
still requires an ordinary disposable shadow database. State cannot be adopted
by a different engine build; this checkpoint supplies no upgrade/import API.
Maintenance pauses, full raw 32-bit XID wrap validation, nested branching,
branch merging, broad project compatibility, Headwig integration and production
certification remain separate work.
