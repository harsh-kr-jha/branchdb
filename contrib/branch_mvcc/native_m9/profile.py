#!/usr/bin/env python3
"""Optional macOS stack sample and portable backend visibility-map memory probe."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import platform
import subprocess
import threading
import sys
import tempfile
from bench import seed_sql
sys.path.insert(0,str(Path(__file__).resolve().parent/'../native_m5'))
from cluster import Cluster
from db import DatabaseError
parser=argparse.ArgumentParser()
parser.add_argument('--bindir',required=True)
parser.add_argument('--output',required=True,type=Path)
parser.add_argument('--rows',type=int,default=100000)
parser.add_argument('--seed-wal-mb',type=int,default=0)
args=parser.parse_args()
with tempfile.TemporaryDirectory(prefix='m9-profile-',dir='/tmp') as directory:
    seed=Path(directory)/'seed.sql';seed.write_text(seed_sql(args.rows,args.seed_wal_mb))
    with Cluster(args.bindir,setup=seed) as cluster, cluster.client().connect() as db:
        db.execute("SET statement_timeout='5s'")
        pid=db.execute('SELECT pg_backend_pid() AS pid')[0]['pid']
        report={'rows':args.rows,'platform':platform.platform()}
        if platform.system()=='Darwin':
            stop=threading.Event()
            def work():
                while not stop.is_set():
                    try:db.execute('SELECT count(*) FROM bench')
                    except DatabaseError as error:
                        if error.sqlstate!='57014':raise
            with ThreadPoolExecutor(max_workers=1) as workers:
                future=workers.submit(work)
                try:
                    result=subprocess.run(['/usr/bin/sample',pid,'1','-file',str(args.output.with_suffix('.stacks.txt'))],capture_output=True,text=True,timeout=15)
                    report['sample_returncode']=result.returncode
                    report['sample_output']=result.stdout+result.stderr
                finally:stop.set()
                future.result(timeout=10)
        db.execute("SET statement_timeout='10s'")
        db.execute('BEGIN')
        db.execute('SET enable_indexscan=off')
        db.execute('DECLARE memory_probe CURSOR FOR SELECT * FROM bench')
        db.execute('FETCH 1 FROM memory_probe')
        report['memory_contexts']=db.execute("SELECT name,total_bytes,used_bytes FROM pg_backend_memory_contexts WHERE name IN ('native statement visibility','native selected TIDs','dynahash') ORDER BY total_bytes DESC LIMIT 12")
        report['backend_allocated_bytes']=db.execute('SELECT sum(total_bytes) AS bytes FROM pg_backend_memory_contexts')[0]['bytes']
        db.execute('ROLLBACK')
        args.output.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,indent=2))
