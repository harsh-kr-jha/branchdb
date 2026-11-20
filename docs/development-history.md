# Development history

Milestone documents are retained as engineering history. They explain design
decisions, record validation, and preserve reproduction commands. Each describes
its checkpoint, not necessarily today's feature set. Git history is preserved;
old commits do not need to be erased.

For current use, start with the [project README](../README.md),
[compatibility](compatibility.md), and [runtime guide](../contrib/branch_mvcc/native_m10/README.md).

| Checkpoint | Record |
| --- | --- |
| Representation proof | [0](../contrib/branch_mvcc/MILESTONE_0.md) |
| Initial native implementation | [1](../contrib/branch_mvcc/MILESTONE_1.md) |
| Engine expansion | [2](../contrib/branch_mvcc/MILESTONE_2.md), [3](../contrib/branch_mvcc/MILESTONE_3.md) |
| Retention and maintenance | [4](../contrib/branch_mvcc/MILESTONE_4.md) |
| Runnable task app | [5](../contrib/branch_mvcc/MILESTONE_5.md) |
| Login and migration roles | [6](../contrib/branch_mvcc/MILESTONE_6.md) |
| Lifecycle and deletion | [7](../contrib/branch_mvcc/MILESTONE_7.md) |
| Prisma and Django fixtures | [8](../contrib/branch_mvcc/MILESTONE_8.md) |
| Optimization and measurements | [9](../contrib/branch_mvcc/MILESTONE_9.md) |
| Distributable runtime | [10](../contrib/branch_mvcc/MILESTONE_10.md) |

The [original design investigation](../contrib/branch_mvcc/DESIGN.md) and
[SQL overlay](../contrib/branch_mvcc/README.md#persistent-branch-experiment)
predate the native runtime. Installing the overlay on stock PostgreSQL does not
provide the current native feature. Historical paths remain intact for old links.
