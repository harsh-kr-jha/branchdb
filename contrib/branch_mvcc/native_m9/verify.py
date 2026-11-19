#!/usr/bin/env python3
"""Run every native checkpoint and the pinned real-framework acceptance."""
import argparse
from pathlib import Path
import subprocess
import sys
HERE=Path(__file__).resolve().parent
parser=argparse.ArgumentParser()
parser.add_argument('--bindir',required=True)
parser.add_argument('--logs',required=True,type=Path)
args=parser.parse_args();args.logs.mkdir(parents=True,exist_ok=True)
for name,path in [(f'm{n}',HERE/f'../native_m{n}/test.py') for n in range(1,10)]+[('frameworks',HERE/'../native_m8/probe.py')]:
    log=args.logs/f'{name}.log'
    with log.open('w') as output:
        result=subprocess.run([sys.executable,'-B',str(path),'--bindir',args.bindir],stdout=output,stderr=subprocess.STDOUT,timeout=600)
    if result.returncode:
        print(log.read_text()[-12000:],file=sys.stderr)
        raise SystemExit(result.returncode)
    print('PASS:',name,flush=True)
