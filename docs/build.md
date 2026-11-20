# Build and test

Clone BranchDB and build out of tree. The source layout is PostgreSQL's;
the engine must be built from this fork.

```sh
git clone https://github.com/harsh-kr-jha/branchdb.git
cd branchdb
branchdb_source="$PWD"
mkdir -p /tmp/branchdb-build /tmp/branchdb-install
cd /tmp/branchdb-build
"$branchdb_source/configure" --prefix=/tmp/branchdb-install \
  --without-icu --without-readline CFLAGS=-O2
make -j4
make install
make -C src/test/regress check
```

Install a C toolchain, Make, Bison, Flex, and zlib development headers first.
Ubuntu CI uses `build-essential bison flex zlib1g-dev`. macOS uses the command-line
toolchain and Homebrew Bison/Flex, with their `bin` directories on PATH.
Run server tests as an ordinary user.

## Native and framework acceptance

From the repository root, install fixture dependencies in a separate environment.
Use Python 3.13.9, matching the standalone runtime, and Node 23.6.1 for the
pinned Prisma fixture. CI creates the client environment with that pinned
interpreter rather than the runner's system Python.

```sh
python3.13 -m venv /tmp/branchdb-client
/tmp/branchdb-client/bin/pip install -r contrib/branch_mvcc/native_m8/requirements.txt
npm --prefix contrib/branch_mvcc/native_m8/prisma ci
/tmp/branchdb-client/bin/python -B contrib/branch_mvcc/native_m9/verify.py \
  --bindir /tmp/branchdb-install/bin --logs /tmp/branchdb-engine-logs
```

This runs native checkpoints 1–9 and real Prisma/Django acceptance. It starts
disposable clusters and writes logs outside the checkout. Reproducing performance
measurements is separate; see [benchmarks](benchmarks.md).

## Archives and containers

Packaging needs a native engine install, matching standalone CPython **3.13.9**,
and platform tools: `otool`, `install_name_tool`, `lipo`, and `codesign` on macOS
ARM64; `ldd` and `patchelf` on Linux x86_64. Follow the
[packaging and runtime acceptance commands](../contrib/branch_mvcc/native_m10/README.md#build-and-validate).
The Linux image consumes the same archive; see the
[container instructions](../contrib/branch_mvcc/native_m10/README.md#linux-container).

The [Actions workflow](../.github/workflows/branchdb-runtime.yml) builds both
platforms, runs core and native suites, packages each runtime, exercises relocation
and framework workflows, runs the Linux container gate, and checks the shipped
isolation demo. Tested archives and reports are uploaded as workflow artifacts.
