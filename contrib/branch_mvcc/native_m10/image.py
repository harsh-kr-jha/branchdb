#!/usr/bin/env python3
"""Build a non-root container from an already validated native Linux archive."""

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("--archive", required=True, type=Path)
parser.add_argument("--tag", default="branchdb:developer-beta")
args = parser.parse_args()
with tempfile.TemporaryDirectory(prefix="branchdb-image-", dir="/tmp") as directory:
    path = Path(directory)
    shutil.copyfile(args.archive, path / "runtime.tar.gz")
    for name in ("Dockerfile", "entrypoint.sh"):
        shutil.copy2(HERE / name, path / name)
    subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "-t", args.tag, str(path)],
        check=True,
    )
