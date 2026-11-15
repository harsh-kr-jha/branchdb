#!/bin/sh
# Build this checkout independently, run core checks and native branch tests.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_dir=$(CDPATH= cd -- "$script_dir/../../.." && pwd)
work_dir=${1:-/tmp/branch-mvcc-m3}
mkdir -p "$work_dir/build" "$work_dir/install"
work_dir=$(CDPATH= cd -- "$work_dir" && pwd)
if [ ! -f "$work_dir/build/GNUmakefile" ]; then
    (cd "$work_dir/build" && "$source_dir/configure" \
        --prefix="$work_dir/install" --enable-cassert --enable-debug \
        --without-icu --without-readline) > "$work_dir/configure.log" 2>&1
fi
make -C "$work_dir/build" -j8 > "$work_dir/build.log" 2>&1
make -C "$work_dir/build" install > "$work_dir/install.log" 2>&1
make -C "$work_dir/build" check > "$work_dir/check.log" 2>&1
python3 "$script_dir/../native_m1/test.py" --bindir "$work_dir/install/bin" > "$work_dir/milestone-1-test.log" 2>&1
python3 "$script_dir/../native_m2/test.py" --bindir "$work_dir/install/bin" > "$work_dir/milestone-2-test.log" 2>&1
python3 "$script_dir/test.py" --bindir "$work_dir/install/bin" > "$work_dir/native-test.log" 2>&1
cat "$work_dir/native-test.log"
printf 'Build and logs: %s\n' "$work_dir"
