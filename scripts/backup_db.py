#!/usr/bin/env python3
"""Create a verified, private SQLite snapshot without copying live WAL files."""
import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3


def backup_database(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise ValueError('Source database does not exist')
    if source == destination:
        raise ValueError('Backup must have a different path')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Refuse overwrite, including a symlink, and protect credential hashes.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as live:
            with closing(sqlite3.connect(destination)) as copy:
                live.backup(copy)
                if copy.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Backup integrity check failed')
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default=os.getenv('DB_PATH', '/app/data/gramask.db'))
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    print(backup_database(args.source, args.destination))
