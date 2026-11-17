# Milestone 5: runnable web-app MVP

Branch Tasks is a small working task app with SQL-file migrations, a JSON HTTP
API and a browser UI. Three separate app processes connect to **main, A and B in
one modified PostgreSQL instance/database**. Pools select a branch at startup;
ordinary SQL queries and writes resolve that branch's schema and data. This adds
an executable app, developer commands and measurements on top of milestone 4;
it adds no new engine SQL features and no separate database per branch.

## Build, test and open the demo

```sh
contrib/branch_mvcc/native_m5/run.sh /tmp/branch-mvcc-m5
python3 -B contrib/branch_mvcc/native_m5/demo.py \
    --bindir /tmp/branch-mvcc-m5/install/bin
```

The runner builds this checkout into its own prefix, runs all 243 PostgreSQL
regressions, the milestone 1–4 suites, and the app acceptance test. Requirements
are PostgreSQL's usual C/Make/Bison/Flex/zlib tools and Python 3. The app uses
libpq from that install through ctypes; no pip/npm packages or ORM are needed.
Use a work path without spaces. This requires the modified server, not stock
PostgreSQL with an extension installed.

The demo prints three `http://127.0.0.1:...` URLs. Open them in separate tabs.
All start with the same task. A has an assignee field; B has a priority field;
main has neither. Edit status, add tasks and delete them independently. The UI
refreshes every five seconds. Ctrl-C stops the apps/server and removes the demo
data. The local fixture uses a trusted superuser and loopback HTTP, with no app
authentication. It is a disposable development demo.

To rerun only the app acceptance/measurements against an installed build:

```sh
python3 -B contrib/branch_mvcc/native_m5/test.py \
    --bindir /tmp/branch-mvcc-m5/install/bin \
    --report /tmp/branch-mvcc-m5/measurements.json
```

## Parallel migration flow

The app's real migration files are in `migrations/`:

- `base/0001_tasks.sql` creates tasks with UUID primary keys and required values.
- `feature_a/0002_collaboration.sql` adds nullable `assignee text` and creates
  `task_notes(id, task_id, body text)`.
- `feature_b/0002_prioritization.sql` adds nullable `priority integer` and creates
  `task_notes(id, task_id, score integer)` with the same SQL table name.

The migration runner owns transaction boundaries and an ordinary branch-local
`app_migrations` table. Each migration's DDL/DML/history commits together; a
failure rolls them all back. Checksums reject editing an applied migration.
Concurrent runners for the same branch serialize with a branch-specific advisory
lock and recheck history; different branches migrate independently. SQL files are
trusted local code and must not contain their own BEGIN/COMMIT/ROLLBACK commands.
Rollback here means transaction rollback/retry of failed migrations, not general
down-migrations: DROP and column removal are still unsupported.

For your own **fresh empty disposable database**, initialize native mode once
with [native_m3/setup.sql](../native_m3/setup.sql), then set `DATABASE_URL` (or use
`--dsn`). For example, with libpq's PGHOST/PGPORT/PGUSER environment configured:

```sh
export DATABASE_URL='postgresql:///branch_demo'
native_bin=/tmp/branch-mvcc-m5/install/bin
app_dir=contrib/branch_mvcc/native_m5
python3 -B "$app_dir/migrate.py" --bindir "$native_bin" \
    --directory "$app_dir/migrations/base"
python3 -B "$app_dir/branches.py" --bindir "$native_bin" create a
python3 -B "$app_dir/branches.py" --bindir "$native_bin" create b
```

In separate terminals, run migrations against the two branches:

```sh
python3 -B "$app_dir/migrate.py" --bindir "$native_bin" --branch a \
    --directory "$app_dir/migrations/base" --directory "$app_dir/migrations/feature_a"
python3 -B "$app_dir/migrate.py" --bindir "$native_bin" --branch b \
    --directory "$app_dir/migrations/base" --directory "$app_dir/migrations/feature_b"
```

Start each app with its own branch and port (one command per terminal):

```sh
python3 -B "$app_dir/app.py" --bindir "$native_bin" --branch main --port 8080
python3 -B "$app_dir/app.py" --bindir "$native_bin" --branch a --port 8081
python3 -B "$app_dir/app.py" --bindir "$native_bin" --branch b --port 8082
```

The optional `--pool-size` defaults to two. Connection pools stay bound to a
branch for their entire lifetime. Stale-write/deadlock SQLSTATEs 40001/40P01 cause
up to two whole-transaction retries. Broken connections are discarded and
reconnected; an ambiguous failed HTTP write is not automatically replayed.

List branches and print a standard libpq connection URL:

```sh
python3 -B "$app_dir/branches.py" --bindir "$native_bin" list
python3 -B "$app_dir/branches.py" --bindir "$native_bin" url a
```

URLs encode `options=-c branch=a`, so stock psql/libpq clients can connect to the
modified server without issuing SET. The helper prints local development URLs
without passwords; authentication/SSL settings can remain in libpq's environment.
The app's explicit `--branch` takes precedence over any branch options in its DSN.

## API

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/info` | Branch, available task/note fields, applied migrations |
| GET / POST | `/api/tasks` | List / create; UUID generated in the app if omitted |
| PATCH / DELETE | `/api/tasks/<uuid>` | Update / delete in this branch |
| GET / POST | `/api/notes` | List / create the branch's body or score note |

Values use libpq's parameterized extended protocol. App-supplied UUIDs, status
and creation time avoid shared sequence/default requirements. There is no FK
constraint; task IDs in notes are checked by app code, and deletion explicitly
removes notes in the same transaction. This is not equivalent to database-level
referential integrity under arbitrary/concurrent SQL writers.

## Acceptance and measured limits

The test starts main's HTTP app, seeds data, forks A/B, and starts both apps
**before** migrating. It forces both migrations to overlap after their DDL using
a test-only 1.2-second SQL sleep, while main serves queries. It verifies isolated
schema/history/CRUD, same-key sibling inserts, same-branch concurrent updates,
failed migration rollback/retry, same-branch migration runners, checksums,
unsupported-feature rejection, CLI commands, and URLs used directly by psql.
All app answers survive VACUUM FREEZE and immediate-stop WAL recovery; the same
running app processes reconnect to their correct branches and keep writing.

It also verifies that forks leave the native version count unchanged and use
the identical physical heap **and primary-key index** for 1,000 unchanged rows.
The report records fork latency and a controlled blocking case, metadata and
database bytes, per-phase WAL bytes, and 60 autocommitted insert/read/update
latencies for native branching versus ordinary heap on the same build.

See [MILESTONE_5.md](../MILESTONE_5.md) and
[sample_measurements.json](sample_measurements.json) for an executed run. These
are local assertion/debug-build measurements, not load-test guarantees. The
prototype's SPI/metadata work adds substantial query/write overhead.

Supported engine migrations remain CREATE TABLE with initial immediate primary/
unique and ordinary NOT NULL constraints, and nullable ADD COLUMN without
defaults. Sequences/identity, default expressions, FK/CHECK, DROP/TRUNCATE,
rewrites, ANALYZE, nested forks and merging remain unsupported. This custom
SQL-file app fixture does not establish Prisma/Django/Alembic compatibility.
Maintenance still pauses native transactions, and full raw-XID wrap testing is
unchanged from [milestone 4](../MILESTONE_4.md). This is a usable local MVP for
the documented subset, not a general production PostgreSQL replacement.
