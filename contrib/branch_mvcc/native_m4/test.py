#!/usr/bin/env python3
"""Native branch-heap integration tests. Standard library; disposable cluster."""
import argparse
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument('--bindir', required=True)
args = parser.parse_args()
bin_dir = Path(args.bindir).resolve()
here = Path(__file__).resolve().parent

with tempfile.TemporaryDirectory(prefix='branch-native-m4-', dir='/tmp') as tmp:
    data = Path(tmp) / 'data'
    env = dict(os.environ, PGHOST=tmp, PGPORT='58444', PGDATABASE='postgres')
    for var in ('PGSERVICE', 'PGOPTIONS', 'PGUSER'):
        env.pop(var, None)
    def command(tool, *argv):
        return subprocess.run([str(bin_dir / tool), *map(str, argv)], env=env,
                              capture_output=True, text=True, check=True)
    def sql(query, branch='main', error=None, database='postgres', timeout=20):
        result = subprocess.run([str(bin_dir / 'psql'), '-XAt', '-v', 'ON_ERROR_STOP=1',
                                 '-v', 'VERBOSITY=verbose', '-c', query],
                                env=dict(env, PGOPTIONS=f'-c branch={branch}', PGDATABASE=database),
                                capture_output=True, text=True, timeout=timeout,
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
        options = f"-k {tmp} -p 58444 -c listen_addresses='' -c max_connections=16 -c shared_buffers=16MB -c autovacuum=off"
        def start():
            command('pg_ctl', '-D', data, '-l', Path(tmp) / 'server.log', '-o', options, '-w', 'start')
        start()
        running = True
        print(sql('SELECT version()'), flush=True)
        command('psql', '-X', '-v', 'ON_ERROR_STOP=1', '-f', here / '../native_m3/setup.sql')
        sql("CREATE TABLE users(id bigint PRIMARY KEY,name text)")
        expect("SELECT reloptions @> ARRAY['autovacuum_enabled=false'] FROM pg_class WHERE oid='users'::regclass",'t')
        sql("CREATE TABLE migrations(version text PRIMARY KEY)")
        sql("CREATE TABLE fixed(id int PRIMARY KEY,amount int)")
        sql("INSERT INTO fixed VALUES(1,10)")
        sql("CREATE TABLE growth(id bigint PRIMARY KEY,payload text)")
        sql("CREATE TABLE documents(id int PRIMARY KEY,body text)")
        sql("INSERT INTO documents SELECT 1,string_agg(md5(n::text),'') FROM generate_series(1,2000) n")
        document_hash=sql('SELECT md5(body) FROM documents')
        sql("INSERT INTO users VALUES(1,'Harsh'),(2,'Shared')")
        sql("INSERT INTO migrations VALUES('base')")
        original = sql("SELECT oid,relfilenode FROM pg_class WHERE relname='users' AND relnamespace='public'::regnamespace")
        versions = sql('SELECT count(*) FROM branch_native.versions')
        sql("SELECT branch_create('a'); SELECT branch_create('b')")
        expect('SELECT count(*) FROM branch_native.versions', versions)
        for branch in ('a','b'):
            descriptor = sql("SELECT oid,relfilenode FROM pg_class WHERE relname='users' AND relnamespace='public'::regnamespace",branch)
            assert descriptor.split('|')[0] != original.split('|')[0], (branch, descriptor,original)
            assert descriptor.split('|')[1] == original.split('|')[1], (branch,descriptor,original)
            expect('SELECT name FROM users WHERE id=1', 'Harsh',branch)
        print('PASS: branch-owned catalog identities with shared physical files',flush=True)
        sql("ALTER TABLE users ADD COLUMN avatar_url text; UPDATE users SET name='A',avatar_url='avatar' WHERE id=1",'a')
        sql("ALTER TABLE users ADD COLUMN subscription_plan integer; UPDATE users SET name='B',subscription_plan=42 WHERE id=1",'b')
        expect('SELECT name,avatar_url FROM users WHERE id=1','A|avatar','a')
        expect('SELECT name,subscription_plan FROM users WHERE id=1','B|42','b')
        expect('SELECT name FROM users WHERE id=1','Harsh')
        sql("CREATE TABLE notes(id bigint PRIMARY KEY,body text); INSERT INTO notes VALUES(1,'A')",'a')
        sql("CREATE TABLE notes(id bigint PRIMARY KEY,priority integer); INSERT INTO notes VALUES(1,7)",'b')
        expect('SELECT body FROM notes WHERE id=1','A','a')
        expect('SELECT priority FROM notes WHERE id=1','7','b')
        expect("SELECT to_regclass('public.notes') IS NULL",'t')
        print('PASS: independent ADD COLUMN types and same-name tables',flush=True)
        def columns(branch):
            return sql("SELECT column_name||':'||data_type FROM information_schema.columns WHERE table_schema='public' AND table_name='users' ORDER BY ordinal_position",branch)
        assert columns('main') == 'id:bigint\nname:text'
        assert columns('a') == 'id:bigint\nname:text\navatar_url:text'
        assert columns('b') == 'id:bigint\nname:text\nsubscription_plan:integer'
        for branch in ('main','a','b'):
            expect("SELECT count(*) FROM pg_class WHERE relnamespace='public'::regnamespace AND relname='users'",'1',branch)
            expect("SELECT count(*) FROM pg_type WHERE typnamespace='public'::regnamespace AND typname='users'",'1',branch)
            expect("SELECT count(*) FROM pg_constraint WHERE conrelid='users'::regclass AND contype='p'",'1',branch)
            expect("SELECT count(*) FROM pg_index WHERE indrelid='users'::regclass",'1',branch)
        expect("SELECT count(*) FROM pg_attribute WHERE attname='avatar_url'",'0','b')
        sql('SELECT avatar_url FROM users','b','42703')
        sql('SELECT subscription_plan FROM users','a','42703')
        # Same SQL names are distinct catalog objects and physical new tables.
        note_a=sql("SELECT oid,relfilenode FROM pg_class WHERE oid='notes'::regclass",'a')
        note_b=sql("SELECT oid,relfilenode FROM pg_class WHERE oid='notes'::regclass",'b')
        assert note_a.split('|')[0] != note_b.split('|')[0] and note_a.split('|')[1] != note_b.split('|')[1]
        print('PASS: pg_catalog and information_schema reflect each branch',flush=True)
        # Parallel transactional migrations, with main querying throughout.
        observer=background("PREPARE main_query AS SELECT name FROM users WHERE id=1; SELECT pg_sleep(2); EXECUTE main_query",'main_observer')
        wait_event('main_observer','PgSleep')
        writer=background("BEGIN; ALTER TABLE users ADD COLUMN bio text; CREATE TABLE activity(id int PRIMARY KEY,body text); UPDATE users SET bio='A-bio' WHERE id=2; INSERT INTO migrations VALUES('a-001'); SELECT pg_sleep(2); COMMIT",'migration_a','a')
        wait_event('migration_a','PgSleep')
        sql("SET statement_timeout='900ms'; BEGIN; ALTER TABLE users ADD COLUMN credits integer; CREATE TABLE activity(id int PRIMARY KEY,points int); UPDATE users SET credits=7 WHERE id=2; INSERT INTO migrations VALUES('b-001'); COMMIT",'b')
        expect('SELECT name FROM users WHERE id=1','Harsh')
        finish(writer)
        assert 'Harsh' in finish(observer)
        expect('SELECT bio FROM users WHERE id=2','A-bio','a')
        expect('SELECT credits FROM users WHERE id=2','7','b')
        expect('SELECT version FROM migrations ORDER BY version','a-001\nbase','a')
        expect('SELECT version FROM migrations ORDER BY version','b-001\nbase','b')
        expect('SELECT version FROM migrations ORDER BY version','base')
        print('PASS: simultaneous independent migrations and migration-history records with main querying',flush=True)
        # Transactional DDL and catalog rollback, including subtransactions.
        sql("BEGIN; ALTER TABLE users ADD COLUMN rolled_back text; CREATE TABLE discarded(id int); UPDATE users SET rolled_back='bad' WHERE id=1; ROLLBACK",'a')
        expect("SELECT to_regclass('discarded') IS NULL",'t','a')
        sql('SELECT rolled_back FROM users','a','42703')
        sql("BEGIN; SAVEPOINT s; CREATE TABLE savepoint_table(id int); ROLLBACK TO s; CREATE TABLE savepoint_table(id int PRIMARY KEY); COMMIT",'a')
        expect("SELECT count(*) FROM pg_class WHERE relname='savepoint_table' AND relnamespace='public'::regnamespace",'1','a')
        # A failed fork must not delete source or shared files.
        object_count=sql('SELECT count(*) FROM branch_native.objects')
        sql("BEGIN; SELECT branch_create('aborted_fork'); ROLLBACK")
        expect("SELECT count(*) FROM branch_native.branches WHERE name='aborted_fork'",'0')
        expect('SELECT count(*) FROM branch_native.objects',object_count)
        expect('SELECT name FROM users WHERE id=1','Harsh')
        expect('SELECT name FROM users WHERE id=1','A','a')
        # A plan prepared against A's descriptor must revalidate after A DDL.
        out=sql("PREPARE own_query AS SELECT name FROM users WHERE id=1; ALTER TABLE users ADD COLUMN fresh text; EXECUTE own_query; DEALLOCATE own_query",'a')
        assert 'A' in out,out
        sql('SELECT fresh FROM users','b','42703')
        import ctypes
        candidates=[bin_dir.parent/'lib'/'libpq.dylib',bin_dir.parent/'lib'/'libpq.so',bin_dir.parent/'lib64'/'libpq.so']
        lib=ctypes.CDLL(str(next(path for path in candidates if path.exists())))
        ptr=ctypes.c_void_p; text=ctypes.c_char_p; integer=ctypes.c_int
        lib.PQconnectdb.argtypes=[text];lib.PQconnectdb.restype=ptr
        lib.PQstatus.argtypes=[ptr];lib.PQstatus.restype=integer
        lib.PQerrorMessage.argtypes=[ptr];lib.PQerrorMessage.restype=text
        lib.PQprepare.argtypes=[ptr,text,text,integer,ptr];lib.PQprepare.restype=ptr
        lib.PQexecPrepared.argtypes=[ptr,text,integer,ctypes.POINTER(text),ptr,ptr,integer];lib.PQexecPrepared.restype=ptr
        lib.PQresultStatus.argtypes=[ptr];lib.PQresultStatus.restype=integer
        lib.PQresultErrorMessage.argtypes=[ptr];lib.PQresultErrorMessage.restype=text
        lib.PQgetvalue.argtypes=[ptr,integer,integer];lib.PQgetvalue.restype=text
        lib.PQclear.argtypes=[ptr];lib.PQclear.restype=None
        lib.PQfinish.argtypes=[ptr];lib.PQfinish.restype=None
        connection=lib.PQconnectdb(f"host={tmp} port=58444 dbname=postgres options='-c branch=a'".encode())
        try:
            assert lib.PQstatus(connection)==0,lib.PQerrorMessage(connection)
            result=lib.PQprepare(connection,b'extended_plan',b'SELECT name FROM users WHERE id=$1',1,None)
            try: assert lib.PQresultStatus(result)==1,lib.PQresultErrorMessage(result)
            finally: lib.PQclear(result)
            sql('ALTER TABLE users ADD COLUMN extended_refresh text','a')
            values=(text*1)(b'1')
            result=lib.PQexecPrepared(connection,b'extended_plan',1,values,None,None,0)
            try:
                assert lib.PQresultStatus(result)==2,lib.PQresultErrorMessage(result)
                assert lib.PQgetvalue(result,0,0)==b'A'
            finally: lib.PQclear(result)
        finally: lib.PQfinish(connection)
        print('PASS: transactional DDL/fork rollback, savepoints and prepared-plan invalidation',flush=True)
        # DDL in the same branch coordinates rather than creating duplicate names.
        writer=background("BEGIN; CREATE TABLE same_branch(id int); SELECT pg_sleep(2); COMMIT",'ddl_same','a')
        wait_event('ddl_same','PgSleep')
        sql('CREATE TABLE same_branch(id int)','a','42P07')
        finish(writer)
        writer=background("BEGIN; CREATE TABLE retry_name(id int); SELECT pg_sleep(2); ROLLBACK",'ddl_abort','a')
        wait_event('ddl_abort','PgSleep')
        sql('CREATE TABLE retry_name(id int PRIMARY KEY)','a')
        finish(writer)
        expect("SELECT count(*) FROM pg_class WHERE relname='retry_name' AND relnamespace='public'::regnamespace",'1','a')
        # Main changes schema/data after the earlier forks.
        sql("ALTER TABLE users ADD COLUMN admin_flag boolean; UPDATE users SET name='main-later',admin_flag=true WHERE id=1; CREATE TABLE notes(id bigint PRIMARY KEY,admin text); INSERT INTO notes VALUES(1,'main-note'); CREATE TABLE parent_only(id int)")
        expect('SELECT name,admin_flag FROM users WHERE id=1','main-later|t')
        expect('SELECT name,avatar_url FROM users WHERE id=1','A|avatar','a')
        expect('SELECT name,subscription_plan FROM users WHERE id=1','B|42','b')
        expect("SELECT to_regclass('parent_only') IS NULL",'t','a')
        sql('SELECT admin_flag FROM users','b','42703')
        sql("SELECT branch_create('c')")
        expect('SELECT name,admin_flag FROM users WHERE id=1','main-later|t','c')
        expect('SELECT admin FROM notes WHERE id=1','main-note','c')
        expect("SELECT to_regclass('activity') IS NULL",'t','c')
        # Fork's command snapshot predates the migration commit. Refreshing
        # both ownership and schema snapshots after the barrier is essential.
        writer=background("BEGIN; ALTER TABLE users ADD COLUMN verified_cutoff text; UPDATE users SET verified_cutoff='at-fork' WHERE id=2; CREATE TABLE cutoff_table(id int PRIMARY KEY); INSERT INTO cutoff_table VALUES(1); SELECT pg_sleep(2); COMMIT",'ddl_fork_writer')
        wait_event('ddl_fork_writer','PgSleep')
        fork=background("SELECT branch_create('d')",'ddl_fork_waiter')
        wait_event('ddl_fork_waiter','advisory')
        finish(writer);finish(fork)
        expect('SELECT verified_cutoff FROM users WHERE id=2','at-fork','d')
        expect('SELECT id FROM cutoff_table','1','d')
        expect("SELECT to_regclass('cutoff_table') IS NULL",'t','c')
        sql('SELECT verified_cutoff FROM users','c','42703')
        print('PASS: same-branch DDL conflicts, writable main and atomic schema/data fork boundary',flush=True)
        sql('ALTER TABLE fixed ADD COLUMN details text','a')
        sql("UPDATE fixed SET details=(SELECT body FROM documents WHERE id=1)",'a')
        expect('SELECT length(details) FROM fixed','64000','a')
        sql('SELECT details FROM fixed','b','42703')
        sql('UPDATE documents SET body=repeat(body,2) WHERE id=1','a')
        sql("UPDATE documents SET body='B'||body WHERE id=1",'b')
        expect('SELECT md5(body) FROM documents',document_hash)
        expect('SELECT length(body) FROM documents','128000','a')
        expect('SELECT length(body) FROM documents','64001','b')
        sql("INSERT INTO users(id,name) VALUES(1,'conflict')",'a','23505')
        sql("INSERT INTO users(id,name) VALUES(99,'only-a')",'a')
        sql("INSERT INTO users(id,name) VALUES(99,'only-b')",'b')
        expect('SELECT name FROM users WHERE id=99','only-a','a')
        expect('SELECT name FROM users WHERE id=99','only-b','b')
        for query in ('CREATE TABLE bad_options(id int) WITH (fillfactor=50)','DROP TABLE users','TRUNCATE users','ALTER TABLE users ALTER COLUMN name TYPE int',"ALTER TABLE users ADD COLUMN bad text DEFAULT 'x'",'VACUUM users','CREATE INDEX new_column_index ON users(avatar_url)'):
            sql(query,'a','0A000')
        # Distinct logical OIDs share heap/index extension locks by root.
        growers={b:background(f"INSERT INTO growth SELECT n,repeat(md5('{b}'||n::text),40) FROM generate_series(1,200) n",'grow_'+b,b) for b in ('main','a','b')}
        for process in growers.values(): finish(process)
        for b in growers:
            expect('SELECT count(*),sum(id) FROM growth','200|20100',b)
            expect(f"SELECT bool_and(payload=repeat(md5('{b}'||id::text),40)) FROM growth",'t',b)
            sql("INSERT INTO growth VALUES(1,'duplicate')",b,'23505')
        print('PASS: concurrent shared heap/index file extension with independent keys',flush=True)
        snapshots={b:sql('SELECT jsonb_agg(t ORDER BY id) FROM users t',b) for b in ('main','a','b','c','d')}
        schemas={b:columns(b) for b in ('main','a','b','c','d')}
        for n in range(3):
            sql(f"UPDATE documents SET body=(SELECT string_agg(md5((g+{n*2000})::text),'') FROM generate_series(1,2000) g) WHERE id=1",'a')
        retained_body=sql('SELECT md5(body) FROM documents','a')
        sql('UPDATE documents SET id=10 WHERE id=1; UPDATE documents SET id=1 WHERE id=10','a')
        for n in range(12):
            sql(f"UPDATE users SET name='obsolete-{n}' WHERE id=1",'a')
        snapshots['a']=sql('SELECT jsonb_agg(t ORDER BY id) FROM users t','a')
        before=int(sql('SELECT count(*) FROM branch_native.versions'))
        sql('BEGIN; SELECT branch_gc(); ROLLBACK')
        expect('SELECT count(*) FROM branch_native.versions',str(before))
        reclaimed=int(sql('SELECT branch_gc()'))
        assert reclaimed>=11,reclaimed
        expect('SELECT count(*) FROM branch_native.versions',str(before-reclaimed))
        for b in snapshots:
            expect('SELECT jsonb_agg(t ORDER BY id) FROM users t',snapshots[b],b)
        # Aborting chunk collection must leave those chunks collectable again.
        rollback_chunks=sql('BEGIN; SELECT branch_gc_toast(); ROLLBACK').splitlines()
        assert int(rollback_chunks[1])>0,rollback_chunks
        chunks=int(sql('SELECT branch_gc_toast()'))
        assert chunks==int(rollback_chunks[1]),(chunks,rollback_chunks)
        assert chunks>0,chunks
        expect('SELECT md5(body) FROM documents',retained_body,'a')
        sql('VACUUM (FREEZE) users')
        sql('SELECT branch_maintain()')
        expect("SELECT relallfrozen>0 FROM pg_class WHERE oid='users'::regclass",'t')
        for b in snapshots:
            expect('SELECT jsonb_agg(t ORDER BY id) FROM users t',snapshots[b],b)
        expect('SELECT branch_gc()','0')
        sql("UPDATE users SET name='after-gc' WHERE id=1",'a')
        expect('SELECT name FROM users WHERE id=1','after-gc','a')
        snapshots['a']=sql('SELECT jsonb_agg(t ORDER BY id) FROM users t','a')
        print(f'PASS: GC rollback, {reclaimed} unreachable versions collected, native freeze sweep and branch retention',flush=True)
        before_crash=int(sql('SELECT count(*) FROM branch_native.versions'))
        interrupted=background("BEGIN; SELECT branch_gc(); SELECT pg_sleep(20); COMMIT",'interrupted_gc')
        wait_event('interrupted_gc','PgSleep')
        command('pg_ctl','-D',data,'-m','immediate','-w','stop')
        running=False
        interrupted.communicate(timeout=8)
        assert interrupted.returncode != 0
        start();running=True
        expect('SELECT count(*) FROM branch_native.versions',str(before_crash))
        for b in snapshots:
            expect('SELECT jsonb_agg(t ORDER BY id) FROM users t',snapshots[b],b)
            assert columns(b)==schemas[b],(b,columns(b),schemas[b])
        expect('SELECT body FROM notes WHERE id=1','A','a')
        expect('SELECT priority FROM notes WHERE id=1','7','b')
        expect('SELECT admin FROM notes WHERE id=1','main-note','c')
        expect('SELECT md5(body) FROM documents',document_hash)
        expect('SELECT length(body) FROM documents','64000','a')
        expect('SELECT length(body) FROM documents','64001','b')
        expect('SELECT length(details) FROM fixed','64000','a')
        expect('SELECT amount FROM fixed','10','b')
        for b in ('main','a','b'):
            expect('SELECT count(*),sum(id) FROM growth','200|20100',b)
            expect(f"SELECT bool_and(payload=repeat(md5('{b}'||id::text),40)) FROM growth",'t',b)
        assert int(sql('SELECT branch_gc()'))>=1
        # A collector's outer SQL snapshot predates its wait for a writer.
        # It must refresh after admission or it could delete the writer's newly
        # committed TOAST chunks as apparently unreachable.
        writer=background("BEGIN; UPDATE documents SET body=body WHERE id=1; SELECT pg_sleep(2); COMMIT",'toast_gc_writer','a')
        wait_event('toast_gc_writer','PgSleep')
        collector=background('SELECT branch_gc_toast()','waiting_toast_gc')
        wait_event('waiting_toast_gc','advisory')
        finish(writer);finish(collector)
        expect('SELECT md5(body) FROM documents',retained_body,'a')
        print('PASS: maintenance waits for active writes and refreshes TOAST reachability after admission',flush=True)
        # The file collector retains every live catalog alias and mapped file.
        # Its unlink is intentionally nontransactional, so explicit transactions
        # and transactions that already wrote are rejected.
        sql('BEGIN; SELECT branch_gc_files(); COMMIT', error='25001')
        sql('BEGIN READ ONLY; SELECT branch_gc(); COMMIT', error='25006')
        db_dir=data/'base'/sql("SELECT oid FROM pg_database WHERE datname=current_database()")
        orphan=db_dir/'2147482000'
        assert not orphan.exists()
        orphan.write_bytes(bytes(8192))
        marker=db_dir/'branch_test_marker'
        marker.write_text('not a PostgreSQL relation file')
        assert int(sql('SELECT branch_gc_files()'))>=1
        sql('CHECKPOINT')
        assert not orphan.exists() and marker.exists()
        marker.unlink()
        # Crash while fork catalogs have been created but not committed. Their
        # source files must survive; any abandoned empty files can be swept.
        interrupted=background("BEGIN; SELECT branch_create('crash_fork'); SELECT pg_sleep(20); COMMIT",'interrupted_fork')
        wait_event('interrupted_fork','PgSleep')
        command('pg_ctl','-D',data,'-m','immediate','-w','stop');running=False
        interrupted.communicate(timeout=8)
        assert interrupted.returncode != 0
        start();running=True
        expect("SELECT count(*) FROM branch_native.branches WHERE name='crash_fork'",'0')
        sql('VACUUM FREEZE')
        sql('CHECKPOINT')
        for b in snapshots:
            expect('SELECT jsonb_agg(t ORDER BY id) FROM users t',snapshots[b],b)
            assert columns(b)==schemas[b]
        print('PASS: orphan file collection, guarded unlink and uncommitted fork crash recovery',flush=True)

        # Exercise a real launcher/worker, rather than calling the worker hook.
        for n in range(6):
            sql(f"UPDATE users SET name='auto-{n}' WHERE id=1",'a')
        snapshots['a']=sql('SELECT jsonb_agg(t ORDER BY id) FROM users t','a')
        before_auto=int(sql('SELECT count(*) FROM branch_native.versions'))
        manual_options=options
        command('pg_ctl','-D',data,'-m','fast','-w','stop');running=False
        options=options.replace('autovacuum=off','autovacuum=on')+' -c autovacuum_naptime=1s -c autovacuum_vacuum_threshold=0 -c autovacuum_vacuum_scale_factor=0 -c autovacuum_vacuum_insert_threshold=0 -c autovacuum_vacuum_insert_scale_factor=0'
        start();running=True
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            if int(sql('SELECT count(*) FROM branch_native.versions'))<before_auto:
                break
            time.sleep(.25)
        else:
            raise AssertionError('native autovacuum worker did not collect obsolete versions')
        for b in snapshots:
            expect('SELECT jsonb_agg(t ORDER BY id) FROM users t',snapshots[b],b)
        command('pg_ctl','-D',data,'-m','fast','-w','stop');running=False
        options=manual_options
        start();running=True
        print('PASS: automatic branch-aware GC and maintenance through a real autovacuum worker',flush=True)

        # Cross a real pg_xact segment boundary using committed subtransactions.
        # The helper is built into this disposable directory and never installed.
        stress_dir=Path(tmp)/'stress';stress_dir.mkdir()
        shutil.copy(here/'xid_stress.c',stress_dir/'xid_stress.c')
        shutil.copy(here/'Makefile.stress',stress_dir/'Makefile')
        subprocess.run(['make',f'PG_CONFIG={bin_dir / "pg_config"}'],cwd=stress_dir,
                       capture_output=True,text=True,check=True)
        module=next(stress_dir.glob('xid_stress.*so'),None)
        if module is None: module=stress_dir/'xid_stress.dylib'
        assert module.exists(),list(stress_dir.iterdir())
        sql(f"CREATE FUNCTION branch_test_consume_xids(integer) RETURNS integer AS '{module}', 'branch_test_consume_xids' LANGUAGE C STRICT",database='template1')
        sql('VACUUM FREEZE')
        initial_xid=int(sql('SELECT pg_current_xact_id()'))
        old_segment=(initial_xid & 0xffffffff)//1048576
        old_clog=data/'pg_xact'/f'{old_segment:04X}'
        assert old_clog.exists(),old_clog
        assert sql('SELECT branch_test_consume_xids(1100000)',database='template1',timeout=120)=='1100000'
        new_xid=int(sql('SELECT pg_current_xact_id()'))
        assert new_xid-initial_xid>=1100000,(initial_xid,new_xid)
        sql('ALTER DATABASE template0 ALLOW_CONNECTIONS true',database='template1')
        sql('VACUUM FREEZE',database='template0')
        sql('VACUUM FREEZE',database='template1')
        sql('VACUUM FREEZE')
        sql('CHECKPOINT')
        assert not old_clog.exists(),f'old transaction status segment still pinned: {old_clog}'
        for b in snapshots:
            expect('SELECT jsonb_agg(t ORDER BY id) FROM users t',snapshots[b],b)
        print('PASS: 1,100,000 real subtransaction IDs, freezing and actual pg_xact segment retirement',flush=True)

        # Controlled epoch simulation ONLY on this fully frozen disposable
        # cluster. This is not a claim to have run 2**32 real transactions.
        full_xid=int(sql('SELECT pg_current_xact_id()'))
        epoch=(full_xid>>32)+1
        raw_xid=(full_xid & 0xffffffff)+4096
        command('pg_ctl','-D',data,'-m','fast','-w','stop');running=False
        command('pg_resetwal','-f','-e',epoch,'-x',raw_xid,data)
        start();running=True
        assert int(sql('SELECT pg_current_xact_id()'))>>32==epoch
        for b in snapshots:
            expect('SELECT jsonb_agg(t ORDER BY id) FROM users t',snapshots[b],b)
            assert columns(b)==schemas[b]
        expect('SELECT md5(body) FROM documents',retained_body,'a')
        sql("UPDATE users SET name='new-epoch' WHERE id=1",'a')
        expect("SELECT name FROM users WHERE id=1",'new-epoch','a')
        sql('VACUUM FREEZE')
        expect("SELECT name FROM users WHERE id=1",'new-epoch','a')
        sql("SELECT branch_create('after_epoch')")
        expect('SELECT name FROM users WHERE id=1','main-later','after_epoch')
        before_crash=int(sql('SELECT count(*) FROM branch_native.versions'))
        interrupted=background("BEGIN; UPDATE documents SET body=repeat('uncommitted',8000); UPDATE users SET name='uncommitted' WHERE id=1; SELECT pg_sleep(20); COMMIT",'interrupted_epoch_write','a')
        wait_event('interrupted_epoch_write','PgSleep')
        command('pg_ctl','-D',data,'-m','immediate','-w','stop');running=False
        interrupted.communicate(timeout=8)
        assert interrupted.returncode != 0
        start();running=True
        expect('SELECT count(*) FROM branch_native.versions',str(before_crash))
        expect('SELECT md5(body) FROM documents',retained_body,'a')
        expect('SELECT name FROM users WHERE id=1','new-epoch','a')
        expect('SELECT name FROM users WHERE id=1','main-later','after_epoch')
        sql('VACUUM FREEZE')
        print('PASS: controlled XID epoch transition preserves branch ownership, TOAST and subsequent writes',flush=True)
        server_log=(Path(tmp)/'server.log').read_text()
        for bad in ('relcache reference leak','TupleDesc reference leak','buffer refcount leak','failed Assert','Segmentation fault'):
            assert bad not in server_log,bad
        print('PASS: shared/new TOAST, inherited indexes, guarded storage lifetime and schema/data crash recovery',flush=True)
    except BaseException:
        log = Path(tmp) / 'server.log'
        if log.exists(): print(log.read_text()[-18000:],flush=True)
        raise
    finally:
        if running: command('pg_ctl','-D',data,'-m','immediate','-w','stop')
