#!/usr/bin/env python3
"""Alternating release pgbench controls, in databases without native metadata."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parent/'../native_m5'))
from cluster import Cluster

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--baseline',required=True)
    parser.add_argument('--candidate',required=True)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--rounds',type=int,default=5)
    parser.add_argument('--seconds',type=int,default=2)
    args=parser.parse_args()
    report={'platform':platform.platform(),'seconds':args.seconds,'rounds':args.rounds,'results':{}}
    with ExitStack() as cleanup:
        clusters={}
        for label,bindir in [('baseline',args.baseline),('candidate',args.candidate)]:
            # A setup file inside /tmp is retained only for the cluster lifetime.
            directory=Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix='m9-control-',dir='/tmp')))
            setup=directory/'setup.sql'
            setup.write_text('CREATE TABLE control(id int PRIMARY KEY,value int); INSERT INTO control SELECT n,n FROM generate_series(1,1000)n; ANALYZE control;')
            clusters[label]=cleanup.enter_context(Cluster(bindir,setup=setup))
        for workload,query in [('scalar','SELECT 1;'),('heap_point','SELECT value FROM control WHERE id=500;'),('heap_update','UPDATE control SET value=value+1 WHERE id=500;')]:
            values={'baseline':[],'candidate':[]}
            for iteration in range(args.rounds):
                order=('baseline','candidate') if iteration%2==0 else ('candidate','baseline')
                for label in order:
                    cluster=clusters[label]
                    script=cluster.root/'workload.sql';script.write_text(query)
                    result=subprocess.run([str(cluster.bindir/'pgbench'),'-n','-M','prepared','-c','1','-j','1','-T',str(args.seconds),'-f',str(script),cluster.dsn],env=cluster.env,capture_output=True,text=True,check=True,timeout=args.seconds+15)
                    output=result.stdout+result.stderr
                    assert 'number of failed transactions: 0' in output,output
                    tps=float(re.search(r'^tps = ([0-9.]+)',output,re.M)[1])
                    values[label].append(tps)
                    (cluster.root/f'{workload}-{iteration}.log').write_text(output)
            baseline=statistics.median(values['baseline']);candidate=statistics.median(values['candidate'])
            report['results'][workload]={'samples_tps':values,'baseline_median_tps':baseline,'candidate_median_tps':candidate,'candidate_over_baseline':candidate/baseline,'passed':candidate>=.9*baseline}
            args.output.write_text(json.dumps(report,indent=2)+'\n')
            print(workload,report['results'][workload],flush=True)
    assert all(r['passed'] for r in report['results'].values()),'Unchanged-workload regression exceeds 10%'

if __name__=='__main__':main()
