#!/usr/bin/env python3
"""Real pinned Prisma/Django workflows through packaged SCRAM branch roles."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import pty
import select
import time

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "../native_m8"
parser = argparse.ArgumentParser()
parser.add_argument("--runtime", required=True, type=Path)
parser.add_argument("--node", default=shutil.which("node"))
parser.add_argument("--report", type=Path)
parser.add_argument("--hide-prefix", action="append", default=[], type=Path)
args = parser.parse_args()
root = args.runtime.resolve()
cli = root / "bin/branchdb"
report = {"checks": []}
with (
    tempfile.TemporaryDirectory(prefix="branchdb-frameworks-", dir="/tmp") as directory,
    ExitStack() as cleanup,
):
    for original in args.hide_prefix:
        original = original.resolve()
        hidden = original.with_name(original.name + ".framework-hidden")
        if hidden.exists():
            raise AssertionError("refusing to overwrite " + str(hidden))
        original.rename(hidden)
        cleanup.callback(hidden.rename, original)
    state = Path(directory) / "state"

    def control(*arguments):
        result = subprocess.run(
            [str(cli), "--state", str(state), "--json", *arguments],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return json.loads(result.stdout)

    control("init")
    control("start")
    credentials = json.loads((state / "credentials.json").read_text())
    config = json.loads((state / "instance.json").read_text())
    spec = importlib.util.spec_from_file_location(
        "packaged_runtime", root / "libexec/branchdb.py"
    )
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)

    def url(branch="main", role="app"):
        return control("branch", "url", branch, "--role", role, "--with-password")[
            "url"
        ]

    secrets_to_redact = [credentials["admin"]["password"]]

    def scrub(value):
        for secret in secrets_to_redact:
            value = value.replace(secret, "[redacted]")
        return value

    def run(arguments, cwd, env, terminal=False):
        if terminal:
            master, slave = pty.openpty()
            process = subprocess.Popen(
                list(map(str, arguments)),
                cwd=cwd,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
            )
            os.close(slave)
            chunks = []
            deadline = time.monotonic() + 90
            try:
                while process.poll() is None or select.select([master], [], [], 0)[0]:
                    assert time.monotonic() < deadline, "framework command timed out"
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            data = os.read(master, 65536)
                        except OSError:
                            break
                        if not data:
                            break
                        chunks.append(data)
                code = process.wait(timeout=5)
                output = b"".join(chunks).decode(errors="replace")
            finally:
                os.close(master)
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
        else:
            result = subprocess.run(
                list(map(str, arguments)),
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
            )
            code = result.returncode
            output = result.stdout + result.stderr
        if code:
            raise AssertionError(scrub(output))
        return scrub(output)

    def env(branch="main", role="app", **extra):
        value = url(branch, role)
        from urllib.parse import urlsplit, unquote

        secrets_to_redact.append(unquote(urlsplit(value).password))
        return dict(os.environ, DATABASE_URL=value, **extra)

    try:
        # A distinct ordinary scratch database is required by Prisma's real
        # migrate-dev command. It never contains a copy of application rows.
        runtime.sql(
            state,
            config,
            credentials,
            "CREATE DATABASE prisma_shadow;",
            database="template1",
        )
        runtime.sql(
            state,
            config,
            credentials,
            "REVOKE ALL ON DATABASE prisma_shadow FROM PUBLIC;",
            database="template1",
        )
        shadow = runtime.endpoint(
            dict(config, database="prisma_shadow"),
            credentials["admin"]["user"],
            password=credentials["admin"]["password"],
        )
        environment = env(role="migration", SHADOW_DATABASE_URL=shadow)
        prisma = FIXTURE / "prisma"
        command = [args.node, prisma / "node_modules/prisma/build/index.js"]
        run([*command, "generate"], prisma, environment)
        run([*command, "migrate", "deploy"], prisma, environment)
        run([args.node, "app.cjs"], prisma, env())
        run([args.node, "seed.cjs"], prisma, env())
        control("branch", "create", "a")
        control("branch", "create", "b")

        def parallel(branch):
            return run(
                [args.node, "parallel.cjs"], prisma, env(branch, BRANCH_NAME=branch)
            )

        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(parallel, ("a", "b")))
        run([*command, "db", "pull", "--print"], prisma, environment)
        project = Path(directory) / "prisma"
        shutil.copytree(prisma, project, ignore=shutil.ignore_patterns("node_modules"))
        (project / "node_modules").symlink_to(
            (prisma / "node_modules").resolve(), target_is_directory=True
        )
        schema = project / "schema.prisma"
        schema.write_text(
            schema.read_text().replace(
                "  title String", "  priority Int @default(0)\n  title String"
            )
        )
        dev = env("b", "migration", SHADOW_DATABASE_URL=shadow)
        run(
            [*command, "migrate", "dev", "--skip-generate", "--name", "priority"],
            project,
            dev,
            terminal=True,
        )
        run([*command, "migrate", "reset", "--force", "--skip-generate"], project, dev)
        assert (
            runtime.query_json(
                state, config, credentials, 'SELECT count(*) AS n FROM "User"'
            )[0]["n"]
            == 1
        )
        report["checks"].append(
            "packaged SCRAM Prisma deploy/dev/shadow/introspection/reset, app CRUD, parallel branch apps and main isolation"
        )
        django = FIXTURE / "django_app"
        run(
            [sys.executable, "manage.py", "migrate", "--noinput"],
            django,
            env(role="migration"),
        )
        run([sys.executable, "app.py", "seed"], django, env())
        for branch in ("django_a", "django_b"):
            control("branch", "create", branch)

        def django_app(branch):
            return run(
                [sys.executable, "app.py", "branch"],
                django,
                env(branch, BRANCH_NAME=branch),
            )

        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(django_app, ("django_a", "django_b")))
        run(
            [sys.executable, "manage.py", "migrate", "tasks", "0001", "--noinput"],
            django,
            env("django_a", "migration"),
        )
        assert (
            runtime.query_json(
                state, config, credentials, "SELECT rank FROM tasks_task"
            )[0]["rank"]
            == 0
        )
        run(
            [sys.executable, "manage.py", "migrate", "tasks", "zero", "--noinput"],
            django,
            env(role="migration"),
        )
        report["checks"].append(
            "packaged SCRAM Django migrations/ORM/parallel branches and isolated reverse migrations"
        )
        assert control("verify")["verified"]
    finally:
        control("stop")
        shutil.rmtree(config["socket"], ignore_errors=True)
report["passed"] = True
if args.report:
    args.report.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
