#!/usr/bin/env python3
"""Relocated archive, SCRAM/ACL, process identity and lifecycle acceptance."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent / "../native_m5"))
from db import Client, DatabaseError

parser = argparse.ArgumentParser()
parser.add_argument("--archive", required=True, type=Path)
parser.add_argument("--hide-prefix", action="append", default=[], type=Path)
parser.add_argument("--report", type=Path)
args = parser.parse_args()
report = {"checks": []}
with (
    tempfile.TemporaryDirectory(prefix="branchdb-acceptance-", dir="/tmp") as directory,
    ExitStack() as cleanup,
):
    base = Path(directory).resolve()
    destination = base / "moved runtime with spaces"
    destination.mkdir()
    with tarfile.open(args.archive) as archive:
        archive.extractall(destination, filter="data")
    assert not any(path.name.startswith("._") for path in destination.rglob("*"))
    root = destination / "branchdb"
    executable = root / "bin/branchdb"
    state = base / "private instance with spaces"
    clean = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    clean["PATH"] = "/nonexistent"
    clean["PGSERVICE"] = "must-not-be-used"
    clean["PGOPTIONS"] = "-c branch=wrong"
    # Original install/interpreter paths become unavailable during every check.
    for original in args.hide_prefix:
        original = original.resolve()
        hidden = original.with_name(original.name + ".m10-hidden")
        if hidden.exists():
            raise AssertionError("refusing to overwrite " + str(hidden))
        original.rename(hidden)
        cleanup.callback(hidden.rename, original)

    def cli(*arguments, error=False, instance=state):
        result = subprocess.run(
            [str(executable), "--state", str(instance), "--json", *arguments],
            env=clean,
            text=True,
            capture_output=True,
            timeout=60,
        )
        if error:
            assert result.returncode, (arguments, result.stdout)
            return json.loads(result.stderr)
        assert result.returncode == 0, (arguments, result.stderr)
        return json.loads(result.stdout)

    def url(branch="main", role="app"):
        return cli("branch", "url", branch, "--role", role, "--with-password")["url"]

    def connect(branch="main", role="app", dsn=None):
        return Client(root / "pgsql/bin", dsn or url(branch, role), branch).connect()

    def denied(query, branch="main"):
        with connect(branch) as db:
            try:
                db.execute(query)
            except DatabaseError:
                return
            raise AssertionError("permission should reject " + query)

    assert cli("verify")["verified"]
    payload = root / "libexec/branchdb.py"
    original_payload = payload.read_bytes()
    payload.write_bytes(original_payload + b"\n# integrity check fixture\n")
    assert "checksum mismatch" in cli("verify", error=True)["error"]
    payload.write_bytes(original_payload)
    original_mode = payload.stat().st_mode & 0o777
    payload.chmod(original_mode | 0o020)
    assert "checksum mismatch" in cli("verify", error=True)["error"]
    payload.chmod(original_mode)
    assert cli("verify")["verified"]
    report["checks"].append("manifest detects modified payloads and unsafe file modes")
    assert cli("version")["python"] == "3.13.9"
    with ThreadPoolExecutor(max_workers=2) as workers:
        initialized = list(workers.map(lambda _: cli("init"), range(2)))
    first = initialized[0]
    assert first["status"] == "initialized"
    assert cli("init")["already_initialized"]
    try:
        assert cli("start")["status"] == "running"
        assert cli("start")["already_running"]
        assert cli("status")["status"] == "running"
        report["checks"].append(
            "relocation, clean PATH, missing build prefixes and idempotent lifecycle"
        )
        credentials = json.loads((state / "credentials.json").read_text())
        passwords = [
            credentials["admin"]["password"],
            *[
                account["password"]
                for account in credentials["branches"]["main"].values()
                if isinstance(account, dict)
            ],
        ]
        for result in (
            first,
            cli("status"),
            cli("branch", "list"),
            cli("branch", "url", "main"),
        ):
            assert all(secret not in json.dumps(result) for secret in passwords)
        assert (state.stat().st_mode & 0o777) == 0o700
        assert ((state / "credentials.json").stat().st_mode & 0o777) == 0o600
        assert "scram-sha-256" in (state / "data/pg_hba.conf").read_text()
        with connect(role="migration") as db:
            db.execute(
                "CREATE TABLE items(id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,body text)"
            )
            db.execute("INSERT INTO items(body) VALUES('inherited')")
        with connect() as db:
            assert db.execute("SELECT body FROM items")[0]["body"] == "inherited"
            assert (
                db.execute("INSERT INTO items(body) VALUES('app') RETURNING id")[0][
                    "id"
                ]
                == "2"
            )
        denied("CREATE TABLE forbidden(id int)")
        denied("SELECT * FROM branch_native.roles")
        denied("SET branch='another'")
        wrong = url().replace("/branchdb?", "/postgres?")
        try:
            connect(dsn=wrong)
        except DatabaseError:
            pass
        else:
            raise AssertionError(
                "application role must not connect to maintenance databases"
            )
        parsed = urlsplit(url())
        bad = urlunsplit(
            parsed._replace(
                netloc=parsed.username
                + ":wrong@"
                + parsed.hostname
                + ":"
                + str(parsed.port)
            )
        )
        try:
            connect(dsn=bad)
        except DatabaseError:
            pass
        else:
            raise AssertionError("SCRAM accepted the wrong password")
        (state / "credentials.json").chmod(0o644)
        assert "0600" in cli("status", error=True)["error"]
        (state / "credentials.json").chmod(0o600)
        alias = base / "alias"
        alias.symlink_to(state, target_is_directory=True)
        assert "symlink" in cli("status", error=True, instance=alias)["error"]
        report["checks"].append(
            "SCRAM, private files, role separation, future-table default grants and immutable branch context"
        )
        cli("branch", "create", "a")
        cli("branch", "create", "b")
        assert cli("branch", "create", "a")["already_exists"]

        def branch_write(branch):
            with connect(branch) as db:
                assert db.execute("SELECT count(*) AS n FROM items")[0]["n"] == "2"
                db.execute("INSERT INTO items VALUES(999,$1)", (branch,))
                db.execute("UPDATE items SET body=$1 WHERE id=1", (branch,))
                assert (
                    db.execute("SELECT body FROM items WHERE id=999")[0]["body"]
                    == branch
                )

        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(branch_write, ("a", "b")))
        with connect() as db:
            assert (
                db.execute("SELECT body FROM items WHERE id=1")[0]["body"]
                == "inherited"
            )
        with connect("a", role="migration") as db:
            db.execute("CREATE TABLE new_after_fork(id int PRIMARY KEY)")
        with connect("a") as db:
            db.execute("INSERT INTO new_after_fork VALUES(1)")
        report["checks"].append(
            "parallel isolated branch writes, cloned identity ownership and post-fork migrations"
        )
        stale_credentials = json.loads((state / "credentials.json").read_text())
        old = url("a")
        held = connect("a")
        try:
            cli("branch", "drop", "a", error=True)
            assert held.execute("SELECT count(*) AS n FROM items")[0]["n"] == "3"
            cli("branch", "drop", "a", "--force")
            assert not held.ok or "a" not in [
                row["name"] for row in cli("branch", "list")["branches"]
            ]
        finally:
            held.close()
        (state / "credentials.json").write_text(json.dumps(stale_credentials))
        cli("branch", "create", "a")
        try:
            connect("a", dsn=old)
        except DatabaseError:
            pass
        else:
            raise AssertionError("retired credentials revived after name reuse")
        with connect("b") as db:
            assert db.execute("SELECT body FROM items WHERE id=999")[0]["body"] == "b"
        report["checks"].append(
            "connected-session refusal, forced deletion and stale-credential fencing"
        )
        # A forged PID file must never signal an unrelated process.
        pidfile = state / "data/postmaster.pid"
        original = pidfile.read_text()
        innocent = subprocess.Popen(["/bin/sleep", "30"])
        try:
            fields = original.splitlines()
            fields[0] = str(innocent.pid)
            pidfile.write_text("\n".join(fields) + "\n")
            assert "unrelated process" in cli("stop", error=True)["error"]
            assert innocent.poll() is None
        finally:
            pidfile.write_text(original)
            innocent.terminate()
            innocent.wait(timeout=5)
        assert cli("status")["status"] == "running"
        pid = int(original.splitlines()[0])
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                result = cli("start")
                break
            except AssertionError:
                time.sleep(0.2)
        else:
            raise AssertionError("crash restart did not complete")
        with connect("b") as db:
            assert db.execute("SELECT body FROM items WHERE id=999")[0]["body"] == "b"
        cli("stop")
        assert cli("stop")["already_stopped"]
        assert cli("status")["status"] == "stopped"
        cli("status", "--check", error=True)
        assert cli("logs")["lines"]
        report["checks"].append(
            "PID spoof protection, crash recovery, restart, stop and logs"
        )
    finally:
        try:
            cli("stop")
        except Exception:
            pass
        config = (
            json.loads((state / "instance.json").read_text())
            if (state / "instance.json").exists()
            else {}
        )
        if config.get("socket"):
            shutil.rmtree(config["socket"], ignore_errors=True)
report["passed"] = True
if args.report:
    args.report.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
