"""Small libpq client for the executable app fixture; Python standard library only.

SQL values use the extended query protocol. A branch is selected at connection
startup and never switched on a pooled connection. This is not a general driver.
"""
from contextlib import contextmanager, suppress
import ctypes as c
import os
from pathlib import Path
import queue
import re


class DatabaseError(Exception):
    def __init__(self, message, sqlstate='08006'):
        super().__init__(message)
        self.sqlstate = sqlstate


def branch_name(value):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,62}', value):
        raise ValueError('branch must contain 1–63 letters, digits, underscores or hyphens and start with a letter')
    return value


def arguments(parser):
    parser.add_argument('--bindir', required=True, type=Path,
                        help='bin directory of the modified PostgreSQL install')
    parser.add_argument('--dsn', default=os.environ.get('DATABASE_URL', ''),
                        help='libpq URL/connection string; defaults to PG* environment')
    parser.add_argument('--branch', default='main', type=branch_name)


class Client:
    def __init__(self, bindir, dsn='', branch='main', application='branch_tasks'):
        self.dsn = dsn
        self.branch = branch_name(branch)
        self.application = application
        prefix = Path(bindir).resolve().parent
        paths = [prefix/'lib'/'libpq.dylib', prefix/'lib'/'libpq.so',
                 prefix/'lib64'/'libpq.so']
        library = next((path for path in paths if path.exists()), None)
        if library is None:
            raise ValueError(f'libpq not found below {prefix}')
        self.lib = lib = c.CDLL(str(library))
        ptr, string, integer = c.c_void_p, c.c_char_p, c.c_int
        signatures = {
            'PQconnectdbParams': (ptr, [c.POINTER(string), c.POINTER(string), integer]),
            'PQstatus': (integer, [ptr]),
            'PQerrorMessage': (string, [ptr]),
            'PQfinish': (None, [ptr]),
            'PQexec': (ptr, [ptr, string]),
            'PQexecParams': (ptr, [ptr, string, integer, ptr, c.POINTER(string), ptr, ptr, integer]),
            'PQresultStatus': (integer, [ptr]),
            'PQresultErrorField': (string, [ptr, integer]),
            'PQclear': (None, [ptr]),
            'PQntuples': (integer, [ptr]),
            'PQnfields': (integer, [ptr]),
            'PQfname': (string, [ptr, integer]),
            'PQgetvalue': (string, [ptr, integer, integer]),
            'PQgetisnull': (integer, [ptr, integer, integer]),
            'PQtransactionStatus': (integer, [ptr]),
            **{name: (string, [ptr]) for name in ('PQhost', 'PQport', 'PQdb', 'PQuser')},
        }
        for name, (result, params) in signatures.items():
            function = getattr(lib, name)
            function.restype, function.argtypes = result, params

    def connect(self):
        return Connection(self)


class Connection:
    def __init__(self, client):
        self.lib = lib = client.lib
        # expand_dbname permits DATABASE_URL as well as ordinary conninfo. These
        # later explicit options override any conflicting branch in the DSN.
        values = {'dbname': client.dsn, 'options': f'-c branch={client.branch}',
                  'application_name': client.application, 'connect_timeout': '5',
                  'client_encoding': 'UTF8'}
        keys = (c.c_char_p*(len(values)+1))(*(key.encode() for key in values), None)
        vals = (c.c_char_p*(len(values)+1))(*(v.encode() for v in values.values()), None)
        self.handle = lib.PQconnectdbParams(keys, vals, 1)
        if not self.ok:
            message = lib.PQerrorMessage(self.handle).decode() if self.handle else 'libpq allocation failed'
            self.close()
            raise DatabaseError(message)
        try:
            self.execute("SET statement_timeout='5s'")
        except BaseException:
            self.close()
            raise

    @property
    def ok(self):
        return bool(self.handle) and self.lib.PQstatus(self.handle) == 0

    def close(self):
        if self.handle:
            self.lib.PQfinish(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    def _result(self, result):
        if not result:
            raise DatabaseError(self.lib.PQerrorMessage(self.handle).decode())
        try:
            if self.lib.PQresultStatus(result) not in (1, 2):
                message = self.lib.PQresultErrorField(result, ord('M')) or b'database operation failed'
                state = self.lib.PQresultErrorField(result, ord('C')) or b'08006'
                raise DatabaseError(message.decode(), state.decode())
            names = [self.lib.PQfname(result, j).decode()
                     for j in range(self.lib.PQnfields(result))]
            return [{name: None if self.lib.PQgetisnull(result, i, j)
                     else self.lib.PQgetvalue(result, i, j).decode()
                     for j, name in enumerate(names)}
                    for i in range(self.lib.PQntuples(result))]
        finally:
            self.lib.PQclear(result)

    def execute(self, sql, params=()):
        encoded = [None if value is None else str(value).encode() for value in params]
        values = (c.c_char_p*len(encoded))(*encoded)
        return self._result(self.lib.PQexecParams(self.handle, sql.encode(), len(encoded),
                                                 None, values, None, None, 0))

    def script(self, sql):
        """Multiple statements, ONLY for trusted local migration files."""
        return self._result(self.lib.PQexec(self.handle, sql.encode()))

    @contextmanager
    def transaction(self):
        self.execute('BEGIN')
        try:
            yield self
            self.execute('COMMIT')
        except BaseException:
            with suppress(DatabaseError):
                self.execute('ROLLBACK')
            raise


class Pool:
    """Bounded pool permanently bound to one branch; one owner per connection."""
    def __init__(self, client, size=2):
        self.client = client
        self.idle = queue.Queue(size)
        try:
            for _ in range(size):
                self.idle.put(client.connect())
        except BaseException:
            self.close()
            raise

    @contextmanager
    def connection(self):
        connection = self.idle.get(timeout=6)
        try:
            if connection is None or not connection.ok:
                if connection is not None:
                    connection.close()
                connection = None
                connection = self.client.connect()
            yield connection
        finally:
            if connection is not None and (not connection.ok or
                                           connection.lib.PQtransactionStatus(connection.handle) != 0):
                connection.close()
                connection = None
            self.idle.put(connection)

    def close(self):
        while not self.idle.empty():
            connection = self.idle.get_nowait()
            if connection is not None:
                connection.close()
