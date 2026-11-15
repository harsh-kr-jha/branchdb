#!/usr/bin/env python3
"""Native branch-heap integration tests. Standard library; disposable cluster."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument('--bindir', required=True)
args = parser.parse_args()
bin_dir = Path(args.bindir).resolve()
here = Path(__file__).resolve().parent

with tempfile.TemporaryDirectory(prefix='branch-native-m2-', dir='/tmp') as tmp:
    data = Path(tmp) / 'data'
    env = dict(os.environ, PGHOST=tmp, PGPORT='58442', PGDATABASE='postgres')
    for var in ('PGSERVICE', 'PGOPTIONS', 'PGUSER'):
        env.pop(var, None)
    def command(tool, *argv):
        return subprocess.run([str(bin_dir / tool), *map(str, argv)], env=env,
                              capture_output=True, text=True, check=True)
    def sql(query, branch='main', error=None):
        result = subprocess.run([str(bin_dir / 'psql'), '-XAt', '-v', 'ON_ERROR_STOP=1',
                                 '-v', 'VERBOSITY=verbose', '-c', query],
                                env=dict(env, PGOPTIONS=f'-c branch={branch}'),
                                capture_output=True, text=True, timeout=20,
                                input='1\tcopy\n' if query == 'COPY items FROM STDIN' else None)
        if error:
            assert result.returncode and error in result.stderr, (query, result)
        else:
            assert result.returncode == 0, (query, result.stderr)
        return result.stdout.strip()
    def expect(query, value, branch='main'):
        actual = sql(query, branch)
        assert actual == value, (branch, query, actual, value)
    def background(query, name, branch='main'):
        return subprocess.Popen([str(bin_dir / 'psql'), '-XAt', '-v', 'ON_ERROR_STOP=1',
                                 '-v', 'VERBOSITY=verbose', '-c', query],
                                env=dict(env, PGAPPNAME=name, PGOPTIONS=f'-c branch={branch}'),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    def wait_event(name, event):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if sql(f"SELECT count(*) FROM pg_stat_activity WHERE application_name='{name}' AND wait_event='{event}'") == '1':
                return
            time.sleep(.03)
        raise AssertionError(f'{name} did not reach {event}')
    def finish(process, error=None):
        out, err = process.communicate(timeout=12)
        if error:
            assert process.returncode and error in err, (out, err)
        else:
            assert process.returncode == 0, (out, err)
        return out
    running = False
    try:
        command('initdb', '-D', data, '-A', 'trust', '--no-locale', '-E', 'UTF8')
        options = f"-k {tmp} -p 58442 -c listen_addresses='' -c max_connections=16 -c shared_buffers=16MB -c autovacuum=off"
        def start():
            command('pg_ctl', '-D', data, '-l', Path(tmp) / 'server.log', '-o', options, '-w', 'start')
        start()
        running = True
        print(sql('SELECT version()'), flush=True)
        command('psql', '-X', '-v', 'ON_ERROR_STOP=1', '-f', here / '../native_m1/setup.sql')
        sql("CREATE TABLE items(id bigint PRIMARY KEY, sku text UNIQUE, value text) USING branch_heap WITH (autovacuum_enabled=false)")
        sql("CREATE TABLE composite(a int, b int, value text, PRIMARY KEY(a,b)) USING branch_heap WITH (autovacuum_enabled=false)")
        sql("CREATE TABLE nullkeys(id int PRIMARY KEY, code text UNIQUE NULLS NOT DISTINCT) USING branch_heap WITH (autovacuum_enabled=false)")
        sql("INSERT INTO nullkeys VALUES(1,NULL)")
        sql("CREATE TABLE build_later(id int, value text) USING branch_heap WITH (autovacuum_enabled=false)")
        sql("INSERT INTO items SELECT n,'sku-'||n,'base-'||n FROM generate_series(1,200) n")
        sql("CREATE INDEX items_value ON items(value)")
        sql("INSERT INTO composite VALUES(1,1,'base'),(1,2,'two')")
        sql("INSERT INTO build_later VALUES(1,'old'),(2,'deleted'); UPDATE build_later SET value='new' WHERE id=1; DELETE FROM build_later WHERE id=2")
        sql("CREATE UNIQUE INDEX build_later_pk ON build_later(id)")
        sql("CREATE TABLE duplicates(id int) USING branch_heap WITH (autovacuum_enabled=false)")
        sql("INSERT INTO duplicates VALUES(1),(1)")
        sql("CREATE UNIQUE INDEX invalid_duplicate ON duplicates(id)", error='23505')
        sql("CREATE INDEX invalid_partial ON items(value) WHERE id>1", error='0A000')
        sql("CREATE INDEX invalid_expr ON items((lower(value)))", error='0A000')
        sql("CREATE INDEX CONCURRENTLY invalid_concurrent ON items(value)", error='0A000')
        sql("CREATE TABLE invalid_deferred(id int PRIMARY KEY DEFERRABLE) USING branch_heap", error='0A000')
        sql("CREATE INDEX invalid_hash ON items USING hash(value)", error='0A000')
        sql("SELECT branch_create('a'); SELECT branch_create('b')")
        print('PASS: primary/composite/secondary indexes, pre-fork builds and rejected unsupported builds', flush=True)

        def indexed(query, branch='main'):
            return sql('SET enable_seqscan=off; SET enable_bitmapscan=off; '+query, branch).removeprefix('SET\nSET\n')
        def sequential(query, branch='main'):
            return sql('SET enable_indexscan=off; SET enable_indexonlyscan=off; SET enable_bitmapscan=off; '+query, branch).removeprefix('SET\nSET\nSET\n')
        def same_plans(query, branch='main'):
            a, b = indexed(query, branch), sequential(query, branch)
            assert a == b, (branch, query, a, b)
            return a

        plan = sql('SET enable_seqscan=off; EXPLAIN (COSTS OFF) SELECT * FROM items WHERE id=1', 'a')
        assert 'Index Scan' in plan and 'Seq Scan' not in plan, plan
        plan = sql('SET enable_seqscan=off; EXPLAIN (COSTS OFF) SELECT id FROM items WHERE id=1', 'a')
        assert 'Index Only Scan' not in plan and 'Index Scan' in plan, plan
        expect('SELECT value FROM build_later WHERE id=1', 'new', 'a')
        expect('SELECT count(*) FROM build_later WHERE id=2', '0', 'a')
        sql("INSERT INTO items VALUES(1,'new-key','conflict')", 'a', '23505')
        sql("INSERT INTO items VALUES(1000,'sku-1','unique-conflict')", 'a', '23505')
        sql("INSERT INTO items VALUES(NULL,'null-id','bad')", 'a', '23502')
        sql("INSERT INTO items VALUES(1000,'local-key','a')", 'a')
        sql("INSERT INTO items VALUES(1000,'local-key','b')", 'b')
        sql("INSERT INTO items VALUES(1000,'local-key','main')")
        for branch in ('main','a','b'):
            expect('SELECT value FROM items WHERE id=1000', branch, branch)
        sql("INSERT INTO items VALUES(1000,'different','duplicate')", 'a', '23505')
        sql("INSERT INTO items VALUES(6500,NULL,'null-one'),(6501,NULL,'null-two')", 'a')
        sql("INSERT INTO nullkeys VALUES(2,NULL)", 'a', '23505')
        sql("DELETE FROM nullkeys WHERE id=1; INSERT INTO nullkeys VALUES(2,NULL)", 'a')
        expect('SELECT id FROM nullkeys WHERE code IS NULL', '2', 'a')
        expect('SELECT id FROM nullkeys WHERE code IS NULL', '1', 'b')
        sql("INSERT INTO composite VALUES(1,1,'duplicate')", 'a', '23505')
        sql("INSERT INTO composite VALUES(2,1,'a')", 'a')
        sql("INSERT INTO composite VALUES(2,1,'b')", 'b')
        assert same_plans('SELECT value FROM composite WHERE a=2 AND b=1', 'a') == 'a'
        # Changed indexed values must shadow old entries on every probe.
        sql("UPDATE items SET id=501,sku='a-sku-1',value='a-new' WHERE id=1", 'a')
        assert same_plans('SELECT count(*) FROM items WHERE id=1', 'a') == '0'
        assert same_plans("SELECT count(*) FROM items WHERE sku='sku-1'", 'a') == '0'
        assert same_plans("SELECT count(*) FROM items WHERE value='base-1'", 'a') == '0'
        assert same_plans('SELECT value FROM items WHERE id=501', 'a') == 'a-new'
        assert same_plans('SELECT value FROM items WHERE id=1', 'b') == 'base-1'
        sql("INSERT INTO items VALUES(1,'sku-1','reused-old-key')", 'a')
        sql("UPDATE items SET value='a-same-key' WHERE id=501", 'a')
        sql("UPDATE items SET id=2 WHERE id=501", 'a', '23505')
        expect('SELECT value FROM items WHERE id=501', 'a-same-key', 'a')
        # Delete/reinsert inherited keys; a tombstone must suppress all old index entries.
        sql("DELETE FROM items WHERE id=2; INSERT INTO items VALUES(2,'sku-2','reborn')", 'a')
        expect('SELECT value FROM items WHERE id=2', 'reborn', 'a')
        expect('SELECT value FROM items WHERE id=2', 'base-2', 'b')
        sql("UPDATE items SET sku='parent-later',value='parent-new' WHERE id=3")
        assert same_plans("SELECT value FROM items WHERE sku='sku-3'", 'a') == 'base-3'
        assert same_plans("SELECT count(*) FROM items WHERE sku='parent-later'", 'a') == '0'
        assert same_plans("SELECT value FROM items WHERE sku='parent-later'") == 'parent-new'
        sql("UPDATE items SET sku='sku-10' WHERE id=9", 'a', '23505')
        sql("UPDATE items SET value='after-secondary-failure' WHERE id=9", 'a')
        assert same_plans("SELECT id FROM items WHERE sku='sku-9'", 'a') == '9'
        # Multiple index probes/parameterized rescans share statement visibility.
        assert same_plans('SELECT id FROM items WHERE id IN (1,2,3,501) ORDER BY id', 'a') == '1\n2\n3\n501'
        assert same_plans('SELECT id FROM items WHERE id BETWEEN 1 AND 10 ORDER BY id DESC', 'b') == '\n'.join(map(str,range(10,0,-1)))
        correlated = "SELECT v.id,(SELECT value FROM items t WHERE t.id=v.id) FROM (VALUES(1),(2),(501)) v(id) ORDER BY v.id"
        assert same_plans(correlated, 'a') == '1|reused-old-key\n2|reborn\n501|a-same-key'
        parallel_plan = sql('SET min_parallel_table_scan_size=0; SET min_parallel_index_scan_size=0; SET parallel_setup_cost=0; SET parallel_tuple_cost=0; EXPLAIN (COSTS OFF) SELECT count(*) FROM items', 'a')
        assert 'Gather' not in parallel_plan, parallel_plan
        sql("BEGIN; SAVEPOINT s; INSERT INTO items VALUES(2000,'abort-key','bad'); ROLLBACK TO s; INSERT INTO items VALUES(2000,'abort-key','good'); COMMIT", 'a')
        expect('SELECT value FROM items WHERE id=2000', 'good', 'a')
        sql("BEGIN; UPDATE items SET id=9999 WHERE id=4; ROLLBACK", 'a')
        assert same_plans('SELECT value FROM items WHERE id=4', 'a') == 'base-4'
        assert same_plans('SELECT count(*) FROM items WHERE id=9999', 'a') == '0'
        sql("INSERT INTO items VALUES(3000,'same-statement','one'),(3000,'same-statement-two','two')", 'a', '23505')
        expect('SELECT count(*) FROM items WHERE id=3000', '0', 'a')
        print('PASS: inherited uniqueness, sibling keys, key changes, tombstones, rollback and index/seq agreement', flush=True)

        # Same-branch insert waits then reports uniqueness; siblings/main proceed.
        writer = background("BEGIN; INSERT INTO items VALUES(4000,'pending-key','first'); SELECT pg_sleep(2); COMMIT", 'unique_writer', 'a')
        wait_event('unique_writer', 'PgSleep')
        sql("SET statement_timeout='700ms'; INSERT INTO items VALUES(4000,'pending-key','b')", 'b')
        sql("SET statement_timeout='700ms'; INSERT INTO items VALUES(4000,'pending-key','main')")
        expect('SELECT count(*) FROM items WHERE id=4000', '0', 'a')
        sql("INSERT INTO items VALUES(4000,'alternate','second')", 'a', '23505')
        finish(writer)
        # If that writer aborts, the waiting same-branch insert succeeds.
        writer = background("BEGIN; INSERT INTO items VALUES(4001,'abort-concurrent','bad'); SELECT pg_sleep(2); ROLLBACK", 'abort_writer', 'a')
        wait_event('abort_writer', 'PgSleep')
        sql("INSERT INTO items VALUES(4001,'abort-concurrent','after-abort')", 'a')
        finish(writer)
        expect('SELECT value FROM items WHERE id=4001', 'after-abort', 'a')
        # A concurrent delete/reinsert must not allow a stale update to hit the replacement.
        writer = background("BEGIN; DELETE FROM items WHERE id=5; INSERT INTO items VALUES(5,'sku-5','replacement'); SELECT pg_sleep(2); COMMIT", 'replacement_writer', 'a')
        wait_event('replacement_writer', 'PgSleep')
        sql("UPDATE items SET value='stale-update' WHERE id=5", 'a', '40001')
        finish(writer)
        expect('SELECT value FROM items WHERE id=5', 'replacement', 'a')
        writer = background("BEGIN; UPDATE items SET value='winner' WHERE id=6; SELECT pg_sleep(2); COMMIT", 'update_writer', 'a')
        wait_event('update_writer', 'PgSleep')
        sql("DELETE FROM items WHERE id=6", 'a', '40001')
        finish(writer)
        expect('SELECT value FROM items WHERE id=6', 'winner', 'a')
        writer = background("BEGIN; DELETE FROM items WHERE id=8; SELECT pg_sleep(2); COMMIT", 'delete_writer', 'a')
        wait_event('delete_writer', 'PgSleep')
        sql("INSERT INTO items VALUES(8,'sku-8','insert-after-delete')", 'a')
        finish(writer)
        expect('SELECT value FROM items WHERE id=8', 'insert-after-delete', 'a')
        expect('SELECT value FROM items WHERE id=8', 'base-8', 'b')
        print('PASS: concurrent same-branch insert commit/abort, independent sibling/main keys and stale delete/reinsert', flush=True)

        # Repeated invisible probes cannot kill a shared ancestor's index entry.
        for _ in range(4):
            assert indexed("SELECT count(*) FROM items WHERE sku='sku-1'", 'b') == '1'
            assert indexed("SELECT count(*) FROM items WHERE sku='parent-later'", 'a') == '0'
            assert indexed("SELECT value FROM items WHERE sku='parent-later'") == 'parent-new'
        sql("SELECT branch_create('c')")
        expect("SELECT value FROM items WHERE sku='parent-later'", 'parent-new', 'c')
        snapshots = {b: same_plans('SELECT jsonb_agg(t ORDER BY id) FROM items t WHERE id>=1', b) for b in ('main','a','b','c')}
        command('pg_ctl', '-D', data, '-m', 'immediate', '-w', 'stop')
        running = False
        start()
        running = True
        for branch, snapshot in snapshots.items():
            assert same_plans('SELECT jsonb_agg(t ORDER BY id) FROM items t WHERE id>=1', branch) == snapshot
        sql("INSERT INTO items VALUES(1,'restart-conflict','bad')", 'b', '23505')
        sql("UPDATE items SET value='after-recovery' WHERE id=501", 'a')
        assert same_plans('SELECT value FROM items WHERE id=501', 'a') == 'after-recovery'
        print('PASS: shared index retention, later forks, crash recovery and uniqueness after restart', flush=True)
    except BaseException:
        log = Path(tmp) / 'server.log'
        if log.exists():
            print(log.read_text()[-18000:], flush=True)
        raise
    finally:
        if running:
            command('pg_ctl', '-D', data, '-m', 'immediate', '-w', 'stop')
