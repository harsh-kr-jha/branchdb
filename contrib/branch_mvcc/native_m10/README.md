# Standalone branchdb developer runtime

Current project overview: [BranchDB](../../../README.md). Download tested archives
from [Releases](https://github.com/harsh-kr-jha/branchdb/releases). After extraction,
run `./branchdb/demo.sh ./branchdb` for a temporary branch-isolation demonstration.

Milestone 10 packages the modified PostgreSQL engine, its shared libraries,
CPython 3.13.9, bootstrap SQL and the `branchdb` CLI in one relocatable archive.
The installed runtime needs neither system PostgreSQL nor Python, Node or an
extension installation. Framework applications still supply their own clients.
This is a developer beta for the SQL and framework subset validated in
[milestone 8](../native_m8/README.md), with the performance limits measured in
[milestone 9](../native_m9/MEASUREMENTS.md).

## Use

Extract the archive for your architecture into a writable location. Run as your
ordinary user. Put global options before the command; every response is JSON,
with `--json` selecting compact output.

```sh
tar -xzf branchdb-developer-beta-macos-arm64.tar.gz
./branchdb/bin/branchdb verify
./branchdb/bin/branchdb --state "$HOME/.branchdb/demo" init
./branchdb/bin/branchdb --state "$HOME/.branchdb/demo" start
./branchdb/bin/branchdb --state "$HOME/.branchdb/demo" branch create feature_a
./branchdb/bin/branchdb --state "$HOME/.branchdb/demo" branch create feature_b
./branchdb/bin/branchdb --state "$HOME/.branchdb/demo" branch list
./branchdb/bin/branchdb --state "$HOME/.branchdb/demo" branch url feature_a --role migration --with-password
./branchdb/bin/branchdb --state "$HOME/.branchdb/demo" branch url feature_a --role app --with-password
./branchdb/bin/branchdb --state "$HOME/.branchdb/demo" status --check
./branchdb/bin/branchdb --state "$HOME/.branchdb/demo" logs --lines 20
./branchdb/bin/branchdb --state "$HOME/.branchdb/demo" branch drop feature_a
./branchdb/bin/branchdb --state "$HOME/.branchdb/demo" stop
```

`BRANCHDB_STATE` supplies the state path when `--state` is omitted. Its default is
`~/.branchdb/default`. `init` selects an available TCP port unless `--port` is
provided, initializes the managed database and credentials, then stops. `start`
starts that instance. Startup reports port conflicts rather than silently moving
an initialized database to a different port. Repeated successful init/start/stop
operations are safe. An interrupted initialization retains its files and requires
a fresh state directory; existing unrelated clusters are never adopted.

Each branch inherits **main at fork time**, including its supported public schema
and unchanged row storage. Different app processes use their respective branch
URLs through ordinary PostgreSQL drivers. Unchanged data is shared within the
same PostgreSQL cluster. Forking does allocate branch catalogs, indexes and
metadata; it does not start another database server. Branches cannot fork from
another child or merge their experimental rows back into main.

Use the migration role for supported schema migrations and the app role for
ordinary CRUD. The runtime provisions future table/sequence grants automatically.
The app role cannot perform DDL, read protected branch metadata, connect to the
maintenance database, or switch branches. Authentication binds both roles to
their branch even when client startup options are omitted. The ordinary URL
command omits the password; `--with-password` explicitly returns a secret, so
capture it into your application's private environment rather than logs.

Prisma `migrate dev` additionally needs a **separate disposable ordinary shadow
database** and its own credentials. Branch migration roles have no CREATEDB
privilege. The acceptance harness creates this database as the private runtime
administrator. Follow the tested setup in milestone 8; arbitrary framework and
schema compatibility is not claimed.

Drop refuses connected sessions by default. `branch drop NAME --force` fences
new connections and disconnects active and idle sessions before deletion. Retired
roles remain NOLOGIN while retained storage descriptors can still reference
their ownership. Deletion does not promise immediate disk compaction.

## Credentials, ownership and recovery

The state directory and data directory require mode 0700 and the invoking UID;
instance metadata, lock and credential files require mode 0600. Credentials live
in `credentials.json`. The administrator is a separate trusted superuser, never
an application identity. Treat the whole state directory as private. State
metadata records the engine binary hash and refuses a different build; there is
no upgrade or import workflow in this milestone.

TCP defaults to loopback with SCRAM for every connection. The runtime sanitizes
inherited PostgreSQL variables, supplies its own private short-lived pgpass file,
and sends verifiers rather than plaintext passwords in CREATE ROLE statements.
Before managing a running server it validates the owner, executable, data path
and authenticated cluster identity. PostgreSQL handles crash recovery using its
normal WAL. Atomic credential writes and serialized control commands allow an
interrupted branch-provisioning operation to be retried with `branch create`.

The archive includes `manifest.json`, per-file hashes, an engine/source hash and
third-party licenses. Adjacent `.sha256` and `.manifest.json` files describe the
archive. `verify` checks installed files without a server. macOS binaries are
ad-hoc signed after relocation; these are not notarized releases. Linux relies
on the host kernel and glibc; consult `build_libc` in the manifest. macOS minimum
version is recorded as `minimum_macos`. Checksums detect corruption; they are not
a signed release-authentication mechanism.

## Build and validate

Build PostgreSQL natively on macOS ARM64 or Linux x86_64. Required packaging tools
are `otool`, `install_name_tool`, `lipo`, `codesign` on macOS, or `ldd` and
`patchelf` on Linux. Supply the corresponding standalone CPython 3.13.9 prefix
(the CI workflow pins its downloader to uv 0.11.7).

```sh
python3 contrib/branch_mvcc/native_m10/build.py \
  --postgres-prefix /absolute/pg-install \
  --python-prefix /absolute/standalone-python \
  --output /absolute/dist
mkdir /absolute/driver
tar -xzf /absolute/dist/branchdb-*.tar.gz -C /absolute/driver
/absolute/driver/branchdb/python/bin/python3 -B contrib/branch_mvcc/native_m10/test.py \
  --archive /absolute/dist/branchdb-developer-beta-linux-x86_64.tar.gz \
  --hide-prefix /absolute/pg-install --hide-prefix /absolute/standalone-python \
  --report /absolute/dist/runtime-acceptance.json
/absolute/client-venv/bin/python -B contrib/branch_mvcc/native_m10/frameworks.py \
  --runtime /absolute/driver/branchdb --node /absolute/node/bin/node \
  --hide-prefix /absolute/pg-install --hide-prefix /absolute/standalone-python \
  --report /absolute/dist/framework-acceptance.json
```

Install pinned application dependencies from `native_m8/requirements.txt` and
`native_m8/prisma/package-lock.json` into a separate client environment first.
The lifecycle test relocates into paths with spaces, hides both build prefixes,
uses a deliberately empty PATH and poisoned PG variables, and exercises SCRAM,
parallel writes, force drop, name reuse, process identity and crash recovery.
Framework acceptance uses real Prisma 6.19.0 migrations/introspection/reset and
Django 5.2.8 migrations/reversals with independent parallel applications.
The new CI workflow also runs core and earlier engine suites before packaging.

## Linux container

Build the image **from the Linux archive**, using a Linux Docker daemon:

```sh
python3 contrib/branch_mvcc/native_m10/image.py \
  --archive /absolute/dist/branchdb-developer-beta-linux-x86_64.tar.gz \
  --tag branchdb:developer-beta
docker compose -f contrib/branch_mvcc/native_m10/compose.yaml up -d
python3 contrib/branch_mvcc/native_m10/container_test.py \
  --tag branchdb:developer-beta --client-runtime /absolute/driver/branchdb \
  --report /absolute/dist/container-acceptance.json
```

The image runs UID/GID 10001, PostgreSQL as PID 1, read-only runtime files and a
health check. Compose supplies a named persistent volume, writable `/tmp`, no
capabilities and loopback port publishing. It enables SCRAM TCP listening inside
the container. Keep the named volume across container replacement. A bind mount
must be writable by UID 10001 with mode 0700; startup never changes ownership of
an existing directory. A different runtime build cannot open the same state.

Maintenance still pauses native transactions. Full raw 32-bit XID wrap testing,
general SQL compatibility, production operations and Headwig integration remain
separate work.
