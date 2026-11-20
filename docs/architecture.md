# Architecture

BranchDB modifies PostgreSQL's engine to keep independently writable branches
inside one database. It is based on PostgreSQL 20devel. Applications use normal
PostgreSQL drivers with an authenticated, branch-bound connection.

## Fork and visibility

A child inherits main's supported public schema and visible rows at fork time.
Main and each child then change independently. Children cannot fork further
children or merge changes into main.

Unchanged heap, inherited index, and TOAST files are shared. Forking duplicates
catalog metadata and creates independent sequence files; it is not zero-cost.
Branch-local version edges describe replacements and deletions. Shared predecessor
tuple headers are not modified to express a child's write. PostgreSQL MVCC still
supplies visibility within the supported READ COMMITTED model.

Metadata lives in ordinary WAL-logged internal relations. Typed backend access
and hash maps resolve version history and selected TIDs. Catalog invalidation
refreshes cached ownership information. Existing WAL and transactional storage
operations provide crash recovery; this patch adds no new WAL record type.

## Schema and lifecycle

Connections resolve the branch's catalog objects. New indexes can be private
while retaining a shared heap. Some destructive schema changes copy a table's
branch-visible rows into a private generation and rebuild indexes, retaining old
storage for siblings. Retained descriptors and cleanup protect shared history.

The runtime provisions separate migration and application roles, bound to their
branch by authentication. App roles have CRUD permissions; migration roles own
supported public objects. A private trusted administrator manages the instance.
Ordinary deletion refuses connected sessions; forced deletion fences and
disconnects them before the branch name can be reused.

## Source map

| Area | Entry points |
| --- | --- |
| Versions and visibility | `src/backend/access/heap/branchheap.c`, `branchmeta.c` |
| Catalogs and schema | `src/backend/access/heap/branchcatalog.c`, `branchschema.c` |
| Lifecycle | `src/backend/access/heap/branchlifecycle.c` |
| Maintenance and permissions | `src/backend/access/heap/branchmaintenance.c`, `branchsecurity.c` |
| Engine API | `src/include/access/branchheap.h`, `branchmeta.h` |
| Runtime and packaging | `contrib/branch_mvcc/native_m10/` |

See [compatibility](compatibility.md), [measurements](benchmarks.md), and
[development history](development-history.md). The original SQL overlay and the
native runtime are distinct implementations, not interchangeable installations.
