#!/usr/bin/env python3
"""Non-superuser branch binding and metadata privilege acceptance."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'../native_m5'))
from cluster import Cluster
from db import Client, DatabaseError
from urllib.parse import parse_qs, urlencode, urlsplit
import subprocess

parser=argparse.ArgumentParser()
parser.add_argument('--bindir',required=True)
args=parser.parse_args()
with Cluster(args.bindir) as cluster:
    def sql(query,branch='main',user=None,error=None,selector=True):
        options=parse_qs(urlsplit(cluster.dsn).query)
        options={key:value[0] for key,value in options.items()}
        if user: options['user']=user
        if selector: options['options']='-c branch='+branch
        dsn='postgresql:///postgres?'+urlencode(options,quote_via=__import__('urllib.parse',fromlist=['quote']).quote)
        r=subprocess.run([str(cluster.bindir/'psql'),dsn,'-XAt','-v','ON_ERROR_STOP=1','-v','VERBOSITY=verbose','-c',query],capture_output=True,text=True,timeout=15)
        if error: assert r.returncode and error in r.stderr,(query,r.stdout,r.stderr)
        else: assert r.returncode==0,(query,r.stdout,r.stderr)
        return r.stdout.strip()
    sql('CREATE TABLE users(id integer PRIMARY KEY,name text); INSERT INTO users VALUES(1,\'base\')')
    sql("SELECT branch_create('a'); SELECT branch_create('b')")
    sql('CREATE ROLE migrate_a LOGIN; CREATE ROLE app_a LOGIN; CREATE ROLE app_b LOGIN; CREATE ROLE unbound LOGIN')
    # Enable new format only after the baseline fixture has been seeded.
    sql('CREATE TABLE branch_native.format(version integer PRIMARY KEY); INSERT INTO branch_native.format VALUES(1); CREATE TABLE branch_native.roles(role oid PRIMARY KEY,branch bigint REFERENCES branch_native.branches(id)); REVOKE ALL ON branch_native.format,branch_native.roles FROM PUBLIC')
    sql("INSERT INTO branch_native.roles SELECT r.oid,b.id FROM pg_roles r CROSS JOIN branch_native.branches b WHERE (r.rolname IN ('migrate_a','app_a') AND b.name='a') OR (r.rolname='app_b' AND b.name='b')")
    sql('GRANT CREATE ON SCHEMA public TO migrate_a')
    sql('ALTER TABLE users OWNER TO migrate_a','a')
    sql('GRANT SELECT,INSERT,UPDATE,DELETE ON users TO app_a','a')
    sql('GRANT SELECT,INSERT,UPDATE,DELETE ON users TO app_b','b')
    assert sql("SHOW branch",user='app_a',selector=False)=='a'
    sql('SELECT 1',branch='b',user='app_a',error='does not match')
    sql('SELECT 1',user='app_a',error='does not match')
    sql('SELECT 1',user='unbound',selector=False,error='not bound')
    sql("UPDATE users SET name='a' WHERE id=1",'a','app_a')
    sql("UPDATE users SET name='b' WHERE id=1",'b','app_b')
    assert sql('SELECT name FROM users','a','app_a')=='a'
    assert sql('SELECT name FROM users','b','app_b')=='b'
    assert sql('SELECT name FROM users')=='base'
    sql('ALTER TABLE users ADD COLUMN bio text; CREATE TABLE notes(id integer PRIMARY KEY,body text)','a','migrate_a')
    sql('SELECT bio FROM users','b','app_b',error='42703')
    sql('CREATE TABLE denied(id integer)','a','app_a',error='42501')
    for query in ('SELECT * FROM branch_native.versions','SELECT * FROM branch_native.roles','SELECT branch_create(\'escape\')','SELECT branch_gc()','SET ROLE migrate_a',"SELECT nextval('branch_native.version_ids')",'SET SESSION AUTHORIZATION migrate_a'):
        sql(query,'a','app_a',error='42501')
    sql("SET branch='b'",'a','app_a',error='55P02')
    dsn=cluster.dsn.replace('user='+__import__('getpass').getuser(), 'user=app_a')
    with Client(cluster.bindir,dsn,'a').connect() as connection:
        connection.execute('DISCARD ALL')
        assert connection.execute('SHOW branch')[0]['branch']=='a'
        # Force an error inside the elevated metadata INSERT and verify that
        # neither identity nor search_path escapes the engine operation.
        last=sql('SELECT last_value FROM branch_native.version_ids')
        sql("SELECT setval('branch_native.version_ids',1,false)")
        try:
            connection.execute("INSERT INTO users VALUES(99,'collision')")
            raise AssertionError('metadata collision should fail')
        except DatabaseError as error:
            assert error.sqlstate=='23505',error
        finally:
            sql("SELECT setval('branch_native.version_ids',"+last+",true)")
        assert connection.execute('SELECT current_user AS who')[0]['who']=='app_a'
        sql('GRANT app_b TO app_a')
        connection.execute('SET ROLE app_b')
        assert connection.execute('SHOW branch')[0]['branch']=='a'
        connection.execute('RESET ROLE')
        assert connection.execute('SHOW branch')[0]['branch']=='a'
        try:
            connection.execute('SELECT * FROM branch_native.versions')
            raise AssertionError('metadata access should remain forbidden')
        except DatabaseError as error:
            assert error.sqlstate=='42501',error
    sql('PREPARE q AS SELECT name FROM users; EXECUTE q','a','app_a')
    sql("SET search_path=public,pg_catalog; UPDATE users SET name='safe' WHERE id=1",'a','app_a')
    assert sql('COPY (SELECT name FROM users) TO STDOUT','a','app_a')=='safe'
    sql('VACUUM FREEZE')
    assert sql('SELECT name FROM users','a','app_a')=='safe'
    cluster.stop('immediate');cluster.start()
    assert sql('SELECT name FROM users',user='app_a',selector=False)=='safe'
    sql('UPDATE branch_native.format SET version=2')
    sql('SELECT 1',error='incompatible native branch metadata format')
    print('PASS: role binding, separate migration/application privileges, metadata denial, immutable context, maintenance and recovery')
