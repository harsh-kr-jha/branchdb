#!/bin/sh
set -eu
/opt/branchdb/bin/branchdb --json init --listen 0.0.0.0 --port 5432 >/dev/null
exec /opt/branchdb/bin/branchdb --json serve
