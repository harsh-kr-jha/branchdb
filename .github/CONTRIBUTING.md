# Contributing to BranchDB

BranchDB is an independent PostgreSQL fork. Report BranchDB bugs and propose
changes through [this repository](https://github.com/harsh-kr-jha/branchdb/issues).
For upstream PostgreSQL development, see <https://www.postgresql.org/developer/>.

Read the [architecture](../docs/architecture.md), [compatibility](../docs/compatibility.md),
and [build instructions](../docs/build.md) first. Include your runtime version,
platform, minimal reproduction, expected/actual behavior, and relevant redacted
logs in bug reports. Do not include connection passwords or private data.

Branch from `main`, keep changes focused, and open a pull request back to `main`.
For changes to visibility, retention, vacuum, locking, or recovery, explain the
invariant being preserved and add regression coverage for the failure mode.
Run the relevant native suites; core engine changes also require PostgreSQL's
regressions. Runtime changes require archive lifecycle acceptance. Framework
compatibility claims require the real pinned fixture tests.

Keep the PostgreSQL source layout and original copyright notices. New code and
contributions use the PostgreSQL License in [LICENSE](../LICENSE).
`upstream-postgres` preserves the original baseline; the `upstream` remote points
to official PostgreSQL. Milestone records remain historical evidence; update the
current README and docs when public behavior changes.
