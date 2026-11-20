#!/usr/bin/env python3
"""Assemble a relocatable branchdb archive from pinned native build inputs."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[2]
SYSTEM_ELF = {
    "libc.so.6",
    "libm.so.6",
    "libpthread.so.0",
    "libdl.so.2",
    "librt.so.1",
    "libutil.so.1",
    "libresolv.so.2",
    "ld-linux-x86-64.so.2",
    "linux-vdso.so.1",
}


def command(*arguments):
    return subprocess.check_output(
        list(map(str, arguments)), text=True, stderr=subprocess.STDOUT
    ).strip()


def binary(path):
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as stream:
        magic = stream.read(4)
    return magic == b"\x7fELF" or magic in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")


def dependencies(path):
    if platform.system() == "Darwin":
        return [
            line.strip().split(" (compatibility", 1)[0]
            for line in command("otool", "-L", path).splitlines()[1:]
        ]
    output = command("ldd", path)
    if "not found" in output:
        raise ValueError("unresolved ELF dependency: " + str(path) + "\n" + output)
    return re.findall(r"=> (/[^\s]+)", output)


def os_library(path):
    if platform.system() == "Darwin":
        return path.startswith(("/usr/lib/", "/System/Library/"))
    return Path(path).name in SYSTEM_ELF


def relocate(root):
    """Copy non-OS libraries and remove build-prefix dependency resolution."""
    common = root / "lib"
    common.mkdir()
    discovered = set()
    if platform.system() == "Darwin":
        pending = [p for p in root.rglob("*") if binary(p)]
        while pending:
            path = pending.pop()
            for dependency in dependencies(path):
                if os_library(dependency):
                    continue
                if dependency.startswith("@"):
                    # Standalone CPython already uses relocatable loader paths.
                    if dependency.startswith("@loader_path/"):
                        resolved = path.parent / dependency[len("@loader_path/") :]
                    elif dependency.startswith("@executable_path/"):
                        resolved = (
                            root / "python/bin" / dependency[len("@executable_path/") :]
                        )
                    else:
                        matches = list(root.rglob(Path(dependency).name))
                        if not matches:
                            raise ValueError("unresolved dependency " + dependency)
                        resolved = matches[0]
                    if (
                        not resolved.resolve().is_relative_to(root)
                        or not resolved.exists()
                    ):
                        raise ValueError("external loader dependency " + dependency)
                    continue
                original = Path(dependency)
                if original.resolve().is_relative_to(root):
                    target = original
                else:
                    # Prefer the already-copied PostgreSQL library of this name.
                    matches = [p for p in root.rglob(original.name) if p.is_file()]
                    target = matches[0] if matches else common / original.name
                    if not target.exists():
                        shutil.copy2(original.resolve(), target)
                        pending.append(target)
                    discovered.add(str(original))
                replacement = "@loader_path/" + os.path.relpath(target, path.parent)
                path.chmod(path.stat().st_mode | stat.S_IWUSR)
                command("install_name_tool", "-change", dependency, replacement, path)
            if path.suffix == ".dylib":
                command("install_name_tool", "-id", "@rpath/" + path.name, path)
        # Re-sign after all dependency edits; signing first invalidates callers.
        for path in root.rglob("*"):
            if binary(path):
                command("codesign", "--force", "--sign", "-", path)
                command("codesign", "--verify", "--strict", path)
    else:
        # Establish package search paths before ldd: some standalone Python
        # extensions omit RPATH and otherwise cannot find their bundled libs.
        def elf_rpath(path):
            paths = [common, root / "pgsql/lib", root / "python/lib"]
            rpath = ":".join(
                "$ORIGIN/" + os.path.relpath(p, path.parent) for p in paths
            )
            existing = command("patchelf", "--print-rpath", path)
            relative = [p for p in existing.split(":") if p.startswith("$ORIGIN")]
            command(
                "patchelf",
                "--set-rpath",
                rpath + (":" + ":".join(relative) if relative else ""),
                path,
            )

        pending = [p for p in root.rglob("*") if binary(p)]
        for path in pending:
            elf_rpath(path)
        handled = set()
        while pending:
            path = pending.pop()
            if path in handled:
                continue
            handled.add(path)
            for dependency in dependencies(path):
                original = Path(dependency)
                if os_library(dependency) or original.resolve().is_relative_to(root):
                    continue
                matches = [p for p in root.rglob(original.name) if p.is_file()]
                if not matches:
                    target = common / original.name
                    shutil.copy2(original.resolve(), target)
                    elf_rpath(target)
                    pending.append(target)
                discovered.add(str(original))
    for path in root.rglob("*"):
        if binary(path):
            for dependency in dependencies(path):
                if os_library(dependency):
                    continue
                if platform.system() == "Darwin":
                    if not dependency.startswith("@"):
                        raise ValueError("absolute non-OS dependency " + dependency)
                elif not Path(dependency).resolve().is_relative_to(root):
                    raise ValueError("non-OS dependency outside bundle " + dependency)
    return sorted(discovered)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-prefix", type=Path, required=True)
    parser.add_argument("--python-prefix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", default="developer-beta")
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    system, machine = platform.system(), platform.machine()
    target = {
        ("Darwin", "arm64"): "macos-arm64",
        ("Linux", "x86_64"): "linux-x86_64",
    }.get((system, machine))
    if target is None:
        raise ValueError("build on the target platform: macOS ARM64 or Linux x86_64")
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="branchdb-package-", dir=args.output
    ) as work:
        root = (Path(work) / "branchdb").resolve()
        root.mkdir()
        shutil.copytree(
            args.postgres_prefix.resolve(),
            root / "pgsql",
            symlinks=True,
            ignore=shutil.ignore_patterns("include", "*.a", "*.o", "pgxs", "._*"),
        )
        shutil.copytree(
            args.python_prefix.resolve(),
            root / "python",
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "._*"),
        )
        (root / "bin").mkdir()
        (root / "libexec").mkdir()
        (root / "share").mkdir()
        (root / "licenses").mkdir()
        shutil.copyfile(SOURCE / "examples/branch-isolation.sh", root / "demo.sh")
        (root / "demo.sh").chmod(0o755)
        shutil.copyfile(HERE / "branchdb.py", root / "libexec/branchdb.py")
        for n in (3, 6, 7, 8):
            directory = root / "share" / f"native_m{n}"
            directory.mkdir()
            shutil.copyfile(HERE / f"../native_m{n}/setup.sql", directory / "setup.sql")
        shutil.copyfile(SOURCE / "COPYRIGHT", root / "licenses/PostgreSQL-COPYRIGHT")
        shutil.copyfile(SOURCE / "LICENSE", root / "licenses/BranchDB-LICENSE")
        shutil.copytree(HERE / "licenses", root / "licenses/dependencies")
        shutil.copyfile(
            args.python_prefix / "lib/python3.13/LICENSE.txt",
            root / "licenses/Python-LICENSE.txt",
        )
        launcher = root / "bin/branchdb"
        launcher.write_text(
            '#!/bin/sh\nset -eu\nruntime=$(CDPATH= cd -- "${0%/*}/.." && pwd -P)\nexec "$runtime/python/bin/python3" -I -B -X utf8 "$runtime/libexec/branchdb.py" "$@"\n'
        )
        launcher.chmod(0o755)
        for path in root.rglob("*"):
            if not binary(path):
                continue
            if system == "Darwin":
                if "arm64" not in command("lipo", "-archs", path).split():
                    raise ValueError("non-ARM64 binary: " + str(path))
            else:
                with path.open("rb") as stream:
                    header = stream.read(20)
                if (
                    header[4] != 2
                    or header[5] != 1
                    or struct.unpack("<H", header[18:20])[0] != 62
                ):
                    raise ValueError("non-x86_64 ELF binary: " + str(path))
        external = relocate(root)
        # No symlink in the payload may refer to a build machine path.
        for path in root.rglob("*"):
            if path.is_symlink() and (
                os.path.isabs(os.readlink(path))
                or not path.resolve().is_relative_to(root)
            ):
                raise ValueError("non-relocatable symlink: " + str(path))
        python_version = command(
            root / "python/bin/python3",
            "-I",
            "-B",
            "-c",
            "import sys; print(sys.version.split()[0])",
        )
        if python_version != "3.13.9":
            raise ValueError("runtime requires pinned CPython 3.13.9")
        engine_sources = hashlib.sha256()
        for path in sorted((SOURCE / "src").rglob("*")):
            if (
                path.is_file()
                and not path.name.startswith("._")
                and path.suffix in (".c", ".h", ".dat")
            ):
                engine_sources.update(str(path.relative_to(SOURCE)).encode())
                engine_sources.update(path.read_bytes())
        source_commit = args.source_commit
        if not source_commit:
            try:
                source_commit = command("git", "-C", SOURCE, "rev-parse", "HEAD")
            except (subprocess.CalledProcessError, FileNotFoundError):
                source_commit = "tree:" + engine_sources.hexdigest()
        info = {
            "format": 1,
            "label": args.label,
            "target": target,
            "postgres": command(root / "pgsql/bin/postgres", "--version"),
            "python": python_version,
            "source_commit": source_commit,
            "engine_source_sha256": engine_sources.hexdigest(),
            "engine": hashlib.sha256(
                (root / "pgsql/bin/postgres").read_bytes()
            ).hexdigest(),
            "metadata_format": 3,
            "external_libraries_copied": external,
            "files": {},
        }
        if system == "Darwin":
            versions = []
            for path in root.rglob("*"):
                if binary(path):
                    versions += re.findall(
                        r"\bminos ([0-9.]+)", command("otool", "-l", path)
                    )
            info["minimum_macos"] = max(
                versions, key=lambda value: tuple(map(int, value.split(".")))
            )
        else:
            info["build_libc"] = dict(zip(("name", "version"), platform.libc_ver()))
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                record = {"symlink": os.readlink(path)}
            elif path.is_file():
                record = {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "mode": oct(stat.S_IMODE(path.stat().st_mode)),
                }
            else:
                continue
            info["files"][str(path.relative_to(root))] = record
        (root / "manifest.json").write_text(
            json.dumps(info, indent=2, sort_keys=True) + "\n"
        )
        archive = args.output / f"branchdb-{args.label}-{target}.tar.gz"
        with (
            archive.open("wb") as output,
            gzip.GzipFile(
                filename="", fileobj=output, mode="wb", mtime=0
            ) as compressed,
            tarfile.open(fileobj=compressed, mode="w") as tar,
        ):
            for path in [root, *sorted(root.rglob("*"))]:
                item = tar.gettarinfo(
                    str(path), arcname=str(path.relative_to(root.parent))
                )
                item.uid = item.gid = 0
                item.uname = item.gname = "root"
                item.mtime = 0
                if item.isfile():
                    with path.open("rb") as stream:
                        tar.addfile(item, stream)
                else:
                    tar.addfile(item)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.with_suffix(archive.suffix + ".sha256").write_text(
            digest + "  " + archive.name + "\n"
        )
        (args.output / (archive.name + ".manifest.json")).write_text(
            json.dumps(info, indent=2, sort_keys=True) + "\n"
        )
        print(json.dumps({"archive": str(archive), "sha256": digest, "target": target}))


if __name__ == "__main__":
    main()
