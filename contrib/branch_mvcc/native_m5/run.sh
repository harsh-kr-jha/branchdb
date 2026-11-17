#!/bin/sh
# Full native acceptance plus the runnable app fixture and measured costs.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
work_dir=${1:-/tmp/branch-mvcc-m5}
"$script_dir/../native_m4/run.sh" "$work_dir"
work_dir=$(CDPATH= cd -- "$work_dir" && pwd)
python3 -B "$script_dir/test.py" --bindir "$work_dir/install/bin" \
    --report "$work_dir/measurements.json" > "$work_dir/app-test.log" 2>&1
cat "$work_dir/app-test.log"
printf 'Start the interactive demo: python3 -B %s/demo.py --bindir %s/install/bin\n' "$script_dir" "$work_dir"
