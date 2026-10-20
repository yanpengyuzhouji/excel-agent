"""Reference-aware session cleanup, staging files before metadata commits."""
import json
import re
import shutil
import uuid
from pathlib import Path

from sqlalchemy import inspect, text
from ..core import ResourceBusyError
from ..models.agent import AgentConversationEntity, AgentMessageEntity, DataSpaceWorkbookEntity, DatasetRelationEntity
from ..models.workbook import WorkbookDatasetEntity, WorkbookImportEntity, AttachmentCacheEntity
from ..models.catalog import WorkbookCatalogEntity
from ..models.conversation import WorkbookConversationEntity


def workbook_refs(scope, snapshot):
    scope = json.loads(scope or '{}') if isinstance(scope, str) else scope
    snapshot = json.loads(snapshot or '[]') if isinstance(snapshot, str) else snapshot
    return set(scope.get('workbook_ids') or []) | {a['workbook_id'] for a in scope.get('attachments', []) if a.get('workbook_id')} | {a['workbook_id'] for a in snapshot or [] if a.get('workbook_id')}


def referenced_workbooks(session):
    refs = set()
    for row in session.query(AgentConversationEntity).all():
        refs.update(workbook_refs(row.scope_json, row.source_snapshot_json))
    refs.update(r[0] for r in session.query(WorkbookConversationEntity.workbook_id).all())
    refs.update(r[0] for r in session.query(DataSpaceWorkbookEntity.workbook_id).all())
    for left, right in session.query(DatasetRelationEntity.left_workbook_id, DatasetRelationEntity.right_workbook_id).all():
        refs.update((left, right))
    return refs


def safe_child(root, identifier):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', identifier):
        raise ValueError('Unsafe artifact identifier')
    path = root / identifier
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise ValueError('Unsafe artifact storage path')
    return path


def cleanup(service, *, conv_id=None, owner_id=None, detach_import=None, workbook_ids=(), report_ids=()):
    root = Path(service.config.resolved_storage_path()).resolve()
    pending = root / '.cleanup_pending'
    if pending.is_symlink() or pending.resolve().parent != root:
        raise ValueError('Unsafe cleanup storage path')
    job = pending / uuid.uuid4().hex
    staged = []
    deleted = []
    kept = []
    with service._lifecycle_lock:
        runs = service._agent_run_scopes
        if (service._active_agent_runs > len(runs) or conv_id in runs
                or conv_id in service._attachment_jobs
                or (not conv_id and service._attachment_jobs)):
            raise ResourceBusyError('该会话或附件正在执行，请完成后再删除。')
        try:
            with service._import_dao.session() as session:
                candidates = set(workbook_ids)
                if conv_id:
                    conv = session.query(AgentConversationEntity).filter_by(conv_id=conv_id, owner_id=owner_id).first()
                    if not conv:
                        raise ValueError('Conversation not found')
                    if detach_import:
                        scope = json.loads(conv.scope_json or '{}')
                        attachments = scope.get('attachments', [])
                        removed = [a for a in attachments if a['import_id'] == detach_import]
                        if not removed:
                            raise ValueError('Attachment not in conversation')
                        candidates.update(a['workbook_id'] for a in removed)
                        scope['attachments'] = [a for a in attachments if a['import_id'] != detach_import]
                        scope['workbook_ids'] = [a['workbook_id'] for a in scope['attachments'] if a['status'] == 'ready']
                        conv.scope_json = json.dumps(scope, ensure_ascii=False)
                        conv.source_snapshot_json = json.dumps([s for s in json.loads(conv.source_snapshot_json or '[]') if s.get('workbook_id') not in candidates])
                    else:
                        candidates.update(workbook_refs(conv.scope_json, conv.source_snapshot_json))
                        session.query(AgentMessageEntity).filter_by(conv_id=conv_id).delete(synchronize_session=False)
                        session.delete(conv)
                        reports = root / 'agent_conversations'
                        if reports.is_symlink() or reports.resolve().parent != root:
                            raise ValueError('Unsafe report directory')
                        path = safe_child(reports, conv_id)
                        if path.exists():
                            job.mkdir(parents=True, exist_ok=True)
                            target = job / ('conversation--'+conv_id)
                            path.rename(target)
                            staged.append((path, target))
                session.flush()
                reports = root / 'agent_conversations'
                if reports.is_symlink() or reports.resolve().parent != root:
                    raise ValueError('Unsafe report directory')
                for report_id in report_ids:
                    if session.query(AgentConversationEntity).filter_by(conv_id=report_id).first():
                        raise ValueError('Cannot clean reports from a live conversation')
                    path = safe_child(reports, report_id)
                    if path.exists():
                        job.mkdir(parents=True, exist_ok=True)
                        target = job / ('conversation--'+report_id)
                        path.rename(target)
                        staged.append((path, target))
                refs = referenced_workbooks(session)
                active_books = set().union(*runs.values()) if runs else set()
                for active_conv in service._attachment_jobs:
                    uploading = session.query(AgentConversationEntity).filter_by(conv_id=active_conv).first()
                    if uploading:
                        active_books.update(workbook_refs(uploading.scope_json, uploading.source_snapshot_json))
                if (candidates - refs) & active_books:
                    raise ResourceBusyError('附件正在被其他分析使用，请完成后再删除。')
                for book_id in sorted(candidates):
                    if book_id in refs:
                        kept.append(book_id)
                        continue
                    dataset = session.query(WorkbookDatasetEntity).filter_by(workbook_id=book_id).first()
                    if dataset and owner_id is not None and dataset.owner_id != owner_id:
                        raise PermissionError('Cannot delete another owner\'s workbook')
                    path = safe_child(root, book_id)
                    if path.exists():
                        job.mkdir(parents=True, exist_ok=True)
                        target = job / ('workbook--'+book_id)
                        path.rename(target)
                        staged.append((path, target))
                    session.query(WorkbookCatalogEntity).filter_by(workbook_id=book_id).delete(synchronize_session=False)
                    if inspect(session.bind).has_table('workbook_catalog_fts'):
                        session.execute(text('DELETE FROM workbook_catalog_fts WHERE workbook_id=:id'), {'id': book_id})
                    session.query(WorkbookImportEntity).filter_by(workbook_id=book_id).delete(synchronize_session=False)
                    session.query(AttachmentCacheEntity).filter_by(workbook_id=book_id).delete(synchronize_session=False)
                    session.query(WorkbookDatasetEntity).filter_by(workbook_id=book_id).delete(synchronize_session=False)
                    deleted.append(book_id)
            # The transaction has committed. Pending folders are no longer live data.
        except Exception:
            for original, target in reversed(staged):
                if target.exists():
                    target.rename(original)
            if job.exists() and not any(job.iterdir()):
                job.rmdir()
            raise
        warnings = []
        if job.exists():
            try:
                shutil.rmtree(job)
            except OSError:
                warnings.append('记录已清理，但部分临时文件暂未移除；重启服务后将重试清理。')
    return {'deleted_workbooks': deleted, 'retained_shared_workbooks': kept, 'cleanup_warnings': warnings}


def recover_pending(service):
    """After a crash: restore pre-commit staged folders, purge committed ones."""
    root = Path(service.config.resolved_storage_path()).resolve()
    pending = root / '.cleanup_pending'
    if not pending.exists():
        return
    if pending.is_symlink() or pending.resolve().parent != root:
        raise ValueError('Unsafe cleanup storage path')
    with service._lifecycle_lock, service._import_dao.session() as session:
        books = {r[0] for r in session.query(WorkbookDatasetEntity.workbook_id).all()}
        conversations = {r[0] for r in session.query(AgentConversationEntity.conv_id).all()}
        for job in pending.iterdir():
            if not re.fullmatch('[0-9a-f]{32}', job.name) or job.is_symlink() or not job.is_dir():
                continue
            for artifact in job.iterdir():
                kind, separator, identifier = artifact.name.partition('--')
                if not separator or kind not in {'workbook', 'conversation'} or artifact.is_symlink():
                    raise ValueError('Unexpected staged artifact; manual review required')
                parent = root if kind == 'workbook' else root / 'agent_conversations'
                if kind == 'conversation' and (parent.is_symlink() or parent.resolve().parent != root):
                    raise ValueError('Unsafe report storage path')
                destination = safe_child(parent, identifier)
                exists = identifier in (books if kind == 'workbook' else conversations)
                if exists:
                    if destination.exists():
                        raise ValueError('Staged artifact conflicts with live data; manual review required')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    artifact.rename(destination)
                else:
                    shutil.rmtree(artifact)
            job.rmdir()


def cleanup_failed_imports(service, owner_id=None):
    with service._import_dao.session() as session:
        query = session.query(WorkbookImportEntity.workbook_id).filter_by(source_channel='agent', status='failed')
        if owner_id is not None:
            query = query.join(WorkbookDatasetEntity, WorkbookDatasetEntity.workbook_id == WorkbookImportEntity.workbook_id).filter(WorkbookDatasetEntity.owner_id == owner_id)
        ids = [r[0] for r in query.all()]
    if ids:
        return cleanup(service, workbook_ids=ids, owner_id=owner_id)
    return {'deleted_workbooks': [], 'cleanup_warnings': []}
