#!/bin/sh
# Run against an extracted runtime. All instance data is temporary.
set -eu

runtime=$(CDPATH= cd -- "${1:-$(dirname -- "$0")}" && pwd -P)
cli="$runtime/bin/branchdb"
python="$runtime/python/bin/python3"
psql="$runtime/pgsql/bin/psql"
demo_state=$(mktemp -d /tmp/branchdb-demo.XXXXXXXX)
export BRANCHDB_STATE="$demo_state"

cleanup() {
    if "$cli" stop >/dev/null 2>&1; then
        rm -rf -- "$demo_state"
    else
        printf 'Demo state retained for inspection: %s\n' "$demo_state" >&2
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

query() {
    # Isolate psql from inherited PG settings; keep secrets out of argv.
    "$cli" branch url "$1" --role "$2" --with-password |
        "$python" -I -B -c '
import json, os, subprocess, sys
from urllib.parse import parse_qs, unquote, urlsplit
url = urlsplit(json.load(sys.stdin)["url"])
env = {"PATH": os.defpath, "LC_ALL": "C", "PGHOST": url.hostname,
       "PGPORT": str(url.port), "PGUSER": unquote(url.username),
       "PGPASSWORD": unquote(url.password), "PGDATABASE": unquote(url.path[1:])}
options = parse_qs(url.query).get("options")
if options:
    env["PGOPTIONS"] = options[0]
sys.exit(subprocess.run([sys.argv[1], "-X", "-v", "ON_ERROR_STOP=1",
                        "-Atc", sys.argv[2]], env=env).returncode)
' "$psql" "$3"
}

"$cli" init >/dev/null
"$cli" start >/dev/null
query main migration "CREATE TABLE demo_items(id integer PRIMARY KEY, body text NOT NULL)" >/dev/null
query main app "INSERT INTO demo_items VALUES (1, 'inherited')" >/dev/null
"$cli" branch create feature_a >/dev/null
"$cli" branch create feature_b >/dev/null
query feature_a app "UPDATE demo_items SET body = 'changed on feature_a' WHERE id = 1" >/dev/null
query main app "UPDATE demo_items SET body = 'changed on main' WHERE id = 1" >/dev/null

main_value=$(query main app "SELECT body FROM demo_items WHERE id = 1")
a_value=$(query feature_a app "SELECT body FROM demo_items WHERE id = 1")
b_value=$(query feature_b app "SELECT body FROM demo_items WHERE id = 1")
[ "$main_value" = 'changed on main' ]
[ "$a_value" = 'changed on feature_a' ]
[ "$b_value" = 'inherited' ]
printf 'main:      %s\nfeature_a: %s\nfeature_b: %s\n' "$main_value" "$a_value" "$b_value"
