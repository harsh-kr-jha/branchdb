#!/usr/bin/env python3
"""Dependency-free integration tests; creates and stops a disposable cluster."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument('--bindir', default=subprocess.check_output(['pg_config', '--bindir'], text=True).strip())
args = parser.parse_args()
bindir = Path(args.bindir)
here = Path(__file__).resolve().parent

with tempfile.TemporaryDirectory(prefix='branch-mvcc-', dir='/tmp') as tmp:
    data = Path(tmp) / 'data'
    env = dict(os.environ, PGHOST=tmp, PGPORT='58439', PGDATABASE='postgres', PGUSER=os.environ.get('USER', 'postgres'))
    # Ignore external connection configuration; all traffic uses this private socket.
    env.pop('PGSERVICE', None)
    env.pop('PGOPTIONS', None)
    def command(program, *argv):
        return subprocess.run([str(bindir / program), *map(str, argv)], env=env,
                              text=True, capture_output=True, check=True)
    def sql(query, error=None):
        result = subprocess.run([str(bindir / 'psql'), '-XAt', '-v', 'ON_ERROR_STOP=1',
                                 '-v', 'VERBOSITY=verbose', '-c', query],
                                env=env, text=True, capture_output=True, timeout=20)
        if error:
            assert result.returncode and error in result.stderr, (query, result)
        else:
            assert result.returncode == 0, (query, result.stderr)
        return result.stdout.strip()
    def expect(query, value):
        actual = sql(query)
        assert actual == value, (query, actual, value)
    def background(query, name):
        return subprocess.Popen([str(bindir / 'psql'), '-XAt', '-v', 'ON_ERROR_STOP=1',
                                 '-v', 'VERBOSITY=verbose', '-c', query],
                                env=dict(env, PGAPPNAME=name), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def sleeping(name):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if sql(f"SELECT count(*) FROM pg_stat_activity WHERE application_name='{name}' AND wait_event='PgSleep'") == '1':
                return
            time.sleep(.03)
        raise AssertionError(f'{name} did not reach synchronization point')
    def finished(process):
        out, err = process.communicate(timeout=12)
        assert process.returncode == 0, (out, err)
    running = False
    try:
        command('initdb', '-D', data, '-A', 'trust', '--no-locale', '-E', 'UTF8')
        options = f"-k {tmp} -p 58439 -c listen_addresses='' -c max_connections=12 -c shared_buffers=16MB"
        def start():
            command('pg_ctl', '-D', data, '-l', Path(tmp) / 'server.log', '-o', options, '-w', 'start')
        start()
        running = True
        print(sql('SELECT version()'))
        command('psql', '-X', '-v', 'ON_ERROR_STOP=1', '-f', here / 'branch_mvcc.sql')
        sql("INSERT INTO branch_mvcc.base_items SELECT n, jsonb_build_object('v',n) FROM generate_series(1,1000) n")
        # A transaction with an old snapshot must still see the sealing trigger.
        old = background("BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT pg_current_snapshot(); SELECT pg_sleep(2); INSERT INTO branch_mvcc.base_items VALUES (9999,'{}'); COMMIT", 'old_snapshot')
        sleeping('old_snapshot')
        sql("SELECT branch_mvcc.create_branch('branch_a'); SELECT branch_mvcc.create_branch('branch_b')")
        _, err = old.communicate(timeout=8)
        assert old.returncode and '55000' in err, err
        expect('SELECT count(*) FROM branch_mvcc.delta', '0')
        expect('SELECT count(*) FROM branch_a.items', '1000')
        sql("SET search_path=branch_a,public; UPDATE items SET payload='{}' WHERE id=1; DELETE FROM items WHERE id=2; INSERT INTO items VALUES(1001,'{}')")
        expect("SELECT payload FROM branch_b.items WHERE id=1", '{"v": 1}')
        expect('SELECT count(*) FROM branch_b.items WHERE id IN (2,1001)', '1')
        expect('SELECT count(*) FROM branch_mvcc.delta', '3')
        sql("UPDATE branch_b.items SET payload='\"b\"' WHERE id=1; INSERT INTO branch_b.items VALUES(1001,'\"b\"')")
        expect('SELECT payload FROM branch_a.items WHERE id=1001', '{}')
        sql("BEGIN; UPDATE branch_a.items SET payload='null' WHERE id=1; DELETE FROM branch_a.items WHERE id=3; ROLLBACK")
        expect('SELECT payload FROM branch_a.items WHERE id=1', '{}')
        expect('SELECT count(*) FROM branch_a.items WHERE id=3', '1')
        sql("INSERT INTO branch_a.items VALUES(1,'{}')", '23505')
        sql("INSERT INTO branch_a.items VALUES(NULL,'{}')", '23502')
        sql("UPDATE branch_a.items SET id=2000 WHERE id=1", '0A000')
        sql("UPDATE branch_a.items SET payload=NULL WHERE id=1", '23502')
        sql('TRUNCATE branch_mvcc.base_items', '55000')
        sql("INSERT INTO branch_mvcc.base_items VALUES(9999,'{}')", '55000')
        sql("BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT branch_mvcc.create_branch('bad')", '0A000')
        sql("SELECT branch_mvcc.create_branch('pg_invalid')", 'P0001')
        sql("BEGIN; SELECT branch_mvcc.create_branch('rolled_back'); ROLLBACK")
        expect("SELECT to_regnamespace('rolled_back') IS NULL", 't')
        # Tombstones hide base rows; reinsertion replaces the tombstone.
        sql("INSERT INTO branch_a.items VALUES(2,'\"reborn\"')")
        expect('SELECT payload FROM branch_a.items WHERE id=2', '"reborn"')
        expect('SELECT payload FROM branch_b.items WHERE id=2', '{"v": 2}')
        # Same-branch stale UPDATE fails rather than silently losing a write.
        writer = background("BEGIN; UPDATE branch_a.items SET payload='\"first\"' WHERE id=3; SELECT pg_sleep(2); COMMIT", 'writer')
        sleeping('writer')
        # Independent branch must make progress while a's metadata row is locked.
        sql("SET statement_timeout='500ms'; UPDATE branch_b.items SET payload='\"independent\"' WHERE id=3")
        sql("UPDATE branch_a.items SET payload='\"stale\"' WHERE id=3", '40001')
        finished(writer)
        expect('SELECT payload FROM branch_a.items WHERE id=3', '"first"')
        # Concurrent inserts of the same branch/key cannot both commit.
        writer = background("BEGIN; INSERT INTO branch_a.items VALUES(2000,'{}'); SELECT pg_sleep(2); COMMIT", 'insert_writer')
        sleeping('insert_writer')
        sql("INSERT INTO branch_a.items VALUES(2000,'{}')", '23505')
        finished(writer)
        # REPEATABLE READ retains its view; a stale writer fails on metadata UPDATE.
        reader = background("BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT payload FROM branch_a.items WHERE id=4; SELECT pg_sleep(2); SELECT payload FROM branch_a.items WHERE id=4; UPDATE branch_a.items SET payload='{}' WHERE id=4; COMMIT", 'rr_reader')
        sleeping('rr_reader')
        sql("UPDATE branch_a.items SET payload='\"new\"' WHERE id=4")
        out, err = reader.communicate(timeout=8)
        assert reader.returncode and '40001' in err and out.count('{"v": 4}') == 2, (out, err)
        expected_a = sql('SELECT jsonb_agg(t ORDER BY id) FROM branch_a.items t')
        expected_b = sql('SELECT jsonb_agg(t ORDER BY id) FROM branch_b.items t')
        sql('VACUUM (FREEZE, ANALYZE) branch_mvcc.base_items')
        sql('VACUUM (FREEZE, ANALYZE) branch_mvcc.delta')
        expect('SELECT jsonb_agg(t ORDER BY id) FROM branch_a.items t', expected_a)
        expect('SELECT jsonb_agg(t ORDER BY id) FROM branch_b.items t', expected_b)
        # Include a fresh committed WAL record after vacuum, then simulate a crash.
        sql("INSERT INTO branch_a.items VALUES(3000,'\"wal\"')")
        command('pg_ctl', '-D', data, '-m', 'immediate', '-w', 'stop')
        running = False
        start()
        running = True
        expect('SELECT payload FROM branch_a.items WHERE id=3000', '"wal"')
        expect('SELECT count(*) FROM branch_b.items WHERE id=3000', '0')
        sql('DELETE FROM branch_a.items WHERE id=3000')
        expect('SELECT jsonb_agg(t ORDER BY id) FROM branch_a.items t', expected_a)
        expect('SELECT jsonb_agg(t ORDER BY id) FROM branch_b.items t', expected_b)
        expect('SELECT count(*) FROM branch_mvcc.base_items', '1000')
        print('PASS: sharing, DML isolation, SQL routing, rollback, constraints, sealing, concurrent writers, repeatable read, VACUUM FREEZE, crash recovery')
    finally:
        if running:
            command('pg_ctl', '-D', data, '-m', 'immediate', '-w', 'stop')
