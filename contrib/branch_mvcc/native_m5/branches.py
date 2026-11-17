#!/usr/bin/env python3
"""Create/list native database branches and print credential-free connection URLs."""
import argparse
import json
import sys
from urllib.parse import quote, urlencode

from db import arguments, branch_name, Client, DatabaseError


def connection_url(db, branch):
    branch_name(branch)
    options = {key: getattr(db.lib, function)(db.handle).decode() for key, function
               in (('host', 'PQhost'), ('port', 'PQport'), ('user', 'PQuser'))}
    options['options'] = f'-c branch={branch}'
    database = db.lib.PQdb(db.handle).decode()
    # libpq URI options use percent escapes; '+' is a literal, not a space.
    return f'postgresql:///{quote(database, safe="")}?{urlencode(options, quote_via=quote)}'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    arguments(parser)
    parser.add_argument('command', choices=('create', 'list', 'url'))
    parser.add_argument('name', nargs='?', type=branch_name)
    args = parser.parse_args()
    if args.branch != 'main':
        parser.error('branch administration connects through main')
    if args.command != 'list' and args.name is None:
        parser.error('this command needs a branch name')
    try:
        with Client(args.bindir, args.dsn, 'main', 'tasks_branches').connect() as db:
            if args.command == 'create':
                db.execute('SELECT branch_create($1)', (args.name,))
                print(json.dumps({'created': args.name, 'url': connection_url(db, args.name)}))
            elif args.command == 'list':
                print(json.dumps(db.execute('SELECT id,name,cutoff FROM branch_native.branches ORDER BY id')))
            else:
                if not db.execute('SELECT name FROM branch_native.branches WHERE name=$1', (args.name,)):
                    raise ValueError('branch does not exist')
                print(connection_url(db, args.name))
    except (DatabaseError, ValueError) as error:
        print(json.dumps({'error': str(error), 'sqlstate': getattr(error, 'sqlstate', None)}), file=sys.stderr)
        sys.exit(1)
