#!/usr/bin/env python3
"""Actual Linux non-root/read-only container and named-volume acceptance."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent / "../native_m5"))
from db import Client

parser = argparse.ArgumentParser()
parser.add_argument("--tag", default="branchdb:developer-beta")
parser.add_argument("--client-runtime", required=True, type=Path)
parser.add_argument("--report", type=Path)
args = parser.parse_args()
nonce = uuid.uuid4().hex[:12]
volume = "branchdb-test-" + nonce
containers = []
report = {"checks": []}


def docker(*arguments, check=True):
    return subprocess.run(
        ["docker", *arguments], text=True, capture_output=True, check=check, timeout=90
    )


def start(name):
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--read-only",
        "--tmpfs",
        "/tmp:mode=1777,size=64m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "-v",
        volume + ":/var/lib/branchdb",
        "-p",
        "127.0.0.1::5432",
        args.tag,
    )
    containers.append(name)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = docker(
            "exec",
            name,
            "/opt/branchdb/bin/branchdb",
            "--json",
            "status",
            "--check",
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise AssertionError(docker("logs", name, check=False).stderr[-6000:])


def control(name, *arguments):
    return json.loads(
        docker("exec", name, "/opt/branchdb/bin/branchdb", "--json", *arguments).stdout
    )


def connect(name, branch="main", role="app"):
    url = control(name, "branch", "url", branch, "--role", role, "--with-password")[
        "url"
    ]
    value = json.loads(docker("inspect", name).stdout)[0]
    port = value["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
    parts = urlsplit(url)
    authority = parts.netloc.rsplit(":", 1)[0] + ":" + port
    return Client(
        args.client_runtime / "pgsql/bin",
        urlunsplit(parts._replace(netloc=authority)),
        branch,
    ).connect()


docker("volume", "create", volume)
try:
    first = "branchdb-" + nonce
    start(first)
    info = json.loads(docker("inspect", first).stdout)[0]
    assert (
        info["Config"]["User"] == "10001:10001" and info["HostConfig"]["ReadonlyRootfs"]
    )
    assert any(
        m.get("Name") == volume and m["Destination"] == "/var/lib/branchdb"
        for m in info["Mounts"]
    )
    assert (
        docker(
            "exec",
            "--user",
            "0",
            first,
            "/opt/branchdb/bin/branchdb",
            "version",
            check=False,
        ).returncode
        != 0
    )
    with connect(first, role="migration") as db:
        db.execute("CREATE TABLE durable(id int PRIMARY KEY,value text)")
    with connect(first) as db:
        db.execute("INSERT INTO durable VALUES(1,'main')")
    control(first, "branch", "create", "a")
    with connect(first, "a") as db:
        db.execute("UPDATE durable SET value='a' WHERE id=1")
    with connect(first) as db:
        assert db.execute("SELECT value FROM durable")[0]["value"] == "main"
    report["checks"].append(
        "non-root UID, read-only root, explicit named volume, SCRAM roles and isolated branch writes"
    )
    docker("stop", "-t", "30", first)
    info = json.loads(docker("inspect", first).stdout)[0]
    assert info["State"]["ExitCode"] == 0
    docker("rm", first)
    containers.remove(first)
    second = first + "-replacement"
    start(second)
    with connect(second) as db:
        assert db.execute("SELECT value FROM durable")[0]["value"] == "main"
    with connect(second, "a") as db:
        assert db.execute("SELECT value FROM durable")[0]["value"] == "a"
    report["checks"].append(
        "SIGINT fast shutdown and retained credentials/data across container replacement"
    )
    report["passed"] = True
finally:
    for name in containers:
        docker("rm", "-f", name, check=False)
    docker("volume", "rm", volume, check=False)
if args.report:
    args.report.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
