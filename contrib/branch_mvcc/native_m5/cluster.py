"""Disposable local cluster/app lifecycle shared by the demo and acceptance test."""
from contextlib import suppress
import getpass
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
from urllib.parse import urlencode

from db import Client

HERE = Path(__file__).resolve().parent


class Cluster:
    def __init__(self, bindir, autovacuum=False):
        self.bindir = Path(bindir).resolve()
        self.temp = tempfile.TemporaryDirectory(prefix='branch-app-', dir='/tmp')
        self.root = Path(self.temp.name)
        self.data = self.root/'data'
        self.running = False
        self.apps = []
        self.files = []
        self.options = (f"-k {self.root} -p 58445 -c listen_addresses='' -c shared_buffers=32MB "
                        f"-c max_connections=32 -c autovacuum={'on' if autovacuum else 'off'}")
        self.dsn = 'postgresql:///postgres?'+urlencode(
            {'host': str(self.root), 'port': '58445', 'user': getpass.getuser()})
        self.env = dict(os.environ)
        for key in ('PGSERVICE', 'PGOPTIONS', 'PGUSER', 'PGDATABASE', 'PGHOST', 'PGPORT', 'DATABASE_URL'):
            self.env.pop(key, None)

    def command(self, program, *args):
        return subprocess.run([str(self.bindir/program), *map(str, args)], env=self.env,
                              capture_output=True, text=True, check=True, timeout=40)

    def __enter__(self):
        try:
            self.command('initdb', '-D', self.data, '-A', 'trust', '--no-locale', '-E', 'UTF8')
            self.start()
            self.command('psql', self.dsn, '-X', '-v', 'ON_ERROR_STOP=1',
                         '-f', HERE/'../native_m3/setup.sql')
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def client(self, branch='main', application='tasks_acceptance', database=None):
        dsn = self.dsn if database is None else self.dsn.replace('/postgres?', '/'+database+'?', 1)
        return Client(self.bindir, dsn, branch, application)

    def start(self):
        self.command('pg_ctl', '-D', self.data, '-l', self.root/'server.log', '-o', self.options, '-w', 'start')
        self.running = True

    def stop(self, mode='fast'):
        self.command('pg_ctl', '-D', self.data, '-m', mode, '-w', 'stop')
        self.running = False

    def app(self, branch, port=0):
        log = (self.root/('app_'+branch+'.log')).open('a')
        self.files.append(log)
        process = subprocess.Popen([sys.executable, '-B', str(HERE/'app.py'), '--bindir', str(self.bindir),
                                    '--dsn', self.dsn, '--branch', branch, '--port', str(port)],
                                   stdout=subprocess.PIPE, stderr=log, text=True, env=self.env)
        self.apps.append(process)
        if not select.select([process.stdout], [], [], 12)[0]:
            raise AssertionError('app did not start: '+branch)
        line = process.stdout.readline()
        if not line:
            raise AssertionError('app exited: '+(self.root/('app_'+branch+'.log')).read_text())
        return json.loads(line)['url']

    def migration(self, branch, *directories):
        argv = [sys.executable, '-B', str(HERE/'migrate.py'), '--bindir', str(self.bindir),
                '--dsn', self.dsn, '--branch', branch]
        for directory in directories:
            argv.extend(['--directory', str(directory)])
        return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=self.env)

    def __exit__(self, kind, error, traceback):
        for process in self.apps:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process.stdout.close()
        if self.running:
            with suppress(subprocess.CalledProcessError):
                self.stop('immediate' if error else 'fast')
        for file in self.files:
            file.close()
        if error:
            for path in (self.root/'server.log', *self.root.glob('app_*.log')):
                if path.exists():
                    print(f'--- {path.name} ---\n'+path.read_text()[-12000:], file=sys.stderr)
        self.temp.cleanup()
