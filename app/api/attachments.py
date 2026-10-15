"""Session attachments backed by the existing workbook import engine."""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .endpoints import authenticate_request, get_service, create_import
from ..agent.session import AgentConversation
from ..core import blocking_func_to_async, http_error
from ..models.agent import AgentConversationDao
from ..service.attachment_policy import review_issues
from ..service.lifecycle import cleanup, cleanup_failed_imports
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


async def finalize_attachment(service, conv_id, principal, response, accept=False):
    errors, severe = review_issues(response)
    if response.status != 'active' and not errors and (not severe or accept):
        codes = [w['code'] for w in response.validation.get('warnings', [])]
        try:
            await blocking_func_to_async(service.confirm_import, response.import_id, response.revision, codes)
            response = service.get_import(response.import_id)
        except Exception as exc:
            raise http_error(exc) from exc
    item = {'import_id': response.import_id, 'workbook_id': response.workbook_id,
            'filename': response.file_name, 'version': response.version,
            'status': 'ready' if response.status == 'active' else 'blocked' if errors else 'needs_review',
            'revision': response.revision, 'errors': errors, 'severe_warnings': severe,
            'warnings': response.validation.get('warnings', [])}
    # Reload after parsing to avoid replacing attachments uploaded before this one.
    conv = owned_conversation(conv_id, principal)
    scope = dict(conv.scope_json)
    attachments = [a for a in scope.get('attachments', []) if a['import_id'] != item['import_id']]
    attachments.append(item)
    scope.update(mode='selected', attachments=attachments,
                 workbook_ids=[a['workbook_id'] for a in attachments if a['status'] == 'ready'])
    conv.set_scope(scope)
    return item


@router.post('/agent/conversations/{conv_id}/attachments')
async def upload_attachment(conv_id: str, file: UploadFile = File(...),
                            service=Depends(get_service), principal=Depends(authenticate_request)):
    conv = owned_conversation(conv_id, principal)
    if len(conv.scope_json.get('attachments', [])) >= service.config.multi_workbook_max_selected:
        raise HTTPException(422, 'Maximum conversation attachments reached')
    try:
        service.begin_attachment_job(conv_id)
    except Exception as exc:
        raise http_error(exc) from exc
    try:
        # Recheck after taking the job fence: deletion may have raced the first lookup.
        conv = owned_conversation(conv_id, principal)
        if len(conv.scope_json.get('attachments', [])) >= service.config.multi_workbook_max_selected:
            raise HTTPException(422, 'Maximum conversation attachments reached')
        response = await create_import(file, 'agent', None, service, principal)
        return await finalize_attachment(service, conv_id, principal, response)
    finally:
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
