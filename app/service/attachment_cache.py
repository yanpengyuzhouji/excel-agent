"""Single-instance attachment reuse; callers hold the lifecycle fence for hits."""
import hashlib
import json
from importlib.metadata import version
from pathlib import Path

from ..models.workbook import AttachmentCacheEntity
from .attachment_policy import review_issues


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def cache_key(service, owner, source_hash, filename):
    suffix = Path(filename).suffix.lower()
    if suffix not in {'.xlsx', '.xlsm', '.csv'}:
        raise ValueError('Only .xlsx, .xlsm and .csv files are supported')
    here = Path(__file__).resolve().parent
    implementation = {name: digest(here / name) for name in (
        'parser.py', 'structured_layout.py', 'constants.py', 'materializer.py',
        'attachment_policy.py', 'attachment_cache.py', 'service.py')}
    payload = {'owner': owner, 'source': source_hash, 'suffix': suffix,
               'limits': service._parser_limits(), 'implementation': implementation,
               'openpyxl': version('openpyxl')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def artifact_paths(service, entity):
    root = Path(service.config.resolved_storage_path()).resolve()
    directory = root / entity.workbook_id / f'v{entity.version}'
    paths = [service._resolve_version_artifact_path(entity.workbook_id, entity.version, value)
             for value in (entity.source_path, entity.draft_path, entity.database_path)]
    if any(p is None or not p.is_file() or p.is_symlink() or p.resolve().parent != directory for p in paths):
        raise ValueError('Cache artifacts missing or outside expected storage')
    return paths


def lookup(service, key, owner, source_hash):
    with service._import_dao.session() as session:
        row = session.query(AttachmentCacheEntity).filter_by(cache_key=key, owner_id=owner).first()
        if row is None:
            return None
        try:
            entity = service._load_entity(row.import_id)
            dataset = service._dataset_dao.get_by_workbook_id(row.workbook_id, owner)
            if (not dataset or dataset.active_import_id != entity.import_id or entity.status != 'active'
                    or entity.revision != 1 or entity.version != 1 or entity.source_hash != source_hash):
                return None
            response = service.get_import(entity.import_id)
            errors, severe = review_issues(response)
            if errors or severe:
                return None
            source, draft, database = artifact_paths(service, entity)
            if (digest(source), digest(draft), digest(database)) != (source_hash, row.draft_hash, row.database_hash):
                return None
            return response
        except (OSError, ValueError, KeyError):
            return None


def remember(service, key, owner, response):
    errors, severe = review_issues(response)
    if response.status != 'active' or response.revision != 1 or response.version != 1 or errors or severe:
        return
    entity = service._load_entity(response.import_id)
    _, draft, database = artifact_paths(service, entity)
    with service._import_dao.session() as session:
        # A stale pointer may be replaced; never merge or delete its old data.
        previous = session.query(AttachmentCacheEntity).filter_by(cache_key=key).first()
        if previous:
            previous.cache_key = hashlib.sha256(('retired:'+key+previous.workbook_id).encode()).hexdigest()
            session.flush()
        session.add(AttachmentCacheEntity(cache_key=key, owner_id=owner,
            workbook_id=response.workbook_id, import_id=response.import_id,
            draft_hash=digest(draft), database_hash=digest(database)))
