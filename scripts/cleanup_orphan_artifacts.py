"""Dry-run by default. --confirm requires a verified full storage archive."""
import argparse
import hashlib
import json
import re
from pathlib import Path

from app.config import build_app_config
from app.db import ensure_tables
from app.service.service import WorkbookImportService
from app.service.lifecycle import cleanup, referenced_workbooks
from app.models.workbook import WorkbookDatasetEntity, WorkbookImportEntity
from app.models.catalog import WorkbookCatalogEntity
from app.models.agent import AgentConversationEntity

parser = argparse.ArgumentParser()
parser.add_argument('--confirm', action='store_true')
parser.add_argument('--archive', type=Path)
args = parser.parse_args()
cfg = build_app_config()
root = Path(cfg.resolved_storage_path()).resolve()
ensure_tables()
service = WorkbookImportService(config=cfg)
with service._import_dao.session() as session:
    refs = referenced_workbooks(session)
    books = {r[0] for r in session.query(WorkbookDatasetEntity.workbook_id).all()}
    books.update(r[0] for r in session.query(WorkbookImportEntity.workbook_id).all())
    books.update(r[0] for r in session.query(WorkbookCatalogEntity.workbook_id).all())
    books.update(p.name for p in root.iterdir() if p.is_dir() and re.fullmatch('[0-9a-f]{32}', p.name))
    candidates = sorted(books-refs)
    live = {r[0] for r in session.query(AgentConversationEntity.conv_id).all()}
reports = root/'agent_conversations'
report_ids = sorted(p.name for p in reports.iterdir() if p.is_dir() and re.fullmatch('[0-9a-f]{32}', p.name) and p.name not in live) if reports.exists() else []
print(json.dumps({'unreferenced_workbooks': candidates, 'orphan_report_directories': report_ids}))
if args.confirm:
    if args.archive is None:
        raise SystemExit('--archive is required before destructive cleanup; stop the server first')
    archive = args.archive.resolve()
    if archive == root or root in archive.parents or not archive.is_dir():
        raise SystemExit('Archive must be an existing directory outside live storage')
    def digest(path):
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    # Verify every live file, including metadata and original uploads, before deletion.
    for source in root.rglob('*'):
        if source.is_file():
            target = archive/source.relative_to(root)
            if not target.is_file() or digest(source) != digest(target):
                raise SystemExit('Archive verification failed: '+str(source))
    print(json.dumps(cleanup(service, workbook_ids=candidates, report_ids=report_ids), ensure_ascii=False))
