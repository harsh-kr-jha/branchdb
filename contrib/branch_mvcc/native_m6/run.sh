#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
work_dir=${1:-/tmp/branch-mvcc-m6}
"$script_dir/../native_m5/run.sh" "$work_dir"
work_dir=$(CDPATH= cd -- "$work_dir" && pwd)
python3 -B "$script_dir/test.py" --bindir "$work_dir/install/bin" > "$work_dir/security-test.log" 2>&1
cat "$work_dir/security-test.log"
