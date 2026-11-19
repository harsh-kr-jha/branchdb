#!/usr/bin/env python3
"""Check measured gates and render a compact comparison from raw run artifacts."""
import argparse
import json
from pathlib import Path


def render(directory):
    def read(name):return json.loads((directory/name).read_text())
    baseline=read('baseline.json')
    candidate=read('optimized-final.json')
    controls=read('control.json')
    previous={(c['rows'],c['branches']):c for c in baseline['cases']}
    current={(c['rows'],c['branches']):c for c in candidate['cases']}
    expected={(rows,branches) for rows in (1000,100000,1000000) for branches in (1,10,100)}
    assert set(previous)==expected and set(current)==expected,'Incomplete benchmark matrix'
    assert set(controls['results'])=={'scalar','heap_point','heap_update'},'Incomplete control gate'
    def duration(measure):
        return f"{measure['median_ms']:.3f}" if 'median_ms' in measure else f"timeout ({measure['timeout_ms']} ms)"
    lines=['# Release measurements','',
           'Times are milliseconds; one warmup followed by five measured queries per case. Timeout entries are failed attempts, not completed medians.','',
           '| Rows | Branches | Old point | New point | Old scan | New scan | Old first write | New first write |',
           '| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for key,new in current.items():
        old=previous[key]
        lines.append('| '+ ' | '.join([f'{key[0]:,}',str(key[1])]+[duration(case[metric]) for metric in ('branch_point','branch_scan','branch_update') for case in (old,new)])+' |')
        assert new['branch_point']['median_ms']<old['branch_point']['median_ms']
    # Verify the improvement repeats independently; interrupted baseline samples
    # cannot be assigned a fictitious median or speedup.
    repeat=read('baseline-repeat.json')
    assert {(c['rows'],c['branches']) for c in repeat['cases']}==expected,'Incomplete baseline repeat'
    assert controls['rounds']>=5,'Control gate needs at least five rounds'
    for run in ('optimized.json','optimized-repeat.json','optimized-final.json'):
        cases=read(run)['cases']
        assert {(c['rows'],c['branches']) for c in cases}==expected,'Incomplete repeat matrix'
        for new in cases:
            assert new['branch_point']['median_ms']<previous[(new['rows'],new['branches'])]['branch_point']['median_ms']
    for old in repeat['cases']:
        if 'median_ms' in old['branch_point']:
            assert current[(old['rows'],old['branches'])]['branch_point']['median_ms']<old['branch_point']['median_ms']
    lines += ['','## Heap-control overhead','',
              'These controls are ordinary heap tables in the same branch-enabled instance, using the same client and dataset.','',
              '| Rows | Branches | New point / heap point | New scan / heap scan |',
              '| ---: | ---: | ---: | ---: |']
    for key,new in current.items():
        lines.append(f"| {key[0]:,} | {key[1]} | {new['branch_point']['median_ms']/new['heap_point']['median_ms']:.2f}× | {new['branch_scan']['median_ms']/new['heap_scan']['median_ms']:.2f}× |")
    lines += ['','## Unchanged-workload gate','',
              'Separate databases contain no native metadata. Five alternating two-second pgbench runs per binary and workload; prepared protocol, one client. Each median must retain at least 90% of baseline TPS.','',
              '| Workload | Baseline TPS | Candidate TPS | Ratio |',
              '| --- | ---: | ---: | ---: |']
    for name,result in controls['results'].items():
        assert result['passed'] and result['candidate_over_baseline']>=.9
        lines.append(f"| {name} | {result['baseline_median_tps']:.0f} | {result['candidate_median_tps']:.0f} | {result['candidate_over_baseline']:.3f} |")
    old_memory=int(read('baseline-profile.json')['backend_allocated_bytes'])
    new_memory=int(read('optimized-profile.json')['backend_allocated_bytes'])
    lines += ['','## Memory and sampling','',
              f'A one-million-row cursor held {old_memory/1048576:.1f} MiB of backend memory before and {new_memory/1048576:.1f} MiB afterward, as reported by pg_backend_memory_contexts. This is allocated memory with the cursor open, not peak RSS. Hash-map construction temporarily also retains a root-selection map.','',
              'macOS sample profiles were captured for both release binaries. See the profile JSON and stack artifacts for the measured scan phase.','',
              'All measured gates passed. Absolute results are specific to this machine; the full scan and the first write still perform O(table versions) work.']
    return '\n'.join(lines)+'\n'

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('directory',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args();result=render(args.directory)
    if args.output:args.output.write_text(result)
    else:print(result)
