#!/usr/bin/env python3
"""Native branch retirement, connection fencing and storage generation tests."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit, quote
sys.path.insert(0,str(Path(__file__).resolve().parent/'../native_m5'))
from cluster import Cluster
from db import Client, DatabaseError

HERE=Path(__file__).resolve().parent
parser=argparse.ArgumentParser()
parser.add_argument('--bindir',required=True)
args=parser.parse_args()
with Cluster(args.bindir,setup=HERE/'setup.sql') as cluster:
    def sql(query,branch='main',error=None):
        with cluster.client(branch).connect() as db:
            try:
                result=db.execute(query)
            except DatabaseError as failure:
                assert error and failure.sqlstate==error,(query,failure.sqlstate,str(failure))
                return
            assert not error,(query,'expected '+str(error))
            return result
    def scalar(query,branch='main'):
        return next(iter(sql(query,branch)[0].values()))
    def expect_error(query,branch,code):sql(query,branch,error=code)
    def wait(predicate,message):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.03)
        raise AssertionError(message)
    sql('CREATE TABLE users(id integer PRIMARY KEY,name text)')
    sql("INSERT INTO users VALUES(1,'base'),(2,'shared')")
    sql('CREATE TABLE docs(id integer PRIMARY KEY,body text)')
    sql("INSERT INTO docs SELECT 1,string_agg(md5(n::text),'') FROM generate_series(1,2000)n")
    digest=scalar('SELECT md5(body) FROM docs')
    for branch in ('a','b'):sql('SELECT branch_create($$'+branch+'$$)')
    print('PASS: format 2 bootstrap and registered branch sessions',flush=True)
    expect_error("SELECT branch_drop('main')",'main','22023')
    with cluster.client('b').connect() as public_locker:
        public_locker.execute('SELECT pg_advisory_lock('+str(0x42524e43<<32)+'::bigint)')
        assert scalar('SELECT name FROM users WHERE id=1')=='base'
    live=cluster.client('a').connect()
    expect_error("SELECT branch_drop('a')",'main','55006')
    live.execute('DISCARD ALL')
    live.execute('SELECT pg_advisory_unlock_all()')
    try: live.execute('SELECT 1/0')
    except DatabaseError: pass
    expect_error("SELECT branch_drop('a')",'main','55006')
    live.close()
    # Transactional retirement rolls back without losing data/catalogs.
    with cluster.client().connect() as db:
        db.execute('BEGIN')
        db.execute("SELECT branch_drop('a')")
        db.execute('ROLLBACK')
    assert scalar('SELECT name FROM users WHERE id=1','a')=='base'
    sql("UPDATE users SET name='a' WHERE id=1",'a')
    sql('ALTER TABLE users ADD COLUMN bio text','a')
    sql("UPDATE users SET bio='private' WHERE id=1",'a')
    sql('CREATE TABLE private_rows(id integer PRIMARY KEY,body text)','a')
    sql("INSERT INTO private_rows SELECT 1,repeat(md5('private'),2000)",'a')
    private_path=scalar("SELECT pg_relation_filepath('private_rows')",'a')
    old_id=scalar("SELECT id FROM branch_native.branches WHERE name='a'")
    sql('CREATE ROLE old_a LOGIN')
    sql("INSERT INTO branch_native.roles SELECT r.oid,b.id FROM pg_roles r,branch_native.branches b WHERE r.rolname='old_a' AND b.name='a'")
    # Force terminates an active uncommitted writer, not just idle sessions.
    live=cluster.client('a').connect()
    live.execute('BEGIN');live.execute("UPDATE users SET name='uncommitted' WHERE id=2")
    sql("SELECT branch_drop('a',true)")
    try:
        live.execute('SELECT 1');raise AssertionError('forced session survived')
    except DatabaseError:pass
    finally:live.close()
    assert scalar("SELECT count(*) FROM branch_native.branches WHERE name='a'")=='0'
    assert scalar('SELECT name FROM users WHERE id=1','b')=='base'
    assert scalar('SELECT md5(body) FROM docs','b')==digest
    sql('VACUUM FREEZE')
    sql('SELECT branch_cleanup()')
    sql('CHECKPOINT')
    assert not (cluster.data/private_path).exists(),private_path
    sql("SELECT branch_create('a')")
    assert scalar("SELECT id FROM branch_native.branches WHERE name='a'")!=old_id
    assert scalar('SELECT name FROM users WHERE id=1','a')=='base'
    # Reusing a name does not rebind the deleted branch's credentials.
    parsed=urlsplit(cluster.dsn);params={k:v[0] for k,v in parse_qs(parsed.query).items()};params['user']='old_a'
    try:
        Client(cluster.bindir,'postgresql:///postgres?'+urlencode(params,quote_via=quote),'a').connect()
        raise AssertionError('stale credentials attached to recreated branch')
    except DatabaseError:pass
    print('PASS: default refusal, unlock-resistant fences, transactional deletion, force rollback, cleanup and name reuse',flush=True)
    # DROP hides only this branch; GC retains the source and sibling TOAST.
    with cluster.client('a').connect() as prepared:
        prepared.execute('PREPARE held_plan AS SELECT body FROM docs')
        sql('DROP TABLE docs','a')
        try:prepared.execute('EXECUTE held_plan');raise AssertionError('cached plan should revalidate after DROP')
        except DatabaseError as failure:assert failure.sqlstate=='42P01'
    assert scalar("SELECT to_regclass('docs') IS NULL",'a')=='t'
    assert scalar('SELECT md5(body) FROM docs','b')==digest
    sql('VACUUM FREEZE')
    assert scalar('SELECT md5(body) FROM docs')==digest
    with cluster.client('b').connect() as db:
        db.execute('BEGIN');db.execute('DROP TABLE docs');db.execute('ROLLBACK')
        assert db.execute('SELECT md5(body) AS hash FROM docs')[0]['hash']==digest
    # A canonical main drop retains the birth descriptor needed by old children.
    sql('DROP TABLE docs')
    assert scalar('SELECT md5(body) FROM docs','b')==digest
    sql('VACUUM FREEZE')
    assert scalar('SELECT md5(body) FROM docs','b')==digest
    print('PASS: branch-local DROP and retained canonical tuple/TOAST descriptors',flush=True)
    sql('CREATE TABLE truncation(id integer PRIMARY KEY,body text)')
    sql("INSERT INTO truncation SELECT 1,string_agg(md5(n::text),'') FROM generate_series(1,2000)n")
    sql("SELECT branch_create('c')")
    sql('SELECT branch_cleanup()')
    before=scalar("SELECT oid FROM pg_class WHERE oid='truncation'::regclass")
    index_before=scalar("SELECT oid FROM pg_class WHERE oid='truncation_pkey'::regclass")
    checksum=scalar('SELECT md5(body) FROM truncation')
    original_generation=scalar("SELECT relfilenode FROM pg_class WHERE oid='truncation'::regclass")
    original_storage=scalar('SELECT id FROM branch_native.storage WHERE root='+before+' AND generation='+original_generation)
    with cluster.client().connect() as db:
        db.execute('BEGIN');db.execute('TRUNCATE truncation');db.execute('ROLLBACK')
        assert db.execute('SELECT md5(body) AS hash FROM truncation')[0]['hash']==checksum
    sql('TRUNCATE truncation')
    assert scalar('SELECT count(*) FROM truncation')=='0'
    assert scalar('SELECT md5(body) FROM truncation','c')==checksum
    assert scalar("SELECT oid FROM pg_class WHERE oid='truncation'::regclass")==before
    assert scalar("SELECT oid FROM pg_class WHERE oid='truncation_pkey'::regclass")==index_before
    assert scalar('SELECT id FROM branch_native.storage WHERE generation='+original_generation)==original_storage
    assert scalar('SELECT root FROM branch_native.storage WHERE id='+original_storage)!=before
    sql("INSERT INTO truncation VALUES(1,'new generation')")
    assert scalar('SELECT body FROM truncation')=='new generation'
    assert scalar('SELECT md5(body) FROM truncation','c')==checksum
    sql("SELECT branch_create('generation_two')")
    sql('TRUNCATE truncation')
    sql("INSERT INTO truncation VALUES(1,'new generation')")
    assert scalar('SELECT body FROM truncation','generation_two')=='new generation'
    assert scalar('SELECT md5(body) FROM truncation','c')==checksum
    sql('VACUUM FREEZE')
    assert scalar('SELECT md5(body) FROM truncation','c')==checksum
    # Truncate an inherited alias independently as well.
    sql('TRUNCATE truncation','c')
    sql("INSERT INTO truncation VALUES(1,'child generation')",'c')
    assert scalar('SELECT body FROM truncation')=='new generation'
    assert scalar('SELECT body FROM truncation','c')=='child generation'
    sql('VACUUM FREEZE')
    print('PASS: stable table/index OIDs, transactional TRUNCATE and independent retained generations',flush=True)
    cluster.stop('immediate');cluster.start()
    assert scalar('SELECT body FROM truncation')=='new generation'
    assert scalar('SELECT body FROM truncation','c')=='child generation'
    assert scalar('SELECT md5(body) FROM docs','b')==digest
    sql('VACUUM FREEZE')
    print('PASS: retired descriptors, cleanup queue and storage generations survive recovery',flush=True)
    # An uncommitted deletion fences connecting clients until rollback, then
    # admits them again; a committed deletion makes waiting startup fail.
    sql("SELECT branch_create('racer')")
    racer_id=scalar("SELECT id FROM branch_native.branches WHERE name='racer'")
    for commit in (False,True):
        with cluster.client().connect() as deleting, ThreadPoolExecutor(max_workers=1) as executor:
            deleting.execute('BEGIN');deleting.execute("SELECT branch_drop('racer')")
            def connect_racer():
                try:
                    with cluster.client('racer').connect() as session:
                        return session.execute('SHOW branch')[0]['branch']
                except DatabaseError:return 'rejected'
            incoming=executor.submit(connect_racer)
            wait(lambda: scalar("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND classid=1112690243 AND objid="+racer_id+" AND objsubid=5 AND NOT granted")=='1','startup did not wait on deletion fence')
            assert not incoming.done()
            deleting.execute('COMMIT' if commit else 'ROLLBACK')
            assert incoming.result(timeout=10)==('rejected' if commit else 'racer')
    print('PASS: connection startup races with deletion commit and rollback',flush=True)
    # Standalone index retirement preserves a sibling's inherited index.
    sql('CREATE TABLE indexed(id integer PRIMARY KEY,code integer)')
    sql('CREATE INDEX code_lookup ON indexed(code)')
    sql('INSERT INTO indexed VALUES(1,7)')
    sql("SELECT branch_create('indexed_child')")
    sql('DROP INDEX code_lookup')
    assert scalar("SELECT to_regclass('code_lookup') IS NULL")=='t'
    assert scalar("SELECT to_regclass('code_lookup') IS NOT NULL",'indexed_child')=='t'
    expect_error('DROP INDEX indexed_pkey','main','2BP01')
    sql('VACUUM FREEZE')
    assert scalar('SELECT id FROM indexed WHERE code=7','indexed_child')=='1'
    # Reject modifying a relation held by a live cursor in this session.
    with cluster.client().connect() as db:
        db.execute('BEGIN');db.execute('DECLARE held CURSOR FOR SELECT * FROM indexed')
        try:
            db.execute('TRUNCATE indexed');raise AssertionError('cursor should prevent truncate')
        except DatabaseError as failure: assert failure.sqlstate=='55006'
        db.execute('ROLLBACK')
    print('PASS: standalone index DROP, constraint dependencies and active cursor protection',flush=True)
    # Crash at three uncommitted boundaries. All committed branch data must
    # survive, and the durable queue must remain safely retryable.
    def crash_transaction(branch,queries):
        connection=cluster.client(branch).connect()
        connection.execute('BEGIN')
        for query in queries: connection.execute(query)
        cluster.stop('immediate')
        connection.close()
        cluster.start()
    sql("SELECT branch_create('crash_drop')")
    sql('CREATE TABLE crash_private(id integer PRIMARY KEY,body text)','crash_drop')
    sql("INSERT INTO crash_private VALUES(1,'retained')",'crash_drop')
    crash_transaction('main',["SELECT branch_drop('crash_drop')"])
    assert scalar('SELECT body FROM crash_private','crash_drop')=='retained'
    old_file=scalar("SELECT pg_relation_filepath('indexed')")
    crash_transaction('main',['TRUNCATE indexed'])
    assert scalar('SELECT code FROM indexed')=='7'
    assert scalar("SELECT pg_relation_filepath('indexed')")==old_file
    private_file=scalar("SELECT pg_relation_filepath('crash_private')",'crash_drop')
    sql("SELECT branch_drop('crash_drop')")
    queued=scalar('SELECT count(*) FROM branch_native.cleanup')
    crash_transaction('main',['SELECT branch_gc()','SELECT branch_cleanup()'])
    assert scalar('SELECT count(*) FROM branch_native.cleanup')==queued
    assert (cluster.data/private_file).exists()
    sql('VACUUM FREEZE');sql('SELECT branch_cleanup()');sql('CHECKPOINT')
    assert not (cluster.data/private_file).exists()
    assert scalar('SELECT md5(body) FROM docs','b')==digest
    assert scalar('SELECT body FROM truncation','c')=='child generation'
    # A target with no sessions still cannot bypass an active sibling transaction.
    # The engine bounds admission waits instead of holding a connection fence forever.
    sql("SELECT branch_create('timeout_target')")
    with cluster.client('b').connect() as holder, cluster.client().connect() as deleting:
        holder.execute('BEGIN');holder.execute('SELECT id FROM users')
        deleting.execute("SET statement_timeout='15s'")
        started=time.monotonic()
        try:
            deleting.execute("SELECT branch_drop('timeout_target')")
            raise AssertionError('active sibling transaction should prevent deletion')
        except DatabaseError as failure:
            assert failure.sqlstate=='55P03',(failure.sqlstate,str(failure))
            assert 9 <= time.monotonic()-started < 14
        holder.execute('ROLLBACK')
    assert scalar("SELECT count(*) FROM branch_native.branches WHERE name='timeout_target'")=='1'
    sql("SELECT branch_drop('timeout_target')")
    print('PASS: bounded deletion admission and retry after a blocking transaction',flush=True)
    # CLI deletion uses the same default and explicit force semantics.
    sql("SELECT branch_create('cli_drop')")
    cli=subprocess.run([sys.executable,'-B',str(HERE/'../native_m5/branches.py'),'--bindir',str(cluster.bindir),'--dsn',cluster.dsn,'drop','cli_drop'],capture_output=True,text=True,check=True)
    assert json.loads(cli.stdout)=={'dropped':'cli_drop'}
    sql('VACUUM FREEZE')
    # Ordinary branch-bound migration roles can retire their own tables and
    # truncate inherited tables after an explicit ownership grant.
    sql('CREATE ROLE m7_migrate LOGIN');sql('CREATE ROLE m7_app LOGIN')
    sql('GRANT CREATE ON SCHEMA public TO m7_migrate')
    sql("INSERT INTO branch_native.roles SELECT r.oid,b.id FROM pg_roles r,branch_native.branches b WHERE r.rolname IN ('m7_migrate','m7_app') AND b.name='b'")
    sql('ALTER TABLE users OWNER TO m7_migrate','b')
    sql('GRANT SELECT,INSERT,UPDATE,DELETE ON users TO m7_app','b')
    def role_client(role):
        params={k:v[0] for k,v in parse_qs(urlsplit(cluster.dsn).query).items()};params['user']=role
        return Client(cluster.bindir,'postgresql:///postgres?'+urlencode(params,quote_via=quote),'b')
    with role_client('m7_app').connect() as application:
        for command in ('DROP TABLE users','TRUNCATE users'):
            try:application.execute(command);raise AssertionError('application DDL should fail')
            except DatabaseError as failure:assert failure.sqlstate=='42501'
    with role_client('m7_migrate').connect() as migration:
        migration.execute('TRUNCATE users')
        assert migration.execute('SELECT count(*) AS n FROM users')[0]['n']=='0'
        migration.execute('CREATE TABLE migration_owned(id integer PRIMARY KEY,body text)')
        migration.execute("INSERT INTO migration_owned VALUES(1,'keep')")
        migration.execute('BEGIN');migration.execute("UPDATE migration_owned SET body='changed'");migration.execute('DROP TABLE migration_owned');migration.execute('ROLLBACK')
        assert migration.execute('SELECT body FROM migration_owned')[0]['body']=='keep'
        migration.execute('DROP TABLE migration_owned')
    assert scalar('SELECT name FROM users WHERE id=1')=='base'
    sql('VACUUM FREEZE')
    print('PASS: branch-bound migration ownership, application DDL denial and transactional retirement after writes',flush=True)
    # Real format-2 autovacuum must retry queued retirement automatically.
    sql("SELECT branch_create('automatic')")
    sql('CREATE TABLE auto_private(id integer PRIMARY KEY,body text)','automatic')
    sql("INSERT INTO auto_private VALUES(1,repeat(md5('auto'),2000))",'automatic')
    automatic_oid=scalar("SELECT oid FROM pg_class WHERE oid='auto_private'::regclass",'automatic')
    automatic_file=scalar("SELECT pg_relation_filepath('auto_private')",'automatic')
    sql("SELECT branch_drop('automatic')")
    assert scalar('SELECT count(*) FROM branch_native.cleanup WHERE object='+automatic_oid)=='1'
    cluster.stop()
    cluster.options=cluster.options.replace('autovacuum=off','autovacuum=on')+' -c autovacuum_naptime=1s -c autovacuum_vacuum_threshold=0 -c autovacuum_vacuum_scale_factor=0 -c autovacuum_vacuum_insert_threshold=0 -c autovacuum_vacuum_insert_scale_factor=0'
    cluster.start()
    deadline=time.monotonic()+30
    while scalar('SELECT count(*) FROM branch_native.cleanup WHERE object='+automatic_oid)!='0':
        assert time.monotonic()<deadline,'autovacuum did not process retirement queue'
        time.sleep(.1)
    sql('CHECKPOINT')
    assert not (cluster.data/automatic_file).exists()
    assert scalar('SELECT md5(body) FROM docs','b')==digest
    print('PASS: real format-2 autovacuum retries the cleanup queue and reclaims private files',flush=True)
    log=(cluster.root/'server.log').read_text()
    for bad in ('failed Assert','Segmentation fault','relcache reference leak','TupleDesc reference leak','buffer refcount leak'):
        assert bad not in log,bad
    print('PASS: crash rollback at deletion, TRUNCATE and cleanup boundaries; idempotent reclamation and CLI deletion',flush=True)

