"""Explicit one-off conversation cleanup, with an SQLite backup before deletion."""
import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from app.config import build_app_config

parser = argparse.ArgumentParser()
parser.add_argument('--confirm', action='store_true', help='Back up and clear conversation tables; stop the server first')
args = parser.parse_args()
storage = Path(build_app_config().resolved_storage_path()).resolve()
database = storage / 'workbook_meta.db'
if not database.is_file():
    raise SystemExit('Metadata database does not exist')
with sqlite3.connect(database) as connection:
    present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    tables = [name for name in ('agent_message', 'agent_conversation', 'workbook_message', 'workbook_conversation') if name in present]
    counts = {name: connection.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0] for name in tables}
    print(json.dumps({'database': str(database), 'conversation_rows': counts}))
    if args.confirm:
        backup = storage / ('history-backup-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.sqlite')
        with sqlite3.connect(backup) as destination:
            connection.backup(destination)
        protected = {name: connection.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                     for name in ('workbook_dataset', 'workbook_import', 'workbook_catalog') if name in present}
        connection.execute('BEGIN IMMEDIATE')
        for name in tables:
            connection.execute(f'DELETE FROM {name}')
        for name, count in protected.items():
            if connection.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0] != count:
                raise RuntimeError('Workbook metadata changed; aborting cleanup')
        connection.commit()
        print(json.dumps({'backup': str(backup), 'cleared': counts, 'workbook_metadata_preserved': protected}))
