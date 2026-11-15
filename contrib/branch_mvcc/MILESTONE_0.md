# Milestone 0: design decision and native representation proof

## Scope and decision

Use a branch-local **version-edge side structure** for the first native patch.
Keep shared predecessor tuple headers unchanged when a child updates/deletes.
A visible edge identifies a successor version or a tombstone; resolving a logical
row follows only edges visible to the connection's branch/ancestry snapshots.
This avoids a tuple-header format change while validating the semantics. It does
not avoid modifications to heap reads/writes, indexes, locks or vacuum.

No native SQL branch feature has been implemented at this milestone. The C test
runs in the built backend but uses synthetic side metadata and transaction status.
It is an executable specification of the representation, not a concurrency,
recovery, subtransaction or storage integration test.

## Version identity and metadata

Proposed identity: (logical relation ID, storage generation ID, monotonic version
ID). Allocate IDs globally for the prototype; gaps are harmless. Persist the
mapping to physical tuples and retain referenced tuples so their slots cannot
be recycled. Branch edges refer to version IDs, never naked ctid values. Logical
rows have a stable root version ID; updates create new version IDs. Storage
rewrites must be rejected until generation remapping is implemented. TOAST data
referenced by retained tuples must also remain reachable.

Each edge records branch ID, predecessor and successor version IDs (or deletion),
creator full XID and command identity. Native implementation must resolve child
XIDs to top-level transaction ownership, reject aborted subtransactions and
preserve combo-CID behavior. The test accepts status/own-XID inputs explicitly;
it does not perform those lookups. Ordinary heap xmin/xmax still governs the
physical side-record transaction lifetime, but cannot be the only permanent
representation of historical branch eligibility after freezing.

Initially store side records in ordinary WAL-logged internal relations. Introduce
a narrowly scoped internal metadata access path using ordinary current catalog
visibility, bypassing application ancestry filtering. Bootstrap, shared catalogs
and branch metadata must not recursively ask themselves for branch eligibility.
A per-backend branch descriptor resolves ancestry once and is invalidated by
catalog updates. Do not expose arbitrary user queries to this bypass.

Before the retention milestone, old fork horizons must be retained and dangerous
maintenance rejected on experimental relations. This is temporary and cannot
support indefinite forks or constitute the final persistence guarantee. Durable
commit/branch eligibility metadata must replace that dependence before allowing
freeze/status truncation to cross retained history.

## Visibility truth table

Assume connection branch A, fork boundary F and current statement snapshot S.
A matching visible tombstone hides the predecessor just like a replacement does.

| Version/edge origin | Condition | Visible in A? |
|---|---|---|
| A | Committed and visible in S | Yes |
| A | Committed after S, or active in S | No |
| A, own transaction | Earlier command and not aborted | Yes |
| A, own transaction | Current/later command under normal scan visibility | No |
| A | Aborted subtransaction or transaction | No |
| A | Another transaction still uncommitted | No |
| Ancestor | Committed and visible at F | Yes |
| Ancestor | Created after F | No |
| Ancestor | Active at F, committed afterward | No |
| Sibling | Any transaction state | No |

Main uses S for its own edges. The first fork protocol drains writers, making
ancestor boundaries simpler, but the representation test still verifies the
active-at-fork case. Dirty/Self/Update snapshots require separate rules for lock
waiting and uniqueness; do not reuse normal read visibility blindly.

The test builds bounded synthetic SnapshotData values and calls PostgreSQL's
XidInMVCCSnapshot for membership. It creates an actual HeapTuple and compares
all its bytes before and after resolving sibling successors and a tombstone.
Its edge ordering is deliberately a tiny, topologically ordered test fixture;
production lookup must handle arbitrary storage order and enforce one applicable
successor chain per branch, including inherited successors and later parent
changes.

## Connection and fork protocol proposal

Startup parameter `branch` (proposed PGC_BACKEND GUC), default main. Resolve and
authorize it after database selection; reject unknown/inaccessible branches.
Disallow SET branch for the connection lifetime initially. Background/parallel
workers need an explicit propagated descriptor or must reject branch execution.
This is a milestone-1 API proposal, not an available command.

Introduce a database-local mutation admission barrier. Every transaction capable
of mutating branchable state acquires shared admission before establishing its
write statement snapshot and holds it to transaction end. Fork takes exclusive
admission, drains existing writers/DDL, rejects prepared transactions initially,
and creates branch descriptors and the fixed boundary in one transaction.
Main resumes writing after publication. A transaction that acquired an earlier
read snapshot must not later write across the barrier with an incompatible
snapshot; initially support READ COMMITTED only and reject stronger levels for
branchable mutations. Audit triggers, COPY, maintenance, catalog writers and
background paths for admission coverage; an executor-only check is insufficient.

Lock order: admission barrier, branch lifecycle reference, logical relation,
branch/root-row locks in deterministic order where feasible, then ordinary heap
and index locks. Ordinary multirow operations can deadlock and use PostgreSQL's
normal deadlock detection/retry. Never wait for branch/key locks while holding
buffer content locks. Fork cannot acquire its exclusive barrier while holding a
conflicting shared admission grant; forbid that upgrade path initially.

Version data and its edge are inserted in one transaction using regular logged
relations. Abort makes both invisible. Branch creation publishes parent, boundary
and ID atomically. The subsequent native patch must prove statement error cleanup,
subtransaction abort, WAL redo and commit publication with crash injection;
ordinary relation WAL alone does not prove all cross-record invariants.

## Catalog implications discovered in source

- heapam.c writes predecessor xmax/cmax and update links. Native branch mutations
  must bypass those global predecessor changes while retaining local lock rules.
- heapam_visibility.c has MVCC, Self, Dirty, Update and vacuum paths. Snapshot
  filtering in normal SELECT alone misses uniqueness and mutation eligibility.
- nbtinsert.c _bt_check_unique consults dirty visibility. Same-branch inherited
  keys must conflict; sibling keys must not. A (branch,key) index alone misses
  ancestor conflicts.
- access/index/genam.c systable_beginscan obtains GetCatalogSnapshot. Catalog
  reads are not automatically governed by an application's statement snapshot.
- catcache.c, relcache.c and inval.c cache object identities and metadata; branch
  context must agree with cache construction, negative entries and invalidation.
- catalog/storage.c RelationDropStorage and relcache.c
  RelationSetNewRelfilenumber schedule storage lifetime changes. Branch-local
  DROP/ALTER cannot unlink files another branch still needs.
- pruneheap.c/vacuumlazy.c/visibilitymap.c can prune or bypass heap visibility.
  Unsupported fast paths must be disabled until branch-aware invariants exist.

## Alternatives

Adding a branch field and changing HeapTupleSatisfiesMVCC alone leaves the one
xmax/successor problem unresolved. Mapping transaction IDs to branches alone loses
required information on freeze/truncation. Permanent xmin pins cannot support
long-lived forks safely. Copying databases does not meet the storage-sharing
requirement. Immutable relation layers are an alternative, but this investigation
will first test native MVCC version edges as requested.

## Reproduction and evidence

Run native_spike/run_m0.sh with a dedicated temporary work directory. It records
configure/build/install/core regression/module-test logs, installs assertion-
enabled server binaries independently of system PostgreSQL, and stops its test
cluster. ICU and readline are disabled to minimize unrelated build dependencies.

Results are recorded below after execution. Milestone 1 requires the user's next
go-ahead and begins native transactional branch integration, not schema migrations
yet. Migration isolation remains the acceptance target for milestone 3.

### Executed results

- Source revision: 77bba5454f96e626651449780e1614892557c1bc (20devel).
- Platform: aarch64 Apple Darwin; Apple clang 16.0.0; GNU make.
- Configure: --prefix=/tmp/branch-mvcc-m0/install --enable-cassert
  --enable-debug --without-icu --without-readline.
- Generated pg_config.h confirms USE_ASSERT_CHECKING=1.
- Native build and installation: passed.
- Core `make check`: all 243 regression tests passed. This is the baseline
  regression suite, not check-world or a branch integration suite.
- Backend C representation module: all 18 explicit checks passed.
- Test cluster stopped after execution. System PostgreSQL was not modified.
- Logs: /tmp/branch-mvcc-m0/{configure,build,install,check,spike-build,spike-test}.log.

Gate 0 is complete as a representation/design milestone. No core visibility
patch has been applied. Native transactional row branching is milestone 1 and
awaits the user's go-ahead. Catalog migration support and retention correctness
are later gates; the passing unit proof must not be interpreted as evidence that
those features already work.
