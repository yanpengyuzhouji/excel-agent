"""Read-only backup verification helper; run against an offline mounted volume."""
import hashlib
import json
from pathlib import Path
import sqlite3


def manifest(root):
    root = Path(root).resolve()
    files, databases = {}, {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Symlink requires separate backup policy: '+str(path))
        if not path.is_file():
            continue
        relative = str(path.relative_to(root))
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        files[relative] = {'size': path.stat().st_size, 'sha256': digest.hexdigest()}
        if path.suffix not in {'.db', '.sqlite'}:
            continue
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as connection:
            integrity = connection.execute('PRAGMA integrity_check').fetchall()
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            counts = {table: connection.execute('SELECT COUNT(*) FROM "'+table.replace('"','""')+'"').fetchone()[0] for table in tables}
        databases[relative] = {'integrity_ok': integrity == [('ok',)], 'table_counts': counts}
    return {'files': files, 'databases': databases, 'file_count': len(files),
            'total_bytes': sum(v['size'] for v in files.values())}


if __name__ == '__main__':
    import sys
    print(json.dumps(manifest(sys.argv[1] if len(sys.argv) > 1 else '/data'), ensure_ascii=False))
