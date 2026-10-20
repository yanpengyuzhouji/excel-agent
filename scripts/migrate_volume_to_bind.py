"""Run in a disposable helper with a STOPPED, read-only /source mount.

Destination /target must be empty; /backup is a separate host backup directory.
This script never deletes source files, overwrites destination data, or starts services.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
from datetime import datetime, timezone


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def manifest(root):
    result = {}
    for p in sorted(root.rglob('*')):
        if p.is_symlink():
            raise ValueError('Symbolic links require manual migration review')
        if p.is_file():
            result[p.relative_to(root).as_posix()] = {'bytes':p.stat().st_size,'sha256':digest(p)}
    return result


def main():
    source, target, backup = map(Path, ['/source','/target','/backup'])
    assert source.is_dir() and target.is_dir() and backup.is_dir()
    assert not any(target.iterdir()), 'Destination is not empty; refusing to overwrite'
    assert (source/'workbook_meta.db').is_file(), 'Source metadata database missing'
    before = manifest(source)
    tag = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    archive = backup / ('named-volume-before-bind-'+tag+'.tar')
    with tarfile.open(archive,'x') as tar:
        tar.add(source,arcname='data',recursive=True)
    # Verify all archived file contents before copying the live dataset.
    with tarfile.open(archive) as tar:
        for name, entry in before.items():
            h = hashlib.sha256()
            with tar.extractfile('data/'+name) as stream:
                for block in iter(lambda:stream.read(1024*1024),b''):
                    h.update(block)
            assert h.hexdigest()==entry['sha256'], 'Archive mismatch: '+name
    for child in source.iterdir():
        destination = target/child.name
        if child.is_dir():
            shutil.copytree(child,destination)
        else:
            shutil.copy2(child,destination)
    after = manifest(target)
    assert before==after, 'Copied content does not reconcile'
    checks = {}
    for name in after:
        p=target/name
        if p.suffix not in {'.db','.sqlite'}:
            continue
        with sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True) as db:
            verdict=[r[0] for r in db.execute('PRAGMA quick_check')]
            assert verdict==['ok'], 'SQLite integrity failure: '+name
            checks[name]=verdict
    for p in [target,*target.rglob('*')]:
        os.chown(p,10001,10001)
    report={'source':'excel-workbook-service_excel-data','files':len(before),
            'bytes':sum(e['bytes'] for e in before.values()), 'all_file_hashes_equal':True,
            'archive':archive.name,'archive_sha256':digest(archive),'sqlite_checks':checks,'manifest':after}
    (backup/('bind-migration-'+tag+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='manifest'},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
