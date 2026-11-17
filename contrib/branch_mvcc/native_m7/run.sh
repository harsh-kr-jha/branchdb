#!/bin/sh
# Shared PGPROC layout changed: use a clean native build for this checkpoint.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
work_dir=${1:-/tmp/branch-mvcc-m7}
mkdir -p "$work_dir"
if [ -f "$work_dir/build/GNUmakefile" ]; then
    make -C "$work_dir/build" clean > "$work_dir/clean.log" 2>&1
fi
"$script_dir/../native_m6/run.sh" "$work_dir"
work_dir=$(CDPATH= cd -- "$work_dir" && pwd)
python3 -B "$script_dir/test.py" --bindir "$work_dir/install/bin" > "$work_dir/lifecycle-test.log" 2>&1
cat "$work_dir/lifecycle-test.log"
