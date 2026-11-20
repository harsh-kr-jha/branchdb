# BranchDB

**Writable database branches inside PostgreSQL, sharing unchanged row storage.**

BranchDB is an open-source PostgreSQL fork for local development. Create two
branches from the same starting data, run different migrations, and let each app
write independently through a normal PostgreSQL connection URL. Main stays writable.
Branches live in one database and one server; a fork does not copy all its rows
or start another server.

**Developer beta.** Based on PostgreSQL **20devel**, with a tested SQL subset.
Use disposable development data. This modifies the engine; it is not an extension
for an existing PostgreSQL installation.

## Try it

Download an archive and its `.sha256` file from
[Releases](https://github.com/harsh-kr-jha/branchdb/releases).
PostgreSQL, Python, and the CLI are bundled; no system PostgreSQL or Python is needed.

| Platform | Archive |
| --- | --- |
| Apple Silicon macOS | `branchdb-developer-beta-macos-arm64.tar.gz` |
| Linux x86_64 | `branchdb-developer-beta-linux-x86_64.tar.gz` |

From the download directory, on macOS:

```sh
shasum -a 256 -c branchdb-developer-beta-macos-arm64.tar.gz.sha256
tar -xzf branchdb-developer-beta-macos-arm64.tar.gz
./branchdb/bin/branchdb verify
./branchdb/demo.sh ./branchdb
```

On Linux, use the Linux filename and `sha256sum -c` for the checksum check.
See [platform requirements](docs/compatibility.md) before downloading.

The demo creates a table on main, forks `feature_a` and `feature_b`, changes A
and main independently, and checks that B still reads the original row:

```text
main:      changed on main
feature_a: changed on feature_a
feature_b: inherited
```

It uses a temporary instance and removes its own data on exit.

## Use it with your app

```sh
export BRANCHDB_STATE="$HOME/.branchdb/my-project"
./branchdb/bin/branchdb init
./branchdb/bin/branchdb start
# Apply your initial schema and seed data to main before forking.
./branchdb/bin/branchdb branch create feature_a
./branchdb/bin/branchdb branch create feature_b
./branchdb/bin/branchdb branch url feature_a --role migration --with-password
./branchdb/bin/branchdb branch url feature_a --role app --with-password
./branchdb/bin/branchdb branch list
./branchdb/bin/branchdb stop
```

Commands return JSON. Use the migration URL for supported DDL and the app URL for
ordinary reads and writes. Store connection secrets in your app's private environment.
Each branch inherits main's supported schema and data at its creation time;
later changes to main do not flow into existing children.

Pinned Prisma 6.19.0 and Django 5.2.8 fixtures have been exercised. This does not
establish compatibility with every application; Prisma `migrate dev` also needs
a separate ordinary shadow database. See the [runtime guide](contrib/branch_mvcc/native_m10/README.md)
and [framework fixtures](contrib/branch_mvcc/native_m8/README.md).

## How it works and what it costs

Branches share unchanged heap, inherited index, and TOAST storage. Branch-local
version metadata selects rows visible to a connection. Writes create branch
versions rather than changing shared predecessor tuple headers. Forks still
allocate catalog metadata and independent sequence state; new indexes and some
destructive migrations have additional storage costs.

There is no nested branching or branch merging. Transactions use READ COMMITTED.
Large scans and first writes still scan version metadata, and per-connection maps
can consume substantial memory. Maintenance takes coarse locks. This beta has no
upgrade/import workflow and binds each instance to its exact runtime build.

- [Architecture](docs/architecture.md)
- [Supported SQL, platforms, and limits](docs/compatibility.md)
- [Measurements and reproduction](docs/benchmarks.md)
- [Build and test from source](docs/build.md)
- [Contribute](.github/CONTRIBUTING.md) · [Report a bug](https://github.com/harsh-kr-jha/branchdb/issues)
- [Development history](docs/development-history.md)

## License and upstream

BranchDB is distributed under the [PostgreSQL License](LICENSE), with the original
[PostgreSQL copyright notice](COPYRIGHT) preserved. Bundled runtime dependencies
retain their own licenses, included in each archive.

This is an independent project by Harsh, not an official PostgreSQL release.
PostgreSQL's source layout and Git history are retained. For upstream
PostgreSQL documentation, see [postgresql.org](https://www.postgresql.org/docs/devel/).
