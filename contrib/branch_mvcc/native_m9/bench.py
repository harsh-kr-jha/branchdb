#!/usr/bin/env python3
"""Reproducible release comparison. Bootstrap bulk seed is benchmark-only."""
import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / '../native_m5'))
from cluster import Cluster
from db import DatabaseError

HERE = Path(__file__).resolve().parent

def seed_sql(rows, seed_wal_mb=0):
    # Normal heap bulk load avoids the old per-row visibility rebuild. Capture
    # identities before installing scoped catalogs. Only the test administrator
    # can attach this known-correct physical base and its matching MVCC metadata.
    prefix=''
    suffix=''
    if seed_wal_mb:
        prefix=f"ALTER SYSTEM SET max_wal_size='{seed_wal_mb}MB'; ALTER SYSTEM SET min_wal_size='32MB'; ALTER SYSTEM SET checkpoint_completion_target='0.1'; SELECT pg_reload_conf(); SELECT pg_sleep(.1);\n"
        suffix="\\connect template1\nALTER SYSTEM RESET max_wal_size; ALTER SYSTEM RESET min_wal_size; ALTER SYSTEM RESET checkpoint_completion_target; SELECT pg_reload_conf(); SELECT pg_sleep(.1); CHECKPOINT;\n"
    return f'''\\set ON_ERROR_STOP on
{prefix}
CREATE TABLE bench(id integer PRIMARY KEY,payload text);
INSERT INTO bench SELECT n,repeat('x',32) FROM generate_series(1,{rows}) n;
CREATE TABLE heap_bench(LIKE bench INCLUDING ALL);
INSERT INTO heap_bench SELECT * FROM bench;
ANALYZE bench;
ANALYZE heap_bench;
SELECT 'bench'::regclass::oid AS base_oid,pg_relation_filenode('bench') AS generation \\gset
CREATE TEMP TABLE seed_objects AS
SELECT 'pg_class'::regclass::oid AS catalog,oid AS object,0::bigint AS owner,oid AS source FROM pg_class WHERE relnamespace='public'::regnamespace
UNION ALL SELECT 'pg_type'::regclass::oid,oid,0,0 FROM pg_type WHERE typnamespace='public'::regnamespace
UNION ALL SELECT 'pg_constraint'::regclass::oid,oid,0,0 FROM pg_constraint WHERE connamespace='public'::regnamespace;
\\ir {HERE / '../native_m8/setup.sql'}
INSERT INTO branch_native.objects SELECT * FROM seed_objects ON CONFLICT DO NOTHING;
INSERT INTO branch_native.versions
SELECT id,id,0,0,:base_oid,:generation,(ctid::text::point)[0]::bigint,(ctid::text::point)[1]::bigint,false FROM bench;
SELECT setval('branch_native.version_ids',{rows},true);
UPDATE pg_class SET relam=(SELECT oid FROM pg_am WHERE amname='branch_heap') WHERE oid=:base_oid;
{suffix}'''

def measure(db, query, repetitions, timeout_ms):
    db.execute(f"SET statement_timeout='{timeout_ms}ms'")
    samples = []
    for attempt in range(repetitions + 1):
        start = time.perf_counter_ns()
        try:
            result = db.execute(query)
        except DatabaseError as error:
            if error.sqlstate != '57014':
                raise
            return {'timeout_ms': timeout_ms, 'completed_samples': len(samples), 'samples_ms': samples}
        if attempt:
            samples.append((time.perf_counter_ns()-start)/1e6)
    return {'median_ms': statistics.median(samples), 'samples_ms': samples,
            'result': result}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bindir', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--rows', default='1000,100000,1000000')
    parser.add_argument('--branches', default='1,10,100', help='Total branches including main')
    parser.add_argument('--repetitions', default=5, type=int)
    parser.add_argument('--timeout-ms', default=3000, type=int)
    parser.add_argument('--seed-wal-mb',type=int,default=0,help='Bound bootstrap WAL only; restore defaults and checkpoint before measurements')
    args = parser.parse_args()
    report = {'platform': platform.platform(), 'bindir': str(Path(args.bindir).resolve()),
              'method': 'warm persistent libpq sessions; one warmup then measured repetitions; bootstrap bulk seed',
              'repetitions': args.repetitions, 'seed_wal_mb':args.seed_wal_mb, 'cases': []}
    branch_counts = sorted(map(int,args.branches.split(',')))
    for rows in map(int,args.rows.split(',')):
        seed = Path('/tmp') / f'branch-m9-seed-{rows}-{time.time_ns()}.sql'
        seed.write_text(seed_sql(rows,args.seed_wal_mb))
        try:
            with Cluster(args.bindir, setup=seed) as cluster:
                with cluster.client().connect() as settings:
                    report['measurement_settings']=settings.execute("SELECT current_setting('max_wal_size') AS max_wal_size,current_setting('min_wal_size') AS min_wal_size,current_setting('checkpoint_completion_target') AS checkpoint_completion_target,current_setting('shared_buffers') AS shared_buffers")[0]
                    if args.seed_wal_mb:assert report['measurement_settings']['max_wal_size']=='1GB',report['measurement_settings']
                made = 1
                for branches in branch_counts:
                    timings = []
                    with cluster.client().connect() as admin:
                        admin.execute("SET statement_timeout='0'")
                        while made < branches:
                            start = time.perf_counter_ns()
                            admin.execute(f"SELECT branch_create('b{made}')")
                            timings.append((time.perf_counter_ns()-start)/1e6)
                            made += 1
                    branch = 'main' if branches == 1 else f'b{branches-1}'
                    case = {'rows': rows, 'branches': branches, 'fork_samples_ms': timings}
                    with cluster.client(branch).connect() as db, cluster.client().connect() as heap:
                        case['scalar'] = measure(db,'SELECT 1 AS n',args.repetitions,args.timeout_ms)
                        case['heap_point'] = measure(heap,f'SELECT payload FROM heap_bench WHERE id={rows}',args.repetitions,args.timeout_ms)
                        case['heap_scan'] = measure(heap,'SELECT count(*) AS n FROM heap_bench',args.repetitions,args.timeout_ms)
                        case['branch_point'] = measure(db,f'SELECT payload FROM bench WHERE id={rows}',args.repetitions,args.timeout_ms)
                        case['branch_scan'] = measure(db,'SELECT count(*) AS n FROM bench',args.repetitions,args.timeout_ms)
                        case['branch_update'] = measure(db,f"UPDATE bench SET payload='changed' WHERE id={rows}",args.repetitions,args.timeout_ms)
                        # Server-side timing and buffer counts help separate client overhead.
                        try:
                            case['explain'] = db.execute(f'EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT payload FROM bench WHERE id={rows}')
                        except DatabaseError as error:
                            if error.sqlstate != '57014': raise
                            case['explain'] = {'timeout_ms':args.timeout_ms}
                    case['data_bytes'] = sum(p.stat().st_size for p in cluster.data.rglob('*') if p.is_file())
                    report['cases'].append(case)
                    args.output.write_text(json.dumps(report,indent=2)+'\n')
                    print(json.dumps({k:case[k] for k in ('rows','branches','branch_point','branch_scan','branch_update')}),flush=True)
        finally:
            seed.unlink(missing_ok=True)

if __name__ == '__main__':
    main()
