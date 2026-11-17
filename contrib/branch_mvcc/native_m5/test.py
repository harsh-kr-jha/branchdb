#!/usr/bin/env python3
"""End-to-end app migrations/HTTP CRUD, recovery, and measured native costs."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from branches import connection_url
from cluster import Cluster, HERE
from db import Client
from migrate import migrate


def api(url, path, method='GET', body=None, expected=200):
    request = Request(url+'/api/'+path, method=method,
                      data=None if body is None else json.dumps(body).encode(),
                      headers={'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=12) as response:
            status, payload = response.status, json.load(response)
    except HTTPError as error:
        status, payload = error.code, json.load(error)
    assert status == expected, (method, path, status, payload)
    return payload


def finished(process, error=None):
    out, err = process.communicate(timeout=15)
    if error:
        assert process.returncode and error in err, (out, err)
    else:
        assert process.returncode == 0, (out, err)
    return json.loads(err if error else out)


def wait_for(predicate, message, timeout=10):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():
            return
        time.sleep(.03)
    raise AssertionError(message)


def summary(values):
    ordered = sorted(values)
    return {'samples': len(values), 'median_ms': round(statistics.median(values), 3),
            'p95_ms': round(ordered[min(len(ordered)-1, int(len(ordered)*.95))], 3)}


def wal_bytes(before, after):
    def number(lsn):
        high, low = lsn.split('/')
        return (int(high, 16)<<32)+int(low, 16)
    return number(after)-number(before)


def benchmark(client, size=60):
    with client.connect() as db:
        samples = {key: [] for key in ('insert', 'point_read', 'update')}
        for n in range(size):
            key = str(UUID(int=n+100))
            for operation, query, params in (
                ('insert', 'INSERT INTO benchmark_rows VALUES($1,$2)', (key, 'original')),
                ('point_read', 'SELECT payload FROM benchmark_rows WHERE id=$1', (key,)),
                ('update', 'UPDATE benchmark_rows SET payload=$2 WHERE id=$1', (key, 'updated'))):
                start = time.perf_counter()
                rows = db.execute(query, params)
                samples[operation].append((time.perf_counter()-start)*1000)
                if operation == 'point_read':
                    assert rows == [{'payload': 'original'}], rows
        assert db.execute("SELECT count(*) AS n FROM benchmark_rows WHERE payload='updated'")[0]['n'] == str(size)
    return {key: summary(values) for key, values in samples.items()}


def run(cluster):
    metrics = {'fixture': 'branch_tasks', 'benchmark': {}, 'fork_ms': {}}
    main = cluster.client()
    assert migrate(main, [HERE/'migrations/base']) == ['0001_tasks']
    with main.connect() as db:
        db.execute('CREATE TABLE baseline_documents(id integer PRIMARY KEY,payload text NOT NULL)')
        db.execute("INSERT INTO baseline_documents SELECT n,repeat(md5(n::text),20) FROM generate_series(1,1000) n")
        db.execute('CREATE TABLE benchmark_rows(id uuid PRIMARY KEY,payload text NOT NULL)')
    seed = str(UUID(int=1))
    apps = {'main': cluster.app('main')}
    api(apps['main'], 'tasks', 'POST', {'id': seed, 'title': 'Inherited task'}, 201)

    def footprint():
        with main.connect() as db:
            row = db.execute("SELECT pg_database_size(current_database()) AS database_bytes, "
                             "pg_total_relation_size('branch_native.versions')+"
                             "pg_total_relation_size('branch_native.objects')+"
                             "pg_total_relation_size('branch_native.branches') AS metadata_bytes,"
                             '(SELECT count(*) FROM branch_native.versions) AS versions, '
                             "pg_relation_size('baseline_documents') AS baseline_heap_bytes,"
                             'pg_current_wal_insert_lsn() AS wal_lsn')[0]
        return {key: value if key == 'wal_lsn' else int(value) for key, value in row.items()}

    metrics['before_fork'] = footprint()
    for branch in ('a', 'b'):
        with main.connect() as db:
            start = time.perf_counter()
            db.execute('SELECT branch_create($1)', (branch,))
            metrics['fork_ms'][branch] = round((time.perf_counter()-start)*1000, 3)
        apps[branch] = cluster.app(branch)
    metrics['after_fork'] = footprint()
    assert metrics['before_fork']['versions'] == metrics['after_fork']['versions']
    source = None
    baseline = None
    for branch in apps:
        with cluster.client(branch).connect() as db:
            path = db.execute("SELECT pg_relation_filepath('baseline_documents'::regclass) AS heap, "
                              "pg_relation_filepath('baseline_documents_pkey'::regclass) AS index")[0]
            fingerprint = db.execute("SELECT count(*) AS n,md5(string_agg(md5(payload),'' ORDER BY id)) AS hash FROM baseline_documents")[0]
            if source is None:
                source, baseline = path, fingerprint
            assert path == source and fingerprint == baseline
        assert api(apps[branch], 'tasks')[0]['title'] == 'Inherited task'
        with urlopen(apps[branch], timeout=5) as response:
            assert b'<title>Branch Tasks</title>' in response.read()
    print('PASS: three live apps; forks retain identical physical baseline files and create no user versions', flush=True)

    # The apps already have live pooled connections before their migrations.
    # Hold each migration after DDL to prove real overlap rather than scheduling.
    directories = {}
    for branch, feature in (('a', 'feature_a'), ('b', 'feature_b')):
        directory = cluster.root/feature
        directory.mkdir()
        source_file = next((HERE/'migrations'/feature).glob('*.sql'))
        (directory/source_file.name).write_text(source_file.read_text()+'\nSELECT pg_sleep(1.2);\n')
        directories[branch] = directory
    observer_stop = threading.Event()
    observer_errors, observer_times = [], []
    def observe():
        while not observer_stop.is_set():
            try:
                start = time.perf_counter()
                info = api(apps['main'], 'info')
                assert 'assignee' not in info['task_columns'] and 'priority' not in info['task_columns']
                assert api(apps['main'], 'tasks')[0]['title'] == 'Inherited task'
                observer_times.append((time.perf_counter()-start)*1000)
            except BaseException as error:
                observer_errors.append(repr(error))
                break
            observer_stop.wait(.05)
    thread = threading.Thread(target=observe)
    thread.start()
    try:
        migrations = {branch: cluster.migration(branch, HERE/'migrations/base', directory)
                      for branch, directory in directories.items()}
        with main.connect() as monitor:
            wait_for(lambda: monitor.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE "
                      "application_name IN ('tasks_migrate_a','tasks_migrate_b') AND wait_event='PgSleep'")[0]['n'] == '2',
                     'branch migrations did not overlap')
        for process in migrations.values():
            finished(process)
    finally:
        observer_stop.set()
        thread.join(timeout=10)
    assert not observer_errors and len(observer_times)>2, (observer_errors, observer_times)
    metrics['main_observer_during_parallel_migrations'] = summary(observer_times)
    for branch, own, absent in (('a', 'assignee', 'priority'), ('b', 'priority', 'assignee')):
        info = api(apps[branch], 'info')
        assert own in info['task_columns'] and absent not in info['task_columns'], info
        assert len(info['migrations']) == 2
        assert ('body' if branch == 'a' else 'score') in info['note_columns']
        assert api(apps[branch], 'tasks')[0][own] is None
    assert api(apps['main'], 'info')['migrations'] == ['0001_tasks']
    print('PASS: parallel transactional app migrations, isolated catalogs/history and main HTTP queries throughout', flush=True)

    # No-op replay and immutable history; a failed multi-statement migration
    # rolls back DDL, DML and its history entry together, then can be retried.
    for branch, directory in directories.items():
        assert not finished(cluster.migration(branch, HERE/'migrations/base', directory))['applied']
    altered = cluster.root/'altered';altered.mkdir()
    original = next(directories['a'].glob('*.sql'))
    (altered/original.name).write_text(original.read_text()+'\n-- changed\n')
    finished(cluster.migration('a', altered), 'migration changed after application')
    failing = cluster.root/'failed';failing.mkdir()
    retry = failing/'0003_retry.sql'
    retry.write_text("ALTER TABLE tasks ADD COLUMN reviewed text; UPDATE tasks SET title='bad'; SELECT missing_column FROM tasks;\n")
    finished(cluster.migration('a', failing), '42703')
    assert 'reviewed' not in api(apps['a'], 'info')['task_columns']
    assert api(apps['a'], 'tasks')[0]['title'] == 'Inherited task'
    retry.write_text("ALTER TABLE tasks ADD COLUMN reviewed text;\n")
    retrying = [cluster.migration('a', failing) for _ in range(2)]
    assert sum(len(finished(process)['applied']) for process in retrying) == 1
    assert 'reviewed' not in api(apps['b'], 'info')['task_columns']
    unsupported = cluster.root/'unsupported';unsupported.mkdir()
    (unsupported/'0004_default.sql').write_text("ALTER TABLE tasks ADD COLUMN unsupported text DEFAULT 'x';\n")
    finished(cluster.migration('a', unsupported), '0A000')
    assert 'unsupported' not in api(apps['a'], 'info')['task_columns']

    shared_key = str(uuid4())
    def feature_work(branch):
        field, value = ('assignee', 'Harsh') if branch == 'a' else ('priority', 7)
        api(apps[branch], 'tasks/'+seed, 'PATCH', {'title': branch+' changed', field: value})
        api(apps[branch], 'tasks', 'POST', {'id': shared_key, 'title': branch+' created', field: value}, 201)
        api(apps[branch], 'notes', 'POST', {'task_id': seed, **({'body': 'Collaborate'} if branch == 'a' else {'score': 9})}, 201)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(feature_work, ('a', 'b')))
    api(apps['main'], 'tasks/'+seed, 'PATCH', {'title': 'Main changed'})
    for branch in ('a', 'b'):
        rows = {row['id']: row for row in api(apps[branch], 'tasks')}
        assert rows[seed]['title'] == branch+' changed' and rows[shared_key]['title'] == branch+' created'
    assert len(api(apps['main'], 'tasks')) == 1
    api(apps['a'], 'tasks', 'POST', {'id': shared_key, 'title': 'duplicate'}, 409)
    api(apps['a'], 'tasks/'+seed, 'PATCH', {'priority': 99}, 400)
    with ThreadPoolExecutor(max_workers=2) as executor:
        work = [executor.submit(api, apps['a'], 'tasks/'+seed, 'PATCH', value)
                for value in ({'title': 'Parallel title'}, {'assignee': 'Parallel assignee'})]
        for result in work:
            result.result()
    rows = {row['id']: row for row in api(apps['a'], 'tasks')}
    assert rows[seed]['title'] == 'Parallel title' and rows[seed]['assignee'] == 'Parallel assignee'
    api(apps['a'], 'tasks/'+shared_key, 'DELETE')
    assert shared_key not in {row['id'] for row in api(apps['a'], 'tasks')}
    assert shared_key in {row['id'] for row in api(apps['b'], 'tasks')}
    # Treat SQL-looking text as a value, not executable syntax.
    api(apps['a'], 'tasks/'+seed, 'PATCH', {'title': "O'Reilly; DROP TABLE tasks; --"})
    print('PASS: migration rollback/retry/checksums, simultaneous app CRUD, isolated same-key inserts and concurrent same-branch updates', flush=True)

    # Credential-free libpq URLs select the branch without any session SET.
    with main.connect() as db:
        url = connection_url(db, 'b')
    with Client(cluster.bindir, url, 'b').connect() as db:
        assert db.execute("SELECT current_setting('branch') AS branch")[0]['branch'] == 'b'
        assert db.execute('SELECT priority FROM tasks WHERE id=$1', (seed,))[0]['priority'] == '7'
    # Verify URL startup selection through unmodified psql/libpq semantics,
    # without our Client explicitly supplying a separate branch parameter.
    assert cluster.command('psql', url, '-XAt', '-c', "SELECT current_setting('branch'),priority FROM tasks WHERE id='"+seed+"'").stdout.strip() == 'b|7'
    listed = subprocess.run([sys.executable, '-B', str(HERE/'branches.py'), '--bindir', str(cluster.bindir),
                             '--dsn', cluster.dsn, 'list'], capture_output=True, text=True, check=True)
    assert [row['name'] for row in json.loads(listed.stdout)] == ['main', 'a', 'b']
    created = subprocess.run([sys.executable, '-B', str(HERE/'branches.py'), '--bindir', str(cluster.bindir),
                              '--dsn', cluster.dsn, 'create', 'cli_demo'], capture_output=True, text=True, check=True)
    created_url = json.loads(created.stdout)['url']
    assert cluster.command('psql', created_url, '-XAt', '-c', "SELECT current_setting('branch'),title FROM tasks WHERE id='"+seed+"'").stdout.strip() == 'cli_demo|Main changed'
    cli_url = subprocess.run([sys.executable, '-B', str(HERE/'branches.py'), '--bindir', str(cluster.bindir),
                              '--dsn', cluster.dsn, 'url', 'b'], capture_output=True, text=True, check=True)
    assert cli_url.stdout.strip() == url
    snapshots = {branch: (api(url, 'info'), api(url, 'tasks'), api(url, 'notes')) for branch, url in apps.items()}
    metrics['after_app_work'] = footprint()
    with main.connect() as db:
        started = time.perf_counter()
        db.execute('VACUUM FREEZE')
        metrics['maintenance_ms'] = round((time.perf_counter()-started)*1000, 3)
    metrics['after_maintenance'] = footprint()
    for branch, url in apps.items():
        assert (api(url, 'info'), api(url, 'tasks'), api(url, 'notes')) == snapshots[branch]
    cluster.stop('immediate')
    cluster.start()
    # Existing pools discover the lost sockets and replace them. Only GETs are
    # retried here; an ambiguous failed write is never blindly replayed.
    for branch, url in apps.items():
        def recovered():
            try:
                return (api(url, 'info'), api(url, 'tasks'), api(url, 'notes')) == snapshots[branch]
            except (AssertionError, URLError):
                return False
        wait_for(recovered, 'app pool failed to reconnect after recovery: '+branch)
    api(apps['b'], 'tasks/'+seed, 'PATCH', {'priority': 8})
    print('PASS: app state and pooled branch connections survive maintenance and immediate-stop recovery', flush=True)

    # Measure a fork waiting on an active main transaction, separately from
    # the uncontended fork costs. No timing threshold is a product guarantee.
    with ThreadPoolExecutor(max_workers=1) as executor, main.connect() as holder, main.connect() as monitor:
        holder.execute('BEGIN');holder.execute('SELECT id FROM tasks')
        def blocked_fork():
            with cluster.client(application='tasks_blocked_fork').connect() as db:
                started = time.perf_counter()
                db.execute("SELECT branch_create('blocked')")
                return (time.perf_counter()-started)*1000
        future = executor.submit(blocked_fork)
        wait_for(lambda: monitor.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE application_name='tasks_blocked_fork' AND wait_event='advisory'")[0]['n'] == '1', 'fork did not wait on active transaction')
        time.sleep(.2)
        holder.execute('COMMIT')
        metrics['fork_with_200ms_deliberate_hold_ms'] = round(future.result(timeout=10), 3)
    with cluster.client(database='template1').connect() as db:
        db.execute('CREATE DATABASE branch_bench')
    heap = cluster.client(database='branch_bench')
    with heap.connect() as db:
        db.execute('CREATE TABLE benchmark_rows(id uuid PRIMARY KEY,payload text NOT NULL)')
    before_bench = footprint()['wal_lsn']
    metrics['benchmark']['ordinary_heap'] = benchmark(heap)
    after_heap = footprint()['wal_lsn']
    metrics['benchmark']['native_branch_a'] = benchmark(cluster.client('a'))
    metrics['after_benchmark'] = footprint()
    metrics['wal_bytes'] = {
        'two_initial_forks': wal_bytes(metrics['before_fork']['wal_lsn'], metrics['after_fork']['wal_lsn']),
        'app_work_and_cli_fork': wal_bytes(metrics['after_fork']['wal_lsn'], metrics['after_app_work']['wal_lsn']),
        'maintenance': wal_bytes(metrics['after_app_work']['wal_lsn'], metrics['after_maintenance']['wal_lsn']),
        'heap_benchmark': wal_bytes(before_bench, after_heap),
        'native_benchmark': wal_bytes(after_heap, metrics['after_benchmark']['wal_lsn']),
    }
    log = (cluster.root/'server.log').read_text()
    for bad in ('failed Assert', 'Segmentation fault', 'buffer refcount leak', 'relcache reference leak', 'TupleDesc reference leak'):
        assert bad not in log, bad
    return metrics


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bindir', required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    with Cluster(args.bindir) as cluster:
        report = run(cluster)
    output = json.dumps(report, indent=2)+'\n'
    if args.report:
        args.report.write_text(output)
    print(output, flush=True)
