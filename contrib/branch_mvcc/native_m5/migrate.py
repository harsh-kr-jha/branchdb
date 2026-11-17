"""Transactional SQL-file migrations for the task app, with immutable checksums."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from db import arguments, Client, DatabaseError


def migrate(client, directories):
    for directory in directories:
        if not Path(directory).is_dir() or not list(Path(directory).glob('*.sql')):
            raise ValueError(f'migration directory is missing or empty: {directory}')
    files = sorted(path for directory in directories for path in Path(directory).glob('*.sql'))
    versions = [path.stem for path in files]
    if len(versions) != len(set(versions)):
        raise ValueError('duplicate migration version')
    applied = []
    with client.connect() as db:
        for path in sorted(files, key=lambda p: p.stem):
            script = path.read_text()
            checksum = hashlib.sha256(script.encode()).hexdigest()
            with db.transaction():
                # Branch-local lock also covers creating the migration table.
                db.execute("SELECT pg_advisory_xact_lock(hashtext(current_database()), "
                           "hashtext(current_setting('branch')||':tasks_migrations'))")
                if db.execute("SELECT to_regclass('public.app_migrations') IS NOT NULL AS present")[0]['present'] == 'f':
                    db.execute('CREATE TABLE app_migrations(version text PRIMARY KEY, '
                               'checksum text NOT NULL, applied_at timestamptz NOT NULL)')
                rows = db.execute('SELECT checksum FROM app_migrations WHERE version=$1', (path.stem,))
                if rows:
                    if rows[0]['checksum'] != checksum:
                        raise ValueError(f'migration changed after application: {path.stem}')
                    continue
                db.script(script)
                db.execute('INSERT INTO app_migrations VALUES($1,$2,clock_timestamp())',
                           (path.stem, checksum))
            applied.append(path.stem)
    return applied


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    arguments(parser)
    parser.add_argument('--directory', action='append', required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps({'branch': args.branch, 'applied': migrate(
            Client(args.bindir, args.dsn, args.branch, 'tasks_migrate_'+args.branch), args.directory)}))
    except (DatabaseError, ValueError) as error:
        print(json.dumps({'error': str(error), 'sqlstate': getattr(error, 'sqlstate', None)}), file=sys.stderr)
        sys.exit(1)
