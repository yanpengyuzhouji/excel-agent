"""Session attachments backed by the existing workbook import engine."""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .endpoints import authenticate_request, get_service
from pathlib import Path
from ..agent.session import AgentConversation
from ..core import blocking_func_to_async, http_error
from ..models.agent import AgentConversationDao
from ..service.attachment_policy import review_issues
from ..service.lifecycle import cleanup, cleanup_failed_imports
from ..service.upload_stream import spool_upload
from ..service.attachment_cache import cache_key, lookup, remember
import logging

router = APIRouter()


@router.get('/capabilities')
def capabilities(principal=Depends(authenticate_request)):
    try:
        from weasyprint import HTML  # noqa: F401
        pdf_available = True
    except (ImportError, OSError):
        pdf_available = False
    return {'pdf_available': pdf_available, 'session_attachments': True}


def owned_conversation(conv_id, principal):
    conv = AgentConversation.load(conv_id, principal)
    if not conv:
        raise HTTPException(404, 'Conversation not found')
    return conv


class ConversationTitle(BaseModel):
    title: str = Field(default='新对话', min_length=1, max_length=80)


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=80)
    pinned: bool | None = None


@router.post('/agent/conversations')
def create_conversation(request: ConversationTitle, principal=Depends(authenticate_request)):
    conv = AgentConversation.create(principal, 'selected',
                                    {'mode': 'selected', 'workbook_ids': [], 'attachments': []},
                                    title=request.title)
    return {'conv_id': conv.conv_id, 'title': conv.title, 'scope': conv.scope_json}


@router.patch('/agent/conversations/{conv_id}')
def rename_conversation(conv_id: str, request: ConversationUpdate, principal=Depends(authenticate_request)):
    owned_conversation(conv_id, principal)
    values = request.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(422, 'Specify title or pinned')
    AgentConversationDao().update(conv_id, principal, values)
    return {'conv_id': conv_id, **values}


def _prepare_attachment(service, response, accept=False):
    errors, severe = review_issues(response)
    if response.status != 'active' and not errors and (not severe or accept):
        codes = [w['code'] for w in response.validation.get('warnings', [])]
        try:
            service.confirm_import(response.import_id, response.revision, codes)
            response = service.get_import(response.import_id)
        except Exception as exc:
            raise http_error(exc) from exc
    return response


def _finalize_attachment(service, conv_id, principal, response, filename=None, reused=False):
    errors, severe = review_issues(response)
    item = {'import_id': response.import_id, 'workbook_id': response.workbook_id,
            'filename': filename or response.file_name, 'version': response.version,
            'status': 'ready' if response.status == 'active' else 'blocked' if errors else 'needs_review',
            'revision': response.revision, 'errors': errors, 'severe_warnings': severe,
            'warnings': response.validation.get('warnings', []), 'reused': reused}
    # Reload after parsing to avoid replacing attachments uploaded before this one.
    conv = owned_conversation(conv_id, principal)
    scope = dict(conv.scope_json)
    attachments = [a for a in scope.get('attachments', []) if a['import_id'] != item['import_id']]
    attachments.append(item)
    scope.update(mode='selected', attachments=attachments,
                 workbook_ids=[a['workbook_id'] for a in attachments if a['status'] == 'ready'])
    conv.set_scope(scope)
    return item


def _locked_finalize(service, conv_id, principal, response, accept=False):
    response = _prepare_attachment(service, response, accept)
    with service._lifecycle_lock:
        return _finalize_attachment(service, conv_id, principal, response)


async def finalize_attachment(service, conv_id, principal, response, accept=False):
    return await blocking_func_to_async(_locked_finalize, service, conv_id, principal, response, accept)


def _import_attachment(service, conv_id, principal, filename, staged_path, size, source_hash):
    key = cache_key(service, principal, source_hash, filename)
    with service._attachment_import_lock:
        with service._lifecycle_lock:
            conv = owned_conversation(conv_id, principal)
            response = lookup(service, key, principal, source_hash)
            already_attached = response and any(a['import_id'] == response.import_id
                for a in conv.scope_json.get('attachments', []))
            if not already_attached and len(conv.scope_json.get('attachments', [])) >= service.config.multi_workbook_max_selected:
                raise HTTPException(422, 'Maximum conversation attachments reached')
            if response:
                logging.getLogger(__name__).info('attachment_cache_hit workbook_id=%s', response.workbook_id)
                return _finalize_attachment(service, conv_id, principal, response, filename, reused=True)
        response = service.create_import_from_path(filename, staged_path, 'agent', None, principal, size, source_hash)
        response = _prepare_attachment(service, response)
        with service._lifecycle_lock:
            item = _finalize_attachment(service, conv_id, principal, response)
            remember(service, key, principal, service.get_import(response.import_id))
            return item


@router.post('/agent/conversations/{conv_id}/attachments')
async def upload_attachment(conv_id: str, file: UploadFile = File(...),
                            service=Depends(get_service), principal=Depends(authenticate_request)):
    owned_conversation(conv_id, principal)
    try:
        service.begin_attachment_job(conv_id)
    except Exception as exc:
        raise http_error(exc) from exc
    staged_path = None
    try:
        # Recheck after taking the job fence: deletion may have raced the first lookup.
        conv = owned_conversation(conv_id, principal)
        staged_path, size, source_hash = await spool_upload(file,
            str(Path(service.config.resolved_storage_path()) / '.incoming'), service.config.max_import_bytes)
        return await blocking_func_to_async(_import_attachment, service, conv_id, principal,
            file.filename or 'workbook.xlsx', staged_path, size, source_hash)
    except Exception as exc:
        raise http_error(exc) from exc
    finally:
        await file.close()
        if staged_path:
            Path(staged_path).unlink(missing_ok=True)
        service.end_attachment_job(conv_id)
        try:
            await blocking_func_to_async(cleanup_failed_imports, service, principal)
        except Exception:
            logging.getLogger(__name__).warning('Failed import cleanup deferred until restart', exc_info=True)


class ReviewAcceptance(BaseModel):
    revision: int
    accept_severe_warnings: bool = False


@router.post('/agent/conversations/{conv_id}/attachments/{import_id}/confirm')
async def confirm_attachment(conv_id: str, import_id: str, request: ReviewAcceptance,
                             service=Depends(get_service), principal=Depends(authenticate_request)):
    conv = owned_conversation(conv_id, principal)
    if not any(a['import_id'] == import_id for a in conv.scope_json.get('attachments', [])):
        raise HTTPException(404, 'Attachment not in conversation')
    try:
        service.assert_import_owner(import_id, principal)
    except Exception as exc:
        raise http_error(exc) from exc
    response = service.get_import(import_id)
    if request.revision != response.revision:
        raise HTTPException(409, 'Draft revision changed; reload the attachment')
    try:
        service.begin_attachment_job(conv_id)
    except Exception as exc:
        raise http_error(exc) from exc
    try:
        owned_conversation(conv_id, principal)
        return await finalize_attachment(service, conv_id, principal, response, request.accept_severe_warnings)
    finally:
        service.end_attachment_job(conv_id)
        try:
            await blocking_func_to_async(cleanup_failed_imports, service, principal)
        except Exception:
            logging.getLogger(__name__).warning('Failed import cleanup deferred until restart', exc_info=True)


@router.delete('/agent/conversations/{conv_id}/attachments/{import_id}')
async def detach_attachment(conv_id: str, import_id: str, service=Depends(get_service), principal=Depends(authenticate_request)):
    owned_conversation(conv_id, principal)
    try:
        result = await blocking_func_to_async(cleanup, service, conv_id=conv_id, owner_id=principal, detach_import=import_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return {'detached': True, **result}
