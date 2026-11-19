#!/usr/bin/env python3
"""Native schema evolution, FK, sequence, upsert and recovery acceptance."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent/'../native_m5'))
from cluster import Cluster
from db import DatabaseError
HERE=Path(__file__).resolve().parent
parser=argparse.ArgumentParser();parser.add_argument('--bindir',required=True)
args=parser.parse_args()
with Cluster(args.bindir,setup=HERE/'setup.sql') as cluster:
    def sql(query,branch='main',error=None):
        with cluster.client(branch).connect() as db:
            try: result=db.execute(query)
            except DatabaseError as failure:
                assert error==failure.sqlstate,(query,failure.sqlstate,str(failure))
                return
            assert not error,(query,'expected '+str(error))
            return result
    def scalar(query,branch='main'):return next(iter(sql(query,branch)[0].values()))
    sql("CREATE SCHEMA AUTHORIZATION CURRENT_USER",error='0A000')
    sql("CREATE TABLE parents(id serial PRIMARY KEY,name text UNIQUE NOT NULL,body text,score int DEFAULT 5 CHECK(score>=0))")
    sql("CREATE TABLE children(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,parent int REFERENCES parents(id) ON UPDATE CASCADE ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED)")
    sql("INSERT INTO parents(name,body) SELECT 'base',string_agg(md5(n::text),'') FROM generate_series(1,2000)n")
    sql('INSERT INTO children(parent) VALUES(1)')
    sql('ALTER SEQUENCE children_id_seq RENAME TO custom_identity_seq')
    digest=scalar('SELECT md5(body) FROM parents')
    original=scalar("SELECT pg_relation_filenode('parents')")
    for branch in ('a','b'):sql("SELECT branch_create('"+branch+"')")
    for branch in ('a','b'):assert scalar("SELECT pg_relation_filenode('parents')",branch)==original
    for branch in ('main','a','b'):
        assert scalar("INSERT INTO parents(name) VALUES('independent') RETURNING id",branch)=='2'
        assert scalar('INSERT INTO children(parent) VALUES(2) RETURNING id',branch)=='2'
    other_sequence=scalar("SELECT 'custom_identity_seq'::regclass::oid",'b')
    sql("CREATE TABLE rejected_default(id int DEFAULT nextval('"+other_sequence+"'::regclass))",'a',error='42501')
    print('PASS: fork shares heap; serial and identity counters are independent',flush=True)
    sql("ALTER TABLE parents ADD COLUMN priority integer NOT NULL DEFAULT 7",'a')
    sql('ALTER TABLE parents ADD COLUMN sibling_layout text','b')
    sql("UPDATE parents SET sibling_layout=repeat('different',1000) WHERE id=1",'b')
    sql('CREATE INDEX parents_priority ON parents(priority)','a')
    sql('CREATE UNIQUE INDEX invalid_rank_unique ON parents(priority)','a',error='23505')
    with cluster.client('a').connect() as db:
        db.execute('SET enable_seqscan=off')
        assert db.execute('SELECT count(*) AS n FROM parents WHERE priority=7')[0]['n']=='2' 
    assert scalar('SELECT priority FROM parents WHERE id=1','a')=='7'
    assert scalar("SELECT pg_relation_filenode('parents')",'a')==original
    assert scalar("SELECT pg_relation_filenode('parents')",'b')==original
    sql('ALTER TABLE parents DROP COLUMN body','a')
    assert scalar('SELECT md5(body) FROM parents WHERE id=1','b')==digest
    sql('ALTER TABLE parents ADD COLUMN optional text','a')
    sql('ALTER TABLE parents ALTER COLUMN optional SET NOT NULL','a',error='23502')
    sql("UPDATE parents SET optional='validated'",'a')
    sql('ALTER TABLE parents ALTER COLUMN optional SET NOT NULL','a')
    sql('ALTER TABLE parents ALTER COLUMN optional DROP NOT NULL','a')
    sql('ALTER TABLE parents ADD CONSTRAINT invalid_check CHECK(score<5)','a',error='23514')
    sql('ALTER TABLE parents ALTER COLUMN score TYPE bigint','a',error='0A000')
    sql('ALTER TABLE parents RENAME COLUMN priority TO rank','a')
    sql('ALTER TABLE parents ALTER COLUMN rank SET DEFAULT 9','a')
    assert scalar("INSERT INTO parents(name) VALUES('default') RETURNING rank",'a')=='9'
    sql('ALTER TABLE parents ALTER COLUMN rank DROP DEFAULT','a')
    sql("INSERT INTO parents(name) VALUES('missing')",'a',error='23502')
    sql("INSERT INTO parents(name,score,rank) VALUES('bad',-1,1)",'a',error='23514')
    sql('INSERT INTO children(parent) VALUES(999)','a',error='23503')
    with cluster.client('a').connect() as db:
        db.execute('BEGIN')
        db.execute('INSERT INTO children(parent) VALUES(99)')
        db.execute("INSERT INTO parents(id,name,rank) VALUES(99,'deferred',1)")
        db.execute('COMMIT')
    sql('UPDATE parents SET id=100 WHERE id=99','a')
    assert scalar('SELECT parent FROM children WHERE parent=100','a')=='100'
    sql('DELETE FROM parents WHERE id=100','a')
    assert scalar('SELECT count(*) FROM children WHERE parent=100','a')=='0'
    def upsert(_):sql("INSERT INTO parents(name,rank,score) VALUES('upsert',1,1) ON CONFLICT(name) DO UPDATE SET score=parents.score+1",'a')
    with ThreadPoolExecutor(max_workers=4) as workers:list(workers.map(upsert,range(8)))
    assert scalar("SELECT score FROM parents WHERE name='upsert'",'a')=='8'
    print('PASS: isolated schema evolution, defaults, checks, deferred FK/cascade and concurrent upsert',flush=True)
    sql('TRUNCATE parents','b',error='2BP01')
    sql('DROP TABLE parents','b',error='2BP01')
    with cluster.client('b').connect() as db:
        db.execute('BEGIN')
        db.execute('TRUNCATE parents RESTART IDENTITY CASCADE')
        db.execute('ROLLBACK')
    assert scalar('SELECT count(*) FROM parents','b')=='2'
    sql('TRUNCATE parents RESTART IDENTITY CASCADE','b')
    assert scalar('SELECT count(*) FROM children','b')=='0'
    assert scalar("INSERT INTO parents(name) VALUES('reset') RETURNING id",'b')=='1'
    assert scalar('SELECT count(*) FROM parents')=='2'
    sql('DROP TABLE parents CASCADE','b')
    sql('INSERT INTO children(parent) VALUES(999)','b')
    sql("SELECT branch_create('after_schema')")
    sql('ALTER TABLE parents DROP COLUMN body')
    assert scalar('SELECT md5(body) FROM parents WHERE id=1','after_schema')==digest
    sql("SELECT branch_create('after_drop')")
    assert scalar('SELECT score FROM parents WHERE id=1','after_drop')=='5'
    sql('CREATE TABLE extra_child(id int REFERENCES parents(id))')
    sql('DROP TABLE extra_child')
    sql('DELETE FROM parents WHERE id=2')
    sql('DROP SEQUENCE parents_id_seq',error='2BP01')
    sql('DROP SEQUENCE custom_identity_seq CASCADE',error='2BP01')
    sql('CREATE SEQUENCE standalone CACHE 4')
    sql("SELECT nextval('standalone')")
    sql('DROP SEQUENCE standalone')
    sql('CREATE TABLE rollback_rows(id int PRIMARY KEY,body text)')
    sql("INSERT INTO rollback_rows VALUES(1,repeat('retained',10000))")
    old=scalar("SELECT pg_relation_filenode('rollback_rows')")
    with cluster.client().connect() as db:
        db.execute('BEGIN')
        db.execute('ALTER TABLE rollback_rows DROP COLUMN body')
        db.execute('ROLLBACK')
    assert scalar("SELECT pg_relation_filenode('rollback_rows')")==old
    assert scalar('SELECT length(body) FROM rollback_rows')=='80000'
    sql('ALTER TABLE parents ADD COLUMN fast_default int DEFAULT 17')
    sql("SELECT branch_create('after_fast_default')")
    assert scalar('SELECT fast_default FROM parents WHERE id=1','after_fast_default')=='17'
    with cluster.client().connect() as writer:
        writer.execute('BEGIN');writer.execute("SELECT nextval('parents_id_seq')")
        with ThreadPoolExecutor(max_workers=1) as workers:
            fork=workers.submit(sql,"SELECT branch_create('sequence_fork')")
            time.sleep(.15);assert not fork.done(),'sequence-only writer must hold fork admission'
            writer.execute('COMMIT');fork.result(timeout=10)
    print('PASS: FK-aware DROP/TRUNCATE, schema rollback, fast defaults and sequence fork barrier',flush=True)
    sql("DROP SCHEMA public CASCADE",'a')
    assert scalar('SELECT count(*) FROM parents')=='1'
    assert scalar("SELECT count(*) FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind IN ('r','S')",'a')=='0'
    cluster.stop('immediate');cluster.start()
    assert scalar('SELECT count(*) FROM parents')=='1'
    assert scalar('SELECT md5(body) FROM parents WHERE id=1','after_schema')==digest
    sql("SELECT branch_drop('a')");sql("SELECT branch_drop('b')")
    sql('VACUUM')
    assert scalar('SELECT md5(body) FROM parents WHERE id=1','after_schema')==digest
    print('PASS: isolated reset, crash recovery and retained TOAST after maintenance',flush=True)
