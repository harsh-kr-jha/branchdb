#!/usr/bin/env python3
"""Start a disposable native cluster and three live task apps. Ctrl-C cleans up."""
import argparse
import json
import signal

from cluster import Cluster, HERE
from migrate import migrate


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bindir', required=True)
    args = parser.parse_args()
    with Cluster(args.bindir, autovacuum=True) as cluster:
        migrate(cluster.client(), [HERE/'migrations/base'])
        with cluster.client().connect() as db:
            db.execute("INSERT INTO tasks VALUES('00000000-0000-0000-0000-000000000001',"
                       "'Build something useful','todo',clock_timestamp())")
            db.execute("SELECT branch_create('a')")
            db.execute("SELECT branch_create('b')")
        migrate(cluster.client('a'), [HERE/'migrations/base', HERE/'migrations/feature_a'])
        migrate(cluster.client('b'), [HERE/'migrations/base', HERE/'migrations/feature_b'])
        print('Open these URLs in separate tabs. Each app has its own branch:', flush=True)
        for branch in ('main', 'a', 'b'):
            print(json.dumps({'branch': branch, 'url': cluster.app(branch)}), flush=True)
        print('Disposable cluster; Ctrl-C stops the apps and deletes demo data.', flush=True)
        try:
            while True:
                signal.pause()
        except KeyboardInterrupt:
            pass
