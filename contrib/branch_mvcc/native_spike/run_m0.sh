#!/bin/sh
# Build/check this source tree and run the backend representation test.
set -eu
spike_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_dir=$(CDPATH= cd -- "$spike_dir/../../.." && pwd)
work_dir=${1:-/tmp/branch-mvcc-m0}
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
make -C "$spike_dir" PG_CONFIG="$work_dir/install/bin/pg_config" > "$work_dir/spike-build.log" 2>&1
make -C "$spike_dir" PG_CONFIG="$work_dir/install/bin/pg_config" install >> "$work_dir/spike-build.log" 2>&1
cluster_dir=$(mktemp -d "$work_dir/cluster.XXXXXX")
started=no
cleanup() {
    if [ "$started" = yes ]; then
        "$work_dir/install/bin/pg_ctl" -D "$cluster_dir/data" -m immediate -w stop > /dev/null
    fi
}
trap cleanup EXIT HUP INT TERM
"$work_dir/install/bin/initdb" -D "$cluster_dir/data" -A trust --no-locale -E UTF8 > "$work_dir/spike-init.log"
"$work_dir/install/bin/pg_ctl" -D "$cluster_dir/data" -l "$cluster_dir/server.log" \
    -o "-k $cluster_dir -p 58440 -c listen_addresses=''" -w start
started=yes
"$work_dir/install/bin/psql" -X -h "$cluster_dir" -p 58440 -d postgres \
    -v ON_ERROR_STOP=1 -c 'SELECT version()' -f "$spike_dir/test.sql" > "$work_dir/spike-test.log" 2>&1
cat "$work_dir/spike-test.log"
printf 'Logs and isolated build: %s\n' "$work_dir"
