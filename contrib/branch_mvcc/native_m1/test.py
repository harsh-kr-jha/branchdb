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

with tempfile.TemporaryDirectory(prefix='branch-native-m1-', dir='/tmp') as tmp:
    data = Path(tmp) / 'data'
    env = dict(os.environ, PGHOST=tmp, PGPORT='58441', PGDATABASE='postgres')
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
        options = f"-k {tmp} -p 58441 -c listen_addresses='' -c max_connections=16 -c shared_buffers=16MB -c autovacuum=off"
        def start():
            command('pg_ctl', '-D', data, '-l', Path(tmp) / 'server.log', '-o', options, '-w', 'start')
        start()
        running = True
        print(sql('SELECT version()'), flush=True)
        command('psql', '-X', '-v', 'ON_ERROR_STOP=1', '-f', here / 'setup.sql')
        sql("CREATE TABLE items(id bigint, value text) USING branch_heap WITH (autovacuum_enabled=false)")
        sql("CREATE TABLE other(id bigint, value text) USING branch_heap WITH (autovacuum_enabled=false)")
        sql("CREATE TABLE large_items(id bigint, value text) USING branch_heap WITH (autovacuum_enabled=false)")
        sql("INSERT INTO large_items SELECT 1, string_agg(md5(n::text), '') FROM generate_series(1,2000) n")
        large_hash = sql("SELECT md5(value) FROM large_items")
        sql("INSERT INTO items SELECT n, 'base-' || n FROM generate_series(1,1000) n")
        expect('SELECT count(*) FROM items', '1000')
        header = sql('SELECT ctid,xmin,xmax FROM items WHERE id=1')
        # Fork is transactional and copies no tuple/version rows.
        versions = sql('SELECT count(*) FROM branch_native.versions')
        sql("BEGIN; SELECT branch_create('rolled_back'); ROLLBACK")
        expect("SELECT count(*) FROM branch_native.branches WHERE name='rolled_back'", '0')
        sql("SELECT branch_create('a'); SELECT branch_create('b')")
        expect('SELECT count(*) FROM branch_native.versions', versions)
        for branch in ('main','a','b'):
            expect('SELECT count(*) FROM items', '1000', branch)
            expect('SELECT ctid,xmin,xmax FROM items WHERE id=1', header, branch)
        print('PASS: forks share the same 1000 physical base rows', flush=True)
        sql("UPDATE items SET value='a' WHERE id=1; DELETE FROM items WHERE id=2; INSERT INTO items VALUES(1001,'only-a')", 'a')
        expect("SELECT value FROM items WHERE id=1", 'a', 'a')
        expect("SELECT value FROM items WHERE id=1", 'base-1', 'b')
        expect('SELECT ctid,xmin,xmax FROM items WHERE id=1', header)
        expect('SELECT count(*) FROM items WHERE id=2', '0', 'a')
        expect('SELECT count(*) FROM items WHERE id=2', '1', 'b')
        sql("UPDATE items SET value='b' WHERE id=1; INSERT INTO items VALUES(1001,'only-b')", 'b')
        expect('SELECT ctid,xmin,xmax FROM items WHERE id=1', header)
        sql("UPDATE items SET value='main-later' WHERE id=1; DELETE FROM items WHERE id=3; INSERT INTO items VALUES(1002,'main-only')")
        expect('SELECT value FROM items WHERE id=1', 'main-later')
        expect('SELECT value FROM items WHERE id=1', 'a', 'a')
        expect('SELECT value FROM items WHERE id=1', 'b', 'b')
        expect('SELECT count(*) FROM items WHERE id=1002', '0', 'a')
        expect('SELECT count(*) FROM items WHERE id=3', '1', 'b')
        sql("BEGIN; UPDATE items SET value='rollback' WHERE id=1; INSERT INTO other VALUES(1,'no'); ROLLBACK", 'a')
        expect('SELECT value FROM items WHERE id=1', 'a', 'a')
        expect('SELECT count(*) FROM other', '0', 'a')
        sql("BEGIN; SAVEPOINT s; UPDATE items SET value='aborted-child' WHERE id=1; ROLLBACK TO s; UPDATE items SET value='after-savepoint' WHERE id=4; COMMIT", 'a')
        expect('SELECT value FROM items WHERE id=1', 'a', 'a')
        expect('SELECT value FROM items WHERE id=4', 'after-savepoint', 'a')
        # A later failing statement rolls back all row replacements in that statement.
        sql("UPDATE items SET value = (10 / (id-2))::text WHERE id IN (1,2)", 'b', '22012')
        expect('SELECT value FROM items WHERE id=1', 'b', 'b')
        # RETURNING and multiple commands inside one transaction.
        out = sql("BEGIN; INSERT INTO other VALUES(10,'own') RETURNING value; UPDATE other SET value='own-updated' WHERE id=10 RETURNING value; SELECT value FROM other; COMMIT", 'a')
        assert 'own-updated' in out, out
        expect('SELECT value FROM other', 'own-updated', 'a')
        expect('SELECT count(*) FROM other', '0', 'b')
        sql("UPDATE large_items SET value=repeat(value,2) WHERE id=1", 'a')
        expect('SELECT length(value) FROM large_items', '128000', 'a')
        expect('SELECT md5(value) FROM large_items', large_hash, 'b')
        expect('SELECT md5(value) FROM large_items', large_hash)
        print('PASS: writable main, child DML, own writes, rollback and savepoints', flush=True)
        # Concurrent sibling writes should not share a writer lock or leak uncommitted rows.
        writer = background("BEGIN; UPDATE items SET value='a-concurrent' WHERE id=5; INSERT INTO other VALUES(20,'pending'); SELECT pg_sleep(2); COMMIT", 'a_writer', 'a')
        wait_event('a_writer', 'PgSleep')
        expect('SELECT count(*) FROM other WHERE id=20', '0', 'a')
        sql("SET statement_timeout='700ms'; UPDATE items SET value='b-concurrent' WHERE id=5", 'b')
        sql("SET statement_timeout='700ms'; UPDATE items SET value='main-concurrent' WHERE id=5")
        finish(writer)
        for branch, value in [('a','a-concurrent'), ('b','b-concurrent'), ('main','main-concurrent')]:
            expect('SELECT value FROM items WHERE id=5', value, branch)
        # Same-branch stale UPDATE must fail instead of losing a committed change.
        writer = background("BEGIN; UPDATE items SET value='first' WHERE id=6; SELECT pg_sleep(2); COMMIT", 'same_writer', 'a')
        wait_event('same_writer', 'PgSleep')
        sql("UPDATE items SET value='stale' WHERE id=6", 'a', '40001')
        finish(writer)
        expect('SELECT value FROM items WHERE id=6', 'first', 'a')
        print('PASS: actual concurrent siblings/main and same-branch stale-write rejection', flush=True)
        # Branch context immutable; unsupported paths fail closed.
        sql("SET branch='b'", 'a', '55P02')
        sql("BEGIN; PREPARE TRANSACTION 'unsupported'", 'a', '0A000')
        sql("INSERT INTO items VALUES(9000,'unsupported') ON CONFLICT DO NOTHING", 'a', '0A000')
        sql("SELECT * FROM items TABLESAMPLE SYSTEM (100)", 'a', '0A000')
        sql('SELECT * FROM items', 'unknown', '42704')
        sql('BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT * FROM items', 'a', '0A000')
        sql('SELECT * FROM items FOR UPDATE', 'a', '0A000')
        sql('CREATE INDEX bad ON items(id)', 'a', '0A000')
        sql('ALTER TABLE items ADD COLUMN bad text', 'a', '0A000')
        sql('TRUNCATE items', 'a', '0A000')
        sql('DROP TABLE items', 'a', '0A000')
        sql('VACUUM items', 'a', '0A000')
        sql("COPY items FROM STDIN", 'a', '0A000')
        sql("BEGIN; SELECT count(*) FROM items; SELECT branch_create('upgrade')", 'main', '0A000')
        # A further sibling inherits main's current state, not earlier fork's state.
        sql("SELECT branch_create('c')")
        expect('SELECT value FROM items WHERE id=1', 'main-later', 'c')
        expect('SELECT count(*) FROM items WHERE id=1002', '1', 'c')
        # Fork must wait for a writer and then include the writer's committed change.
        writer = background("BEGIN; UPDATE items SET value='before-fork' WHERE id=7; SELECT pg_sleep(2); COMMIT", 'fork_writer')
        wait_event('fork_writer', 'PgSleep')
        fork = background("SELECT branch_create('d')", 'fork_waiter')
        wait_event('fork_waiter', 'advisory')
        finish(writer)
        finish(fork)
        expect('SELECT value FROM items WHERE id=7', 'before-fork', 'd')
        expect('SELECT value FROM items WHERE id=7', 'base-7', 'a')
        print('PASS: unsupported operations rejected, later fork boundary and active-writer barrier', flush=True)
        snapshots = {b: sql('SELECT jsonb_agg(t ORDER BY id) FROM items t', b) for b in ('main','a','b','c','d')}
        sql("INSERT INTO other VALUES(99,'durable-wal')", 'b')
        command('pg_ctl', '-D', data, '-m', 'immediate', '-w', 'stop')
        running = False
        start()
        running = True
        for branch, snapshot in snapshots.items():
            expect('SELECT jsonb_agg(t ORDER BY id) FROM items t', snapshot, branch)
        expect('SELECT value FROM other WHERE id=99', 'durable-wal', 'b')
        expect('SELECT count(*) FROM other WHERE id=99', '0', 'a')
        expect('SELECT length(value) FROM large_items', '128000', 'a')
        expect('SELECT md5(value) FROM large_items', large_hash, 'b')
        print('PASS: branch context, metadata and tuple versions survive crash recovery', flush=True)
    finally:
        if running:
            command('pg_ctl', '-D', data, '-m', 'immediate', '-w', 'stop')
