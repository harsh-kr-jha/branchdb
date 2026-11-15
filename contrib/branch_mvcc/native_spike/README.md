# Milestone 0 backend representation proof

Run `./contrib/branch_mvcc/native_spike/run_m0.sh /tmp/branch-mvcc-m0` from
any directory. Needs the usual PostgreSQL C compiler, make, bison, flex and zlib
build dependencies; ICU and readline are disabled for this debug build.
The script builds this checkout with assertions, installs it under the supplied
work directory, runs core `make check`, builds the C module against that install,
and runs its test in a separate disposable cluster with TCP disabled.
It stops the server and preserves logs and cluster files for inspection.
Use a work path without spaces (the pg_ctl server option string assumes this).
The chosen work directory must be dedicated to this checkout/configuration.

The module executes inside the freshly built PostgreSQL backend. It constructs
an actual HeapTuple, verifies its bytes remain unchanged, and resolves synthetic
branch-local successor edges using PostgreSQL's XidInMVCCSnapshot function.
Tests cover sibling successors, main, ancestor fork boundaries (including an
XID that was active at fork but committed later), local snapshot membership,
abort/in-progress, own command IDs, an aborted child transaction input and
branch-local tombstones.

This is a representation/unit proof. Commit status, own-XID membership, branch
metadata and edges are synthetic inputs. The module does not implement SQL
branch selection, actual concurrent transactions, subtransaction lookup, indexes,
WAL, catalog branching or durable retention. Passing it does not establish those
properties. See ../MILESTONE_0.md for the design and evidence.
