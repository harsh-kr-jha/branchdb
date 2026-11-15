# Native branching: minimum changes and unresolved work

## Conclusion

A predicate in `HeapTupleSatisfiesMVCC` cannot implement writable branches.
A shared predecessor tuple has one `xmax`, one update link (`t_ctid`), and one
set of lock bits. Two siblings updating it need independent successor/deletion
state. Treating a sibling's `xmin` as invisible does not repair the conflicting
physical mutations, index uniqueness, or pruning decisions.

The smallest safe experiment is immutable shared storage plus branch-local
replacement rows/tombstones, implemented here with existing SQL facilities.
It needs **zero core changes**, but deliberately restricts the baseline to one
sealed table. Native branching of an actively writable, arbitrary database is
substantially larger. The recommended next step retains this overlay model and
moves relation routing, reads and writes into PostgreSQL's execution/storage
paths, rather than adding a branch bit to every existing tuple header.

This analysis inspected the local PostgreSQL 20devel sources. The runnable
prototype is separate; no native changes below have been implemented.

## Desired semantics

A branch has a durable ID, parent, and immutable fork boundary. A connection
selects its branch before its first statement; prohibit changes during a
transaction, including prepared-transaction setup. A transaction's ordinary
snapshot still determines visibility of writes in its own branch. Each ancestor
is observed at its applicable fork boundary. Changes committed later in the
parent or siblings never become visible. Reads resolve the newest applicable
row replacement/tombstone along the ancestry path. Own command-ID and
subtransaction behavior must remain PostgreSQL-compatible.

A boundary is not just an XID or wall-clock time: transactions with XIDs below
`xmax` can still be in progress at fork. A snapshot representation needs xmin,
xmax, in-progress top-level/subtransaction membership and commit/abort knowledge.
`pg_export_snapshot` provides neither persistent historical storage nor a way to
import a snapshot after its exporting transaction ends.

Example acceptance scenario for a native prototype: start with `(1,'base')`;
fork A and B; commit `(1,'parent-later')` in main. Both A and B retain `'base'`.
A updates to `'a'`, B deletes; main remains `'parent-later'`. Independently
inserting the same key in A and B succeeds, duplicating it within A fails.
After VACUUM FREEZE, restart, and recovery the three answers must be identical.
The SQL prototype covers the sibling operations but **not parent-later**.

## Two implementation choices

**Immutable layers / copy-on-write overlays (recommended).** Preserve the old
layer at fork and route subsequent parent writes to a fresh overlay as well.
Share immutable heaps and their indexes by reference, with branch-local overlay
indexes. A stable logical row ID identifies replacements regardless of physical
TID. For arbitrary fork boundaries, seal a consistent group of layers while
coordinating active writers. A naive metadata flip misses in-flight old-layer
writers: block/drain them or route their commit consistently. A database-wide
fork should initially use a short write barrier and schema freeze; explicitly
define whether it waits for all writers rather than pretending a collection of
per-table locks is an instantaneous database snapshot.

This avoids retroactively asking heap visibility to understand a sibling's
mutation of the base row. It still requires native integration for transparent
arbitrary tables, constraints, row locking, planning and lifecycle management.

**Branch-aware shared version heap.** Keep per-version branch ownership plus
branch-local predecessor/successor/tombstone metadata. A single predecessor
`xmax`/`t_ctid` cannot represent sibling changes, so deletion/update state must
move into branch-aware side records or a new version-chain representation.
A side mapping avoids immediate tuple-header format changes but adds lookup
cost, stable-identity and WAL requirements. Storing branch on transactions alone
is insufficient: freezing discards ordinary XIDs, lock-only xmax/MultiXacts need
special treatment, and a predecessor can have multiple successors. This option
has a much larger visibility/pruning/locking audit surface.

## Subsystem changes

| Area | Minimum native work | Source anchors in this checkout |
|---|---|---|
| Catalog/session | WAL-backed branch catalog, layer references, durable fork boundary; transaction-pinned branch context; permissions; relcache and plan invalidation/routing keyed by branch or stable runtime dispatch | `src/include/catalog/`, `src/backend/utils/cache/relcache.c`, `src/backend/utils/misc/guc_tables.c` |
| Snapshot/transactions | Compose local live snapshot with immutable ancestor boundaries; preserve command IDs, own writes, subxacts, abort and prepared transactions; stable branch ownership independent of truncated transaction status | `src/include/utils/snapshot.h`, `src/backend/utils/time/snapmgr.c`, `src/backend/access/transam/xact.c`, `src/backend/storage/ipc/procarray.c` |
| Tuple reads | Overlay-aware sequential, index, bitmap, TID, parallel scans and fetches; branch tombstones shadow ancestor rows even when the replacement no longer matches the predicate | `src/backend/access/heap/heapam_visibility.c`, `src/backend/access/heap/heapam_handler.c`, `src/include/access/tableam.h` |
| Writes/locks | Redirect inherited UPDATE/DELETE into private replacement/tombstone; branch-local logical row locks and EvalPlanQual; stable row identity, immutable-layer protection, speculative insertion, RETURNING | `src/backend/access/heap/heapam.c`, `src/backend/executor/nodeModifyTable.c`, `src/backend/executor/execMain.c` |
| Indexes | Search shared ancestor plus local indexes, suppress shadowed rows, enforce uniqueness against the merged branch view; branch-specific conflicts only; branch-aware predicate locks | `src/backend/access/nbtree/nbtinsert.c` (`_bt_check_unique` uses SnapshotDirty), `src/backend/access/index/indexam.c`, `src/backend/storage/lmgr/predicate.c` |
| VACUUM/pruning | Preserve every branch-reachable version/layer and shadow record; safe HOT traversal; branch-aware visibility-map/index-only semantics; collect unreachable layers transactionally | `src/backend/access/heap/pruneheap.c`, `src/backend/access/heap/vacuumlazy.c`, `src/backend/access/heap/README.HOT`, `src/backend/access/heap/visibilitymap.c` |
| WAL/recovery | Atomically publish fork/layer references, row replacements and tombstones; replay must restore branch metadata before dependent data is exposed; branch drop versus checkpoint/recovery lifetime | `src/backend/access/heap/heapam_xlog.c`, `src/backend/access/transam/xact.c`, `src/backend/access/transam/xlog.c`, `src/include/access/heapam_xlog.h` |

### Visibility and indexing traps

For a shared version-heap design, audit `HeapTupleSatisfiesMVCC`, `Self`, `Dirty`,
`Update`, `VacuumHorizon`, non-vacuumable visibility, batch visibility and
historic/logical-decoding snapshots. Updating only the normal MVCC reader misses
both update eligibility and `_bt_check_unique`'s dirty-snapshot checks.

Index entries refer to physical TIDs, not a logical branch-visible row. An
ancestor key can still conflict with a local insert even if the ancestor index
is shared. Conversely, a key deleted in this branch can be reused. A replacement
that changes an indexed column must shadow the old ancestor entry on both old
and new key probes; filtering only matching local results is incorrect.
The conservative initial implementation disables HOT across layers and
index-only scans on merged branch relations. Disabling index-only scans is
necessary if overlay suppression otherwise requires an unchecked heap lookup.
A single ordinary visibility-map bit cannot certify branch reachability.

Shared global unique indexes with `(branch,key)` alone are insufficient: they
miss inherited keys. The SQL experiment checks the merged view while holding a
branch-wide write lock. A native version should use branch/key conflict locks
and recheck the merged view, or provide equivalent index-AM support. Test
speculative inserts, concurrent delete/reinsert, deferrable constraints and
SERIALIZABLE predicate conflicts before claiming normal index semantics.

### VACUUM and transaction-ID wraparound

A quick shared-heap experiment could pin the oldest fork xmin in
`ComputeXidHorizons`/`GetOldestNonRemovableTransactionId`, but this is not a
production persistence design. It retains unbounded history, interacts with
pruning/HOT and freezing, and cannot preserve ancient 32-bit XID semantics
indefinitely. A raw saved snapshot plus an XID-to-branch table is insufficient
once CLOG/subtransaction state is truncated or insertion XIDs are frozen.

For immutable layers, reclaim physically dead versions only after establishing
which rows constitute the layer's fixed visible contents; freezing those
retained rows is then safe because ancestry is represented by durable layer
metadata rather than their historical xmin. How the live layer becomes that
immutable representation is required implementation work, not a free consequence
of marking a relation read-only. With a shared version heap, persist a separate
long-lived visibility/commit representation before freezing and status
truncation. FullTransactionId avoids epoch ambiguity but does not retain commit
status or prevent pruning. Persistent branch references, not live backend xmin
pins alone, must survive restart and guard retention.

### WAL

The SQL prototype uses only ordinary logged table/index/catalog operations;
branch identity is stored in rows and branch views. No WAL format change is
needed. Crash recovery has been tested for this restricted representation.

An implementation using ordinary catalog/overlay relations can also initially
reuse existing WAL records; it need not add branch fields to every record if
relation/tuple identity determines ownership. Custom side maps, tuple headers,
layer storage or lock/update chains require WAL records (or extensions to
existing records) covering those mutations. Never rely on a replay backend's
session branch. Reference publication/removal must be transactional and recovery
must not collect shared files still referenced by another branch. Physical
backup/replication must preserve all referenced layers. Logical decoding,
publication scope and pg_dump require explicit branch semantics and are deferred
from the first native prototype.

## Suggested next increment

1. Introduce a branchable table AM for fixed-schema heap tables, stable logical
   keys, two sibling branches, no nested forks, schema changes or foreign keys.
   Table AM hooks are useful but not guaranteed sufficient for index routing,
   merged uniqueness and executor row locking; prove those paths explicitly.
2. Add durable branch/layer catalogs and a transaction-pinned selector. Seal a
   consistent baseline under a write barrier; route **main and both children**
   to independent overlays. This supplies the missing writable-parent scenario.
3. Implement scan/fetch shadowing and branch/key locks; disallow unsupported
   operations explicitly. Existing WAL-logged overlay relations minimize new
   recovery machinery. Preserve the SQL implementation as a behavioral oracle.
4. Add forced index/bitmap/parallel plans, concurrent fork/writer tests,
   subtransaction/2PC tests, vacuum/pruning/freeze stress, wraparound simulation,
   and crash injection around fork/drop and overlay publication. Only then
   consider branch-aware heap format changes or optimized shared indexes.

Native database-wide schema/catalog branching, sequences, large objects, FKs,
DDL dependencies, role permissions and merge/conflict policy remain separate
scope. The prototype should not be presented as solving them.

## References

The local source is authoritative for this checkout. Supporting PostgreSQL
upstream documentation:

- [Index uniqueness checks](https://www.postgresql.org/docs/current/index-unique-checks.html)
  describes why uniqueness must consult heap transaction visibility.
- [Routine vacuuming](https://www.postgresql.org/docs/current/routine-vacuuming.html)
  describes dead-version removal, freezing and visibility maps.
- [Snapshot synchronization](https://www.postgresql.org/docs/current/functions-admin.html#FUNCTIONS-SNAPSHOT-SYNCHRONIZATION)
  describes exported snapshot lifetime.
- [Table access method interface](https://www.postgresql.org/docs/current/tableam.html)
  provides the starting extension boundary, not a complete branch API.
