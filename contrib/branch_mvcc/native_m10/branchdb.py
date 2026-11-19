#!/usr/bin/env python3
"""Private local native-branch runtime. Uses only its bundled Python and psql."""

import argparse
import base64
from collections import deque
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlencode

ROOT = Path(__file__).resolve().parents[1]
PG = ROOT / "pgsql/bin"
FORMAT = 1


class RuntimeError_(Exception):
    pass


def fail(message):
    raise RuntimeError_(message)


def name(value):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,62}", value):
        raise argparse.ArgumentTypeError(
            "branch names need 1–63 ASCII letters, digits, underscores or hyphens, starting with a letter"
        )
    return value


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def environment():
    result = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PG")
        and k
        not in (
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            "PYTHONHOME",
            "PYTHONPATH",
        )
    }
    result["PATH"] = str(PG) + os.pathsep + os.defpath
    result["LC_ALL"] = "C"
    return result


def private(path, directory=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail("missing instance file: " + str(path))
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
        fail(
            "instance path must be owned by this user and must not be a symlink: "
            + str(path)
        )
    correct_type = (
        stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    )
    if not correct_type or stat.S_IMODE(info.st_mode) != (
        0o700 if directory else 0o600
    ):
        fail(
            "instance paths require mode "
            + ("0700" if directory else "0600")
            + ": "
            + str(path)
        )


def atomic(path, value):
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(program, *arguments, input=None, env=None, timeout=60):
    result = subprocess.run(
        [str(program), *map(str, arguments)],
        input=input,
        text=True,
        capture_output=True,
        encoding="utf-8",
        env=env or environment(),
        timeout=timeout,
    )
    if result.returncode:
        # Never echo SQL or connection strings: they may contain credentials.
        message = result.stderr.strip() or result.stdout.strip() or "command failed"
        for line in message.splitlines():
            if "ERROR:" in line or "FATAL:" in line:
                message = line
                break
        fail(program.name + ": " + message[:1200])
    return result.stdout


def manifest():
    return json.loads((ROOT / "manifest.json").read_text())


@contextmanager
def locked(state):
    private(state, True)
    path = state / ".lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if (
            not stat.S_ISREG(os.fstat(fd).st_mode)
            or os.fstat(fd).st_uid != os.geteuid()
            or stat.S_IMODE(os.fstat(fd).st_mode) != 0o600
        ):
            fail("unsafe instance lock permissions")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load(state, incomplete=False):
    private(state, True)
    for file in ("instance.json", "credentials.json"):
        private(state / file)
    config = json.loads((state / "instance.json").read_text())
    if config.get("format") != FORMAT or config.get("uid") != os.geteuid():
        fail("unsupported instance format or process owner")
    if config.get("engine") != manifest()["engine"]:
        fail(
            "this instance belongs to a different engine build; use its original runtime"
        )
    if not incomplete and not config.get("initialized"):
        fail(
            "initialization was interrupted; files were retained; use a fresh state directory"
        )
    private(state / "data", True)
    return config, json.loads((state / "credentials.json").read_text())


def endpoint(config, user, branch="main", password=None):
    authority = quote(user, safe="")
    if password is not None:
        authority += ":" + quote(password, safe="")
    return (
        "postgresql://"
        + authority
        + "@127.0.0.1:"
        + str(config["port"])
        + "/"
        + quote(config["database"], safe="")
        + "?"
        + urlencode({"options": "-c branch=" + branch}, quote_via=quote)
    )


def sql(state, config, credentials, query, branch="main", database=None):
    # Password exists only in a private, short-lived pgpass file, never argv.
    admin = credentials["admin"]
    fd, path = tempfile.mkstemp(prefix=".pgpass-", dir=state)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(
                "127.0.0.1:"
                + str(config["port"])
                + ":*:"
                + admin["user"]
                + ":"
                + admin["password"]
                + "\n"
            )
        env = environment()
        env["PGPASSFILE"] = path
        url = endpoint(config, admin["user"], branch)
        if database:
            url = url.replace(
                "/" + quote(config["database"], safe="") + "?",
                "/" + quote(database, safe="") + "?",
                1,
            )
        return run(
            PG / "psql",
            "-XAtqw",
            "-v",
            "ON_ERROR_STOP=1",
            "-d",
            url,
            input=query,
            env=env,
        )
    finally:
        os.unlink(path)


def query_json(state, config, credentials, query, branch="main"):
    output = sql(
        state,
        config,
        credentials,
        "SELECT COALESCE(json_agg(result),'[]'::json) FROM (" + query + ") result;",
        branch,
    )
    return json.loads(output)


def socket_directory(config):
    path = Path(config["socket"])
    if not path.exists():
        path.mkdir(mode=0o700)
    private(path, True)
    return path


def pid_status(state):
    path = state / "data/postmaster.pid"
    if not path.exists():
        return False
    private(path)
    fields = path.read_text().splitlines()
    if len(fields) < 4 or Path(fields[1]).resolve() != (state / "data").resolve():
        fail("postmaster PID file does not belong to this instance")
    try:
        os.kill(int(fields[0]), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        fail("postmaster belongs to another process owner")
    return True


def verify_process(state):
    fields = (state / "data/postmaster.pid").read_text().splitlines()
    pid = int(fields[0])
    expected = str(PG / "postgres") + " -D " + str(state / "data")
    if sys.platform == "darwin":
        output = run(
            Path("/bin/ps"), "-ww", "-p", str(pid), "-o", "uid=", "-o", "command="
        )
        uid, command = output.strip().split(None, 1)
        valid = int(uid) == os.geteuid() and command == expected
    else:
        process = Path("/proc") / str(pid)
        arguments = (process / "cmdline").read_bytes().split(b"\0")
        valid = (
            process.stat().st_uid == os.geteuid()
            and (process / "exe").resolve() == (PG / "postgres").resolve()
            and len(arguments) >= 3
            and arguments[1] == b"-D"
            and Path(os.fsdecode(arguments[2])).resolve() == (state / "data").resolve()
        )
    if not valid:
        fail("refusing to manage an unrelated process from the PID file")
    return pid


def verify_server(state, config, credentials):
    before = verify_process(state)
    values = json.loads(
        sql(
            state,
            config,
            credentials,
            "SELECT json_agg(value) FROM (SELECT current_setting('data_directory') AS data,current_setting('cluster_name') AS identity) value;",
            database="template1",
        )
    )
    if (
        len(values) != 1
        or Path(values[0]["data"]).resolve() != (state / "data").resolve()
        or values[0]["identity"] != config["identity"]
    ):
        fail("refusing to manage a server with a different instance identity")
    if before != verify_process(state):
        fail("postmaster identity changed during verification")


def start(state, config, credentials):
    socket_directory(config)
    if pid_status(state):
        verify_server(state, config, credentials)
        return {"status": "running", "already_running": True, "port": config["port"]}
    run(
        PG / "pg_ctl",
        "-D",
        state / "data",
        "-l",
        state / "server.log",
        "-w",
        "-t",
        "30",
        "start",
        timeout=40,
    )
    verify_server(state, config, credentials)
    return {"status": "running", "port": config["port"]}


def stop(state, config, credentials):
    if not pid_status(state):
        return {"status": "stopped", "already_stopped": True}
    verify_server(state, config, credentials)
    run(
        PG / "pg_ctl",
        "-D",
        state / "data",
        "-m",
        "fast",
        "-w",
        "-t",
        "30",
        "stop",
        timeout=40,
    )
    return {"status": "stopped"}


def verifier(password):
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 4096)
    stored = hashlib.sha256(hmac.digest(salted, b"Client Key", "sha256")).digest()
    server = hmac.digest(salted, b"Server Key", "sha256")

    def b64(value):
        return base64.b64encode(value).decode()

    return "SCRAM-SHA-256$4096:" + b64(salt) + "$" + b64(stored) + ":" + b64(server)


def new_branch():
    nonce = secrets.token_hex(8)
    return {
        "id": None,
        "migration": {"user": "bdb_m_" + nonce, "password": secrets.token_urlsafe(32)},
        "app": {"user": "bdb_a_" + nonce, "password": secrets.token_urlsafe(32)},
    }


def branch_rows(state, config, credentials):
    return query_json(
        state,
        config,
        credentials,
        "SELECT id,name,cutoff FROM branch_native.branches WHERE name IS NOT NULL ORDER BY id",
    )


def reconcile(state, config, credentials, branch):
    entry = credentials["branches"].get(branch)
    if not entry:
        fail("no managed credentials for this branch")
    rows = query_json(
        state,
        config,
        credentials,
        "SELECT b.id FROM branch_native.branches b WHERE b.name="
        + literal(branch)
        + " AND (SELECT count(*) FROM branch_native.roles r JOIN pg_roles p ON p.oid=r.role WHERE r.branch=b.id AND p.rolname IN ("
        + literal(entry["migration"]["user"])
        + ","
        + literal(entry["app"]["user"])
        + "))=2",
    )
    if len(rows) != 1 or (entry["id"] is not None and entry["id"] != rows[0]["id"]):
        fail(
            "branch credentials are incomplete, retired or no longer match its identity"
        )
    if entry["id"] is None:
        entry["id"] = rows[0]["id"]
        atomic(state / "credentials.json", credentials)
    return entry


def provision(state, config, credentials, branch, fork=True):
    live = next(
        (
            row
            for row in branch_rows(state, config, credentials)
            if row["name"] == branch
        ),
        None,
    )
    if live and fork:
        entry = reconcile(state, config, credentials, branch)
        if entry.get("ready"):
            return {"name": branch, "id": entry["id"], "already_exists": True}
    previous = credentials["branches"].get(branch)
    if fork and not live and previous and previous.get("id") is not None:
        credentials["branches"][branch] = new_branch()
    entry = credentials["branches"].setdefault(branch, new_branch())
    # Persist high-entropy passwords before committing the roles. A crash after
    # COMMIT can recover the pending entry by verifying both role bindings.
    atomic(state / "credentials.json", credentials)
    statements = ["BEGIN;"]
    if fork:
        statements.append("SELECT branch_create(" + literal(branch) + ");")
    for role in ("migration", "app"):
        account = entry[role]
        statements.append(
            "CREATE ROLE "
            + identifier(account["user"])
            + " LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD "
            + literal(verifier(account["password"]))
            + ";"
        )
        statements.append(
            "INSERT INTO branch_native.roles SELECT p.oid,b.id FROM pg_roles p CROSS JOIN branch_native.branches b WHERE p.rolname="
            + literal(account["user"])
            + " AND b.name="
            + literal(branch)
            + ";"
        )
        statements.append(
            "GRANT CONNECT ON DATABASE "
            + identifier(config["database"])
            + " TO "
            + identifier(account["user"])
            + ";"
        )
    statements.append("COMMIT;")
    if not (live and fork):
        sql(state, config, credentials, "\n".join(statements))
    entry = reconcile(state, config, credentials, branch)
    # Scope DDL and ACLs through a fresh administrator connection to the child.
    tables = query_json(
        state,
        config,
        credentials,
        "SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='r'",
        branch,
    )
    sequences = query_json(
        state,
        config,
        credentials,
        "SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='S'",
        branch,
    )
    migration, app = (
        identifier(entry["migration"]["user"]),
        identifier(entry["app"]["user"]),
    )
    acl = ["BEGIN;", "GRANT CREATE ON SCHEMA public TO " + migration + ";"]
    for table in tables:
        acl.append(
            "ALTER TABLE public."
            + identifier(table["relname"])
            + " OWNER TO "
            + migration
            + ";"
        )
    for sequence in sequences:
        acl.append(
            "ALTER SEQUENCE public."
            + identifier(sequence["relname"])
            + " OWNER TO "
            + migration
            + ";"
        )
    acl += [
        "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO "
        + app
        + ";",
        "GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO " + app + ";",
        "ALTER DEFAULT PRIVILEGES FOR ROLE "
        + migration
        + " IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO "
        + app
        + ";",
        "ALTER DEFAULT PRIVILEGES FOR ROLE "
        + migration
        + " IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO "
        + app
        + ";",
        "COMMIT;",
    ]
    sql(state, config, credentials, "\n".join(acl), branch)
    entry["ready"] = True
    atomic(state / "credentials.json", credentials)
    return {"name": branch, "id": entry["id"]}


def initialize(state, args):
    if state.exists():
        private(state, True)
    else:
        try:
            state.mkdir(mode=0o700, parents=True)
        except FileExistsError:
            private(state, True)
    with locked(state):
        # Another initializer may have completed while this process waited.
        if (state / "instance.json").exists():
            config, credentials = load(state)
            return {
                "status": "initialized",
                "already_initialized": True,
                "state": str(state),
                "port": config["port"],
            }
        if any(path.name != ".lock" for path in state.iterdir()):
            fail("init requires an empty private directory")
        identity = secrets.token_hex(16)
        port = args.port
        if not port:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
        config = {
            "format": FORMAT,
            "uid": os.geteuid(),
            "engine": manifest()["engine"],
            "identity": "branchdb-" + identity,
            "database": "branchdb",
            "port": port,
            "listen": args.listen,
            "socket": str(Path("/tmp") / ("branchdb-" + identity)),
            "initialized": False,
        }
        credentials = {
            "admin": {"user": "branchdb_admin", "password": secrets.token_urlsafe(32)},
            "branches": {},
        }
        atomic(state / "credentials.json", credentials)
        atomic(state / "instance.json", config)
        fd, password_file = tempfile.mkstemp(prefix=".init-password-", dir=state)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(credentials["admin"]["password"] + "\n")
            run(
                PG / "initdb",
                "-D",
                state / "data",
                "-U",
                credentials["admin"]["user"],
                "--pwfile",
                password_file,
                "--auth-local=scram-sha-256",
                "--auth-host=scram-sha-256",
                "--no-locale",
                "-E",
                "UTF8",
                "--no-instructions",
            )
        finally:
            os.unlink(password_file)
        socket_directory(config)
        settings = {
            "listen_addresses": config["listen"],
            "port": port,
            "unix_socket_directories": config["socket"],
            "unix_socket_permissions": "0700",
            "password_encryption": "scram-sha-256",
            "cluster_name": config["identity"],
            "shared_buffers": "32MB",
            "max_connections": 32,
            "max_wal_size": "128MB",
            "min_wal_size": "32MB",
            "log_min_error_statement": "panic",
            "logging_collector": "on",
            "log_directory": "..",
            "log_filename": "server.log",
            "log_rotation_age": "0",
            "log_rotation_size": "0",
        }
        with (state / "data/postgresql.conf").open("a") as stream:
            for key, value in settings.items():
                stream.write(
                    key
                    + " = "
                    + (str(value) if isinstance(value, int) else literal(value))
                    + "\n"
                )
        (state / "data/pg_hba.conf").write_text(
            "local all all scram-sha-256\nhost all all 127.0.0.1/32 scram-sha-256\n"
            + (
                "host all all 0.0.0.0/0 scram-sha-256\n"
                if args.listen == "0.0.0.0"
                else ""
            )
        )
        try:
            start(state, config, credentials)
            sql(
                state,
                config,
                credentials,
                "CREATE DATABASE "
                + identifier(config["database"])
                + "; REVOKE ALL ON DATABASE postgres,template1 FROM PUBLIC;",
                database="postgres",
            )
            # psql includes use the bundled setup tree, with a clean environment.
            setup = (
                str(ROOT / "share/native_m8/setup.sql")
                .replace("\\", "\\\\")
                .replace("'", "\\'")
            )
            sql(state, config, credentials, "\\ir '" + setup + "'\n")
            sql(
                state,
                config,
                credentials,
                "REVOKE ALL ON DATABASE "
                + identifier(config["database"])
                + " FROM PUBLIC; REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
            )
            provision(state, config, credentials, "main", fork=False)
            config["initialized"] = True
            atomic(state / "instance.json", config)
        finally:
            if pid_status(state):
                stop(state, config, credentials)
        return {"status": "initialized", "state": str(state), "port": port}


def execute(args):
    if os.geteuid() == 0:
        fail("branchdb must run as a non-root user")
    os.umask(0o077)
    if args.command == "version":
        info = manifest()
        return {
            key: info[key]
            for key in (
                "label",
                "target",
                "postgres",
                "python",
                "engine",
                "metadata_format",
            )
        }
    if args.command == "verify":
        data = manifest()
        for relative, record in data["files"].items():
            path = ROOT / relative
            if record.get("symlink") is not None:
                if not path.is_symlink() or os.readlink(path) != record["symlink"]:
                    fail("checksum mismatch: " + relative)
            elif (
                path.is_symlink()
                or not path.is_file()
                or stat.S_IMODE(path.stat().st_mode) != int(record["mode"], 8)
                or path.stat().st_mode & 0o022
                or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]
            ):
                fail("checksum mismatch: " + relative)
        return {"verified": True, "engine": data["engine"]}
    state = Path(args.state).expanduser().absolute()
    if state.is_symlink():
        fail("state directory must not be a symlink")
    state = state.resolve()
    if args.command == "init":
        return initialize(state, args)
    with locked(state):
        config, credentials = load(state)
        if args.command == "start":
            return start(state, config, credentials)
        if args.command == "stop":
            return stop(state, config, credentials)
        if args.command == "status":
            running = pid_status(state)
            if running:
                verify_server(state, config, credentials)
            if args.check and not running:
                fail("instance is stopped")
            return {
                "status": "running" if running else "stopped",
                "state": str(state),
                "port": config["port"],
                "identity": config["identity"],
            }
        if args.command == "logs":
            path = state / "server.log"
            if not path.exists():
                return {"lines": []}
            private(path)
            with path.open(errors="replace") as stream:
                return {"lines": list(deque(stream, maxlen=args.lines))}
        if args.command == "serve":
            if pid_status(state):
                fail("instance is already running")
            socket_directory(config)
            # flock file descriptors are close-on-exec. PostgreSQL becomes PID 1
            # in the container, whose stop signal is SIGINT for fast shutdown.
            os.execve(
                PG / "postgres",
                [str(PG / "postgres"), "-D", str(state / "data")],
                environment(),
            )
        if not pid_status(state):
            fail("instance is stopped; run start first")
        verify_server(state, config, credentials)
        if args.action == "list":
            return {"branches": branch_rows(state, config, credentials)}
        if args.action == "create":
            return provision(state, config, credentials, args.name)
        if args.action == "url":
            entry = reconcile(state, config, credentials, args.name)
            if not entry.get("ready"):
                fail("branch provisioning is incomplete; retry branch create")
            account = entry[args.role]
            return {
                "url": endpoint(
                    config,
                    account["user"],
                    args.name,
                    account["password"] if args.with_password else None,
                ),
                "branch": args.name,
                "role": args.role,
                "credentials_file": str(state / "credentials.json"),
            }
        if args.name == "main":
            fail("cannot drop main")
        entry = reconcile(state, config, credentials, args.name)
        statements = [
            "BEGIN;",
            "SELECT branch_drop("
            + literal(args.name)
            + ","
            + ("true" if args.force else "false")
            + ");",
        ]
        for role in ("migration", "app"):
            statements.append(
                "ALTER ROLE " + identifier(entry[role]["user"]) + " NOLOGIN;"
            )
        statements.append("COMMIT;")
        sql(state, config, credentials, "\n".join(statements))
        del credentials["branches"][args.name]
        atomic(state / "credentials.json", credentials)
        return {"dropped": args.name}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        default=os.environ.get(
            "BRANCHDB_STATE", str(Path.home() / ".branchdb/default")
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="compact machine-readable output"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--port", type=int, default=0)
    init.add_argument("--listen", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    for command in ("start", "stop", "status", "version", "verify", "serve"):
        sub = commands.add_parser(command)
        if command == "status":
            sub.add_argument(
                "--check",
                action="store_true",
                help="fail unless the verified server is running",
            )
    logs = commands.add_parser("logs")
    logs.add_argument("--lines", type=int, default=40)
    branch = commands.add_parser("branch")
    actions = branch.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    for action in ("create", "drop", "url"):
        sub = actions.add_parser(action)
        sub.add_argument("name", type=name)
        if action == "drop":
            sub.add_argument("--force", action="store_true")
        if action == "url":
            sub.add_argument("--role", choices=("migration", "app"), default="app")
            sub.add_argument(
                "--with-password",
                action="store_true",
                help="explicitly include the private password in the returned URL",
            )
    args = parser.parse_args()
    if args.command == "init" and args.port and not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        print(json.dumps(execute(args), indent=None if args.json else 2))
    except (KeyError, TypeError):
        print(json.dumps({"error": "invalid instance metadata"}), file=sys.stderr)
        return 1
    except (RuntimeError_, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
