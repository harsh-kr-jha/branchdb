#!/usr/bin/env python3
"""Runnable task app: one process/connection pool per native database branch."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from db import arguments, Client, DatabaseError, Pool


class RequestError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def identifier(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise RequestError('invalid UUID') from None


def columns(db, table):
    return [row['column_name'] for row in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
        'AND table_name=$1 ORDER BY ordinal_position', (table,))]


def objects(db, sql, params=()):
    return [json.loads(row['item']) for row in db.execute(sql, params)]


def dispatch(db, method, path, body):
    if path == '/api/info' and method == 'GET':
        return 200, {'branch': db.execute("SELECT current_setting('branch') AS branch")[0]['branch'],
                     'task_columns': columns(db, 'tasks'),
                     'note_columns': columns(db, 'task_notes'),
                     'migrations': [r['version'] for r in db.execute(
                         'SELECT version FROM app_migrations ORDER BY version')]}
    if path == '/api/tasks' and method == 'GET':
        return 200, objects(db, 'SELECT row_to_json(t)::text AS item FROM tasks t ORDER BY created_at,id')
    if path == '/api/notes' and method == 'GET':
        if not columns(db, 'task_notes'):
            return 200, []
        return 200, objects(db, 'SELECT row_to_json(n)::text AS item FROM task_notes n ORDER BY id')
    if path == '/api/tasks' and method == 'POST':
        allowed = {'id', 'title', 'status'} | ({'assignee', 'priority'} & set(columns(db, 'tasks')))
        if set(body)-allowed:
            raise RequestError('field is unavailable in this branch')
        if not isinstance(body.get('title'), str) or not body['title'].strip():
            raise RequestError('title is required')
        if body.get('status', 'todo') not in ('todo', 'doing', 'done'):
            raise RequestError('invalid status')
        task = {'id': identifier(body.get('id', uuid4())), 'title': body['title'],
                'status': body.get('status', 'todo')}
        for field in ('assignee', 'priority'):
            if field in body:
                task[field] = body[field]
        validate_fields(task)
        names = list(task)
        parameters = ','.join(f'${i+1}' for i in range(len(names)))
        return 201, objects(db, f"INSERT INTO tasks({','.join(names)},created_at) "
                           f'VALUES({parameters},clock_timestamp()) RETURNING row_to_json(tasks)::text AS item',
                           tuple(task.values()))[0]
    if path == '/api/notes' and method == 'POST':
        fields = set(columns(db, 'task_notes'))
        value_field = 'body' if 'body' in fields else 'score'
        if not fields:
            raise RequestError('notes migration has not been applied')
        if set(body)-{'id', 'task_id', value_field} or value_field not in body or 'task_id' not in body:
            raise RequestError('invalid note fields for this branch')
        if value_field == 'score' and (type(body['score']) is not int or not -2147483648 <= body['score'] <= 2147483647):
            raise RequestError('score must be an integer')
        if value_field == 'body' and not isinstance(body['body'], str):
            raise RequestError('body must be text')
        task_id = identifier(body['task_id'])
        if not db.execute('SELECT id FROM tasks WHERE id=$1', (task_id,)):
            raise RequestError('task not found', 404)
        return 201, objects(db, f'INSERT INTO task_notes(id,task_id,{value_field}) VALUES($1,$2,$3) '
                           'RETURNING row_to_json(task_notes)::text AS item',
                           (identifier(body.get('id', uuid4())), task_id, body[value_field]))[0]
    if path.startswith('/api/tasks/'):
        task_id = identifier(path.removeprefix('/api/tasks/'))
        if method == 'PATCH':
            allowed = {'title', 'status'} | ({'assignee', 'priority'} & set(columns(db, 'tasks')))
            if not body or set(body)-allowed:
                raise RequestError('invalid update fields for this branch')
            validate_fields(body)
            assignment = ','.join(f'{field}=${i+1}' for i, field in enumerate(body))
            rows = objects(db, f'UPDATE tasks SET {assignment} WHERE id=${len(body)+1} '
                           'RETURNING row_to_json(tasks)::text AS item', (*body.values(), task_id))
            if not rows:
                raise RequestError('task not found', 404)
            return 200, rows[0]
        if method == 'DELETE':
            # No database FK/cascade support is claimed. This app's deletion
            # explicitly removes its notes in the same transaction.
            if columns(db, 'task_notes'):
                db.execute('DELETE FROM task_notes WHERE task_id=$1', (task_id,))
            rows = db.execute('DELETE FROM tasks WHERE id=$1 RETURNING id', (task_id,))
            if not rows:
                raise RequestError('task not found', 404)
            return 200, {'deleted': task_id}
    raise RequestError('route not found', 404)


def validate_fields(values):
    if 'title' in values and (not isinstance(values['title'], str) or not values['title'].strip()):
        raise RequestError('title is required')
    if 'status' in values and values['status'] not in ('todo', 'doing', 'done'):
        raise RequestError('invalid status')
    if 'assignee' in values and values['assignee'] is not None and not isinstance(values['assignee'], str):
        raise RequestError('assignee must be text')
    if 'priority' in values and values['priority'] is not None:
        if type(values['priority']) is not int or not -2147483648 <= values['priority'] <= 2147483647:
            raise RequestError('priority must be a 32-bit integer')


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, content, content_type='application/json'):
        payload = (json.dumps(content).encode() if content_type == 'application/json' else content)
        self.send_response(status)
        self.send_header('Content-Type', content_type+'; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def request(self):
        try:
            path = urlsplit(self.path).path
            if self.command == 'GET' and path == '/':
                self.respond(200, Path(__file__).with_name('index.html').read_bytes(), 'text/html')
                return
            size = int(self.headers.get('Content-Length', '0'))
            if size < 0 or size > 65536:
                raise RequestError('request body too large', 413)
            body = json.loads(self.rfile.read(size)) if size else {}
            if not isinstance(body, dict):
                raise RequestError('expected a JSON object')
            for attempt in range(3):
                try:
                    with self.server.pool.connection() as db, db.transaction():
                        status, result = dispatch(db, self.command, path, body)
                    self.respond(status, result)
                    return
                except DatabaseError as error:
                    if error.sqlstate not in ('40001', '40P01') or attempt == 2:
                        raise
                    time.sleep(.02*(attempt+1))
        except RequestError as error:
            self.respond(error.status, {'error': str(error)})
        except (ValueError, json.JSONDecodeError):
            self.respond(400, {'error': 'invalid JSON or request length'})
        except queue.Empty:
            self.respond(503, {'error': 'database pool is busy', 'retryable': True})
        except DatabaseError as error:
            conflict = error.sqlstate == '23505'
            self.respond(409 if conflict else 503,
                         {'error': str(error), 'sqlstate': error.sqlstate,
                          'retryable': error.sqlstate in ('40001', '40P01')})

    do_GET = do_POST = do_PATCH = do_DELETE = request


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    arguments(parser)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--pool-size', type=int, default=2)
    args = parser.parse_args()
    if args.pool_size < 1:
        parser.error('pool size must be positive')
    pool = Pool(Client(args.bindir, args.dsn, args.branch, 'tasks_http_'+args.branch), args.pool_size)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = False
    server.pool = pool
    print(json.dumps({'branch': args.branch, 'url': f'http://127.0.0.1:{server.server_port}'}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        pool.close()
