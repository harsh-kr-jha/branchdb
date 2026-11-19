#!/usr/bin/env python3
"""Cache invalidation, statement snapshots and incremental writer-map tests."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'../native_m5'))
from cluster import Cluster
from db import DatabaseError
HERE=Path(__file__).resolve().parent
parser=argparse.ArgumentParser();parser.add_argument('--bindir',required=True)
args=parser.parse_args()
with Cluster(args.bindir,setup=HERE/'../native_m8/setup.sql') as cluster:
    def sql(query,branch='main'):
        with cluster.client(branch).connect() as db:return db.execute(query)
    def error(db,query,code):
        try:db.execute(query)
        except DatabaseError as failure:assert failure.sqlstate==code,(failure.sqlstate,str(failure))
        else:raise AssertionError('expected '+code+': '+query)
    sql('CREATE TABLE items(id int PRIMARY KEY,value int)')
    sql('INSERT INTO items SELECT n,n FROM generate_series(1,1000)n')
    sql("SELECT branch_create('a')");sql("SELECT branch_create('b')")
    with cluster.client('a').connect() as warm, cluster.client('a').connect() as writer:
        for _ in range(10):assert warm.execute('SELECT count(*) AS n FROM items')[0]['n']=='1000'
        writer.execute('CREATE TABLE remote_table(id int PRIMARY KEY)')
        assert warm.execute('SELECT count(*) AS n FROM remote_table')[0]['n']=='0'
        writer.execute('ALTER TABLE items ADD COLUMN remote_column text')
        assert warm.execute('SELECT remote_column FROM items WHERE id=1')[0]['remote_column'] is None
        writer.execute('DROP TABLE remote_table')
        error(warm,'SELECT * FROM remote_table','42P01')
        with warm.transaction():
            warm.execute('SAVEPOINT ddl')
            warm.execute('CREATE TABLE rolled_back(id int)')
            assert warm.execute('SELECT count(*) AS n FROM rolled_back')[0]['n']=='0'
            warm.execute('ROLLBACK TO ddl')
            error(warm,'SELECT * FROM rolled_back','42P01')
            warm.execute('ROLLBACK TO ddl')
        warm.execute('BEGIN')
        warm.execute('CREATE TABLE aborted_table(id int)')
        warm.execute('SELECT * FROM aborted_table')
        warm.execute('ROLLBACK')
        error(warm,'SELECT * FROM aborted_table','42P01')
    with cluster.client('b').connect() as db:error(db,'SELECT remote_column FROM items','42703')
    print('PASS: warm ownership cache observes remote DDL, retirement and aborted/subtransaction DDL',flush=True)
    # Both cursor paths retain a statement snapshot while another connection
    # commits. Later statements on that same connection see the new versions.
    for indexed in (False,True):
        with cluster.client('a').connect() as reader, cluster.client('a').connect() as writer:
            reader.execute('SET enable_seqscan='+('off' if indexed else 'on'))
            reader.execute('SET enable_indexscan='+('on' if indexed else 'off'))
            reader.execute('BEGIN')
            reader.execute('DECLARE old_view CURSOR FOR SELECT id,value FROM items WHERE id<=3 ORDER BY id')
            first=reader.execute('FETCH 1 FROM old_view')
            expected=int(first[0]['value'])
            writer.execute('UPDATE items SET value=value+10000 WHERE id<=3')
            tail=reader.execute('FETCH ALL FROM old_view')
            assert [int(row['value']) for row in tail]==[expected+1,expected+2]
            assert int(reader.execute('SELECT value FROM items WHERE id=1')[0]['value'])==expected+10000
            reader.execute('COMMIT')
    print('PASS: sequential/hash and targeted-index cursor snapshots survive concurrent commits',flush=True)
    with cluster.client('a').connect() as db:
        db.execute("SET statement_timeout='20s'")
        db.execute('CREATE TABLE second_table(id int PRIMARY KEY,value int)')
        db.execute('BEGIN')
        db.execute('INSERT INTO items SELECT n,n FROM generate_series(1001,11000)n')
        db.execute('INSERT INTO second_table VALUES(1,1)')
        db.execute('SAVEPOINT before_changes')
        db.execute('UPDATE items SET value=-1 WHERE id=11000')
        db.execute('DELETE FROM second_table WHERE id=1')
        db.execute('ROLLBACK TO before_changes')
        assert db.execute('SELECT value FROM items WHERE id=11000')[0]['value']=='11000'
        assert db.execute('SELECT value FROM second_table')[0]['value']=='1'
        error(db,'INSERT INTO items VALUES(11000,0)','23505')
        db.execute('ROLLBACK TO before_changes')
        db.execute('INSERT INTO second_table VALUES(1,9) ON CONFLICT(id) DO UPDATE SET value=excluded.value')
        db.execute('INSERT INTO items VALUES(11000,0) ON CONFLICT DO NOTHING')
        db.execute('COMMIT')
        assert db.execute('SELECT count(*) AS n FROM items')[0]['n']=='11000'
        assert db.execute('SELECT value FROM second_table')[0]['value']=='9'
    assert sql('SELECT count(*) AS n FROM items','b')[0]['n']=='1000'
    print('PASS: 10K-row native insert, multiple writer maps, savepoint/error recovery and both conflict actions',flush=True)

# Exercise a collation with a different mixed-case order. Index scan keys must
# use the metadata column's collation rather than ScanKeyInit's C default.
with Cluster(args.bindir,setup=HERE/'../native_m8/setup.sql') as cluster:
    with cluster.client().connect() as db:
        collations=db.execute("SELECT collname FROM pg_collation WHERE collprovider='c' AND collname LIKE 'en_US%' AND collencoding IN (-1,6) ORDER BY length(collname) LIMIT 1")
        if collations:
            collation=collations[0]['collname'].replace('"','""')
            db.execute('CREATE TABLE locale_rows(id int PRIMARY KEY)')
            db.execute('INSERT INTO locale_rows VALUES(1)')
            db.execute('ALTER TABLE branch_native.branches ALTER COLUMN name TYPE text COLLATE pg_catalog."'+collation+'"')
            for branch in ('a','A','b','B','z','Z'):db.execute("SELECT branch_create('"+branch+"')")
    if collations:
        for branch in ('main','a','A','b','B','z','Z'):
            with cluster.client(branch).connect() as db:
                assert db.execute('SELECT id FROM locale_rows')[0]['id']=='1'
        print('PASS: typed branch-name lookup follows non-C metadata index collation',flush=True)
    else:
        print('SKIP: no libc en_US collation installed for mixed-case index test',flush=True)
