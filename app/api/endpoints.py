"""FastAPI endpoints, ported from
DB-GPT packages/dbgpt-serve/src/dbgpt_serve/workbook/api/endpoints.py.

Changes: Result wrapper removed (returns models/dicts directly), no
SystemApp / get_service component lookup (service injected via app.state),
owner-scoped API-key authentication, added GET /workbook-imports list, health.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from urllib.parse import quote

from ..agent import tools
from ..agent.budget import AgentBudget
from ..agent.context_window import estimate_tokens
from ..agent.multi_workbook import run_multi_workbook_agent
from ..agent.general_chat import run_general_chat
from ..agent.session import AgentConversation
from ..agent.react_loop import run_react, has_tool_protocol
from ..analysis.profile import build_workbook_profile
from ..analysis.routing import select_analysis_route
from ..conversation import ExcelWorkbookConversation
from ..core import blocking_func_to_async, http_error
from ..service.llm import ExcelLLM
from ..service.service import WorkbookImportService
from ..service.upload_stream import spool_upload
from ..security import AuthConfigurationError, AuthenticationError
from .schemas import (
    ConfirmRequest,
    ConfirmResponse,
    ConversationDetail,
    ConversationMessage,
    ConversationSummary,
    DraftUpdateRequest,
    SQLQueryRequest,
    SQLQueryResponse,
    WorkbookImportResponse,
)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    max_rows: int = Field(default=500, ge=1, le=1000)
    # Optional conversation (session) support.  Pass an existing conv_id to
    # continue a multi-turn conversation; omit to start a new one.
    conv_id: Optional[str] = None
    # Optional model selection. Matches the `name` field in `excel.llms`
    # (e.g. "deepseek-v4-flash", "Qwen3-4B", or
    # "Qwen3-14B-AWQ"). Defaults to excel.llm.
    model: Optional[str] = None
    # Optional route override for controlled A/B tests.  Otherwise the selected
    # model's config metadata and the 30B boundary choose the route.
    analysis_route: Optional[Literal["large", "small"]] = None


class MultiWorkbookScope(BaseModel):
    mode: Literal["auto", "selected", "space"] = "auto"
    workbook_ids: List[str] = Field(default_factory=list, max_length=5)
    space_id: Optional[str] = None


class MultiWorkbookAskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    scope: MultiWorkbookScope = Field(default_factory=MultiWorkbookScope)
    max_rows: int = Field(default=500, ge=1, le=1000)
    model: Optional[str] = None
    conv_id: Optional[str] = None


class DataSpaceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=512)
    description: str = Field(default="", max_length=4000)
    workbook_ids: List[str] = Field(default_factory=list, max_length=100)


class DatasetRelationRequest(BaseModel):
    left_workbook_id: str
    left_table: str
    left_column: str
    right_workbook_id: str
    right_table: str
    right_column: str
    relation_type: str = "equality"
    cardinality: Optional[str] = None

logger = logging.getLogger(__name__)


_REPORT_PDF_FONT_ENV = "EXCEL_REPORT_PDF_FONT_PATH"
_REPORT_PDF_EMOJI_FONT_ENV = "EXCEL_REPORT_PDF_EMOJI_FONT_PATH"
_REPORT_PDF_FONT_CANDIDATES = (
    # Common Linux CJK font locations.
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    # WSL commonly has the Windows fonts mounted here.  The explicit
    # @font-face below is required because fontconfig does not necessarily
    # index /mnt/c/Windows/Fonts.
    "/mnt/c/Windows/Fonts/msyh.ttc",
    "/mnt/c/Windows/Fonts/simsun.ttc",
)
_REPORT_PDF_EMOJI_FONT_CANDIDATES = (
    # The WSL/Windows deployment has Segoe UI Emoji; Linux deployments may
    # provide the equivalent Noto Color Emoji font.
    "/mnt/c/Windows/Fonts/seguiemj.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf",
)
_CJK_TEXT_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _report_pdf_font_path(service: WorkbookImportService) -> Optional[Path]:
    """Find a local font that can render Chinese report text.

    WeasyPrint otherwise falls back to DejaVu Sans in a minimal Linux/WSL
    image.  That font has no CJK glyphs, so the PDF contains ``.notdef``
    boxes even though the source HTML is UTF-8.  Prefer an explicit setting
    and then check the standard fonts used by the supported environments.
    """
    raw = getattr(getattr(service, "config", None), "raw", {}) or {}
    server = raw.get("server", {}) if isinstance(raw, dict) else {}
    configured = (
        os.environ.get(_REPORT_PDF_FONT_ENV)
        or (server.get("pdf_font_path") if isinstance(server, dict) else None)
        or ""
    ).strip()
    candidates = [configured] if configured else []
    candidates.extend(_REPORT_PDF_FONT_CANDIDATES)
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    return None


def _report_pdf_emoji_font_path() -> Optional[Path]:
    """Find an emoji font for report icons without changing stored HTML."""
    configured = os.environ.get(_REPORT_PDF_EMOJI_FONT_ENV, "").strip()
    candidates = [configured] if configured else []
    candidates.extend(_REPORT_PDF_EMOJI_FONT_CANDIDATES)
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    return None


def _report_pdf_font_css(
    font_path: Path, emoji_font_path: Optional[Path] = None
) -> str:
    """Embed CJK text and an optional emoji fallback in the PDF stylesheet."""
    font_uri = font_path.resolve().as_uri()
    css = (
        # WeasyPrint's default 2cm page margin is not part of the stored HTML
        # and makes the PDF visibly differ from the iframe preview. Let the
        # report's own body padding and layout determine the page content.
        "@page { margin: 0; }"
        "@font-face {"
        "font-family: 'ExcelReportCJK';"
        f"src: url('{font_uri}');"
        "font-style: normal; font-weight: normal;"
        "}"
        "body, body * {"
        "font-family: 'ExcelReportCJK', 'ExcelReportEmoji', 'Segoe UI', sans-serif !important;"
        "}"
    )
    if emoji_font_path:
        emoji_uri = emoji_font_path.resolve().as_uri()
        css = (
            "@font-face {"
            "font-family: 'ExcelReportEmoji';"
            f"src: url('{emoji_uri}');"
            "font-style: normal; font-weight: normal;"
            "}"
            + css
        )
    return css


def get_service(request: Request) -> WorkbookImportService:
    return request.app.state.excel_service


def authenticate_request(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> str:
    if request.url.path == "/api/v1/health":
        return "health"
    try:
        principal = request.app.state.authenticator.authenticate(
            authorization or "", x_api_key or ""
        )
    except AuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=401,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    request.state.principal = principal
    return principal


def require_workbook_owner(
    workbook_id: str,
    principal: str = Depends(authenticate_request),
    service: WorkbookImportService = Depends(get_service),
) -> str:
    try:
        service.assert_workbook_owner(workbook_id, principal)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Workbook not found") from exc
    return principal


def require_import_owner(
    import_id: str,
    principal: str = Depends(authenticate_request),
    service: WorkbookImportService = Depends(get_service),
) -> str:
    try:
        service.assert_import_owner(import_id, principal)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Workbook import not found") from exc
    return principal


router = APIRouter(dependencies=[Depends(authenticate_request)])


def prepare_conversation_turn(
    conv: ExcelWorkbookConversation,
    question: str,
    meta: Optional[dict] = None,
):
    """Snapshot prior history before persisting the current user turn."""
    history = conv.get_history_messages()
    conv.start_new_round()
    if meta is None:
        # Keep compatibility with lightweight conversation doubles used by
        # callers/tests that implement the original one-argument method.
        conv.add_user_message(question)
    else:
        conv.add_user_message(question, meta=meta)
    return history


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.post("/workbook-imports", response_model=WorkbookImportResponse)
async def create_import(
    file: UploadFile = File(...),
    source_channel: str = Form(...),
    workbook_id: Optional[str] = Form(default=None),
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request),
):
    staged_path = None
    try:
        max_bytes = service.config.max_import_bytes
        incoming_dir = Path(service.config.resolved_storage_path()) / ".incoming"
        staged_path, size, source_hash = await spool_upload(
            file, str(incoming_dir), max_bytes
        )
        response = await blocking_func_to_async(
            service.create_import_from_path,
            file.filename or "workbook.xlsx",
            staged_path,
            source_channel,
            workbook_id,
            principal,
            size,
            source_hash,
        )
        return response
    except Exception as exc:
        logger.exception("Failed to create workbook import")
        raise http_error(exc) from exc
    finally:
        await file.close()
        if staged_path:
            Path(staged_path).unlink(missing_ok=True)


@router.get("/workbook-imports", response_model=List[WorkbookImportResponse])
async def list_imports(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request),
):
    try:
        return service.list_imports(
            status=status, limit=limit, offset=offset, owner_id=principal
        )
    except Exception as exc:
        raise http_error(exc) from exc


@router.get("/workbook-imports/{import_id}", response_model=WorkbookImportResponse)
async def get_import(
    import_id: str,
    service: WorkbookImportService = Depends(get_service),
    _principal: str = Depends(require_import_owner),
):
    try:
        return service.get_import(import_id)
    except Exception as exc:
        raise http_error(exc) from exc


@router.get("/workbook-imports/{import_id}/sheets/{sheet_id}/rows")
async def get_sheet_rows(
    import_id: str,
    sheet_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    service: WorkbookImportService = Depends(get_service),
    _principal: str = Depends(require_import_owner),
):
    try:
        return service.get_sheet_rows(import_id, sheet_id, offset, limit)
    except Exception as exc:
        raise http_error(exc) from exc


@router.patch(
    "/workbook-imports/{import_id}/draft",
    response_model=WorkbookImportResponse,
)
async def update_draft(
    import_id: str,
    request: DraftUpdateRequest,
    service: WorkbookImportService = Depends(get_service),
    _principal: str = Depends(require_import_owner),
):
    try:
        return service.update_draft(import_id, request)
    except Exception as exc:
        raise http_error(exc) from exc


@router.post(
    "/workbook-imports/{import_id}/validate",
    response_model=WorkbookImportResponse,
)
async def validate_import(
    import_id: str,
    service: WorkbookImportService = Depends(get_service),
    _principal: str = Depends(require_import_owner),
):
    try:
        return service.validate_import(import_id)
    except Exception as exc:
        raise http_error(exc) from exc


@router.post(
    "/workbook-imports/{import_id}/confirm",
    response_model=ConfirmResponse,
)
async def confirm_import(
    import_id: str,
    request: ConfirmRequest,
    service: WorkbookImportService = Depends(get_service),
    _principal: str = Depends(require_import_owner),
):
    try:
        response = await blocking_func_to_async(
            service.confirm_import,
            import_id,
            request.revision,
            request.accepted_warning_codes,
        )
        return response
    except Exception as exc:
        logger.exception("Failed to confirm workbook import")
        raise http_error(exc) from exc


@router.post(
    "/workbooks/{workbook_id}/versions/{version}/activate",
    response_model=ConfirmResponse,
)
async def activate_version(
    workbook_id: str,
    version: int,
    service: WorkbookImportService = Depends(get_service),
    _principal: str = Depends(require_workbook_owner),
):
    try:
        return service.activate_version(workbook_id, version)
    except Exception as exc:
        raise http_error(exc) from exc


@router.delete("/workbooks/{workbook_id}")
async def delete_workbook(
    workbook_id: str,
    service: WorkbookImportService = Depends(get_service),
    _principal: str = Depends(require_workbook_owner),
):
    """Delete a workbook (metadata + all on-disk artifacts)."""
    try:
        await blocking_func_to_async(service.delete_workbook, workbook_id)
        return {"status": "deleted", "workbook_id": workbook_id}
    except Exception as exc:
        raise http_error(exc) from exc


@router.post("/workbooks/{workbook_id}/query", response_model=SQLQueryResponse)
async def query_workbook(
    workbook_id: str,
    request: SQLQueryRequest,
    service: WorkbookImportService = Depends(get_service),
    _principal: str = Depends(require_workbook_owner),
):
    try:
        response = await blocking_func_to_async(
            service.execute_readonly_sql,
            workbook_id,
            request.sql,
            request.max_rows,
        )
        return response
    except Exception as exc:
        raise http_error(exc) from exc


def _sse(event: dict) -> str:
    import json as _json

    # Some reverse proxies buffer small text/event-stream chunks until their
    # buffer fills. An SSE comment is ignored by clients, but the padding
    # makes each visible event flush promptly for slow local-vLLM rounds.
    padding = " " * 2048
    return (
        f": stream-flush{padding}\n"
        f"data: {_json.dumps(event, ensure_ascii=False)}\n\n"
    )


def _matches_model_config(item: dict, requested: str) -> bool:
    """Match a canonical model name and any rollout aliases."""
    if item.get("name") == requested or item.get("model") == requested:
        return True
    aliases = item.get("aliases") or []
    return isinstance(aliases, list) and requested in aliases


def resolve_model_config(config, requested: Optional[str]) -> dict:
    if not requested:
        return config.llm
    for item in config.llms:
        if _matches_model_config(item, requested):
            return item
    if _matches_model_config(config.llm, requested):
        return config.llm
    raise HTTPException(status_code=422, detail=f"Unknown model: {requested}")


@router.post("/workbooks/{workbook_id}/ask")
async def ask_workbook(
    workbook_id: str,
    request: AskRequest,
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(require_workbook_owner),
):
    """SSE ReAct Text2SQL + HTML report loop (with optional session history)."""
    service.active_database_path(workbook_id)  # 404 early if no active DB
    run_id = request.conv_id or uuid.uuid4().hex
    if not service.try_acquire_agent_run(run_id, [workbook_id]):
        raise HTTPException(
            status_code=429,
            detail="Agent concurrency limit reached; retry later",
            headers={"Retry-After": "5"},
        )
    try:
        # Load or create a conversation only after reserving execution capacity.
        if request.conv_id:
            conv = ExcelWorkbookConversation.load(
                request.conv_id, workbook_id, owner_id=principal
            )
            if not conv:
                raise HTTPException(
                    status_code=404,
                    detail=f"Conversation not found: {request.conv_id}",
                )
        else:
            conv = ExcelWorkbookConversation.create(
                workbook_id, title=request.question[:50], owner_id=principal
            )
        llm_config = resolve_model_config(service.config, request.model)
        legacy_route = select_analysis_route(
            llm_config, requested_route=request.analysis_route
        )
        # All question answering now uses the model-driven Agent loop. Keep
        # the old route metadata as a diagnostic hint for compatibility, but
        # never fall back to the deterministic small/template route.
        route = {
            **legacy_route,
            "route": "agent",
            "reason": f"agent-only ({legacy_route.get('reason', 'configured')})",
        }
        llm = ExcelLLM(llm_config)
        configured_model_name = (
            llm_config.get("name")
            or request.model
            or llm_config.get("model")
            or "template"
        )
        model_id = (
            getattr(llm, "model_name", None)
            or llm_config.get("model")
            or configured_model_name
        )
        model_context = {
            "model_name": configured_model_name,
            "model_label": llm_config.get("label") or configured_model_name,
            "model_id": model_id,
            "analysis_route": route["route"],
            "analysis_reason": route["reason"],
            "model_size_b": route.get("model_size_b"),
        }
        conv.set_model_context(**model_context)
        history = prepare_conversation_turn(
            conv,
            request.question,
            meta=model_context,
        )
        # The legacy profile/template implementation remains available as a
        # regression tool, but is no longer part of the default ask route.
        structured_report = False
        max_loops = int((service.config.agent or {}).get("max_loops", 8))
        budget = AgentBudget.from_config(
            service.config.agent, prior=conv.get_budget_usage()
        )
        llm.deadline = budget.started + budget.max_seconds
        service.bind_agent_run(run_id, conv.conv_id, [workbook_id])
        run_id = conv.conv_id
    except Exception as exc:
        service.release_agent_run(run_id)
        raise http_error(exc) from exc
    logger.info(
        "ask conv=%s wid=%s model=%s route=%s reason=%s question=%r",
        conv.conv_id,
        workbook_id,
        model_id,
        route["route"],
        route["reason"],
        request.question[:200],
    )

    def _record_tool_step_full(action, observation, detail):
        """Persist one ReAct tool step as a 'tool' message with structured
        detail (sql/table/html) so the frontend can rebuild the full execution
        record of that turn."""
        try:
            # Keep the complete bounded SQL observation for conversation restore;
            # the SQL service/agent max_rows setting is the authoritative limit.
            conv.add_tool_message(action, observation, detail=detail)
        except Exception:
            logger.exception("Failed to persist tool step")

    def generate():
        final_answer = ""
        answer_persisted = False
        try:
            yield _sse({
                "type": "session",
                "conv_id": conv.conv_id,
                "done": False,
                **conv.model_context(),
            })
            yield _sse({
                "type": "analysis_route",
                "route": route["route"],
                "reason": route["reason"],
                "model_size_b": route.get("model_size_b"),
                "model_name": model_context["model_name"],
                "model_label": model_context["model_label"],
                "model_id": model_context["model_id"],
            })
            events = run_react(
                llm,
                service,
                workbook_id,
                request.question,
                history_messages=history,
                max_loops=max_loops,
                sql_max_rows=request.max_rows,
                on_tool_step_full=_record_tool_step_full,
                budget=budget,
                structured_report=structured_report,
            )
            for event in events:
                # DB-GPT's StorageConversation keeps user-facing render
                # artifacts as ViewMessage records. Persist only the report
                # locator/metadata there; the auditable tool detail carries
                # the normalized HTML when the route provides one.
                if event.get("type") == "html" and not event.get("error"):
                    try:
                        conv.add_view_message(
                            json.dumps(
                                {
                                    "type": "html",
                                    "title": event.get("title") or "分析报告",
                                    "report_id": event.get("report_id"),
                                    "url": event.get("url"),
                                },
                                ensure_ascii=False,
                            )
                        )
                    except Exception:
                        logger.exception("Failed to persist report view message")
                if event.get("type") == "done":
                    final_answer = event.get("full_answer", "") or ""
                    event = {
                        **event,
                        "conv_id": conv.conv_id,
                        "analysis_route": route["route"],
                        "model_name": model_context["model_name"],
                        "model_label": model_context["model_label"],
                        "model_id": model_context["model_id"],
                        "model_size_b": model_context["model_size_b"],
                    }
                    # Persist before exposing `done` to the client.  The UI
                    # refreshes its conversation list upon completion; if the
                    # event arrives first, that refresh can replace the live
                    # answer with an older history snapshot.
                    if final_answer:
                        try:
                            conv.add_ai_message(final_answer)
                            answer_persisted = True
                            logger.info(
                                "ask done conv=%s answer=%r",
                                conv.conv_id,
                                final_answer[:200],
                            )
                        except Exception:
                            logger.exception("Failed to persist final answer")
                yield _sse(event)
        except Exception as exc:
            logging.getLogger(__name__).exception("ask failed")
            yield _sse({
                "type": "error", "error": str(exc), "done": True,
                "conv_id": conv.conv_id,
                "model_name": model_context["model_name"],
                "model_label": model_context["model_label"],
                "model_id": model_context["model_id"],
                "analysis_route": model_context["analysis_route"],
            })
        finally:
            try:
                conv.add_budget_usage(budget.conversation_usage())
            except Exception:
                logger.exception("Failed to persist agent budget usage")
            if final_answer and not answer_persisted:
                conv.add_ai_message(final_answer)
                logger.info("ask done conv=%s answer=%r", conv.conv_id, final_answer[:200])
            elif conv.messages:
                # Still persist the round even if no answer (e.g. error).
                pass
            service.release_agent_run(run_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity",
        },
    )


@router.get("/data-catalog")
async def list_data_catalog(
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request),
):
    """Return metadata-only catalogs for the authenticated user's workbooks."""

    try:
        return await blocking_func_to_async(
            service.list_workbook_catalog, principal, limit=200
        )
    except Exception as exc:
        raise http_error(exc) from exc


@router.get("/data-catalog/search")
async def search_data_catalog(
    q: str = Query(default="", max_length=300),
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request),
):
    """Search only the authenticated user's metadata-only catalog."""

    try:
        return await blocking_func_to_async(
            service.search_workbook_catalog, principal, q
        )
    except Exception as exc:
        raise http_error(exc) from exc


@router.get("/data-spaces")
async def list_data_spaces(
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request),
):
    return await blocking_func_to_async(service.list_data_spaces, principal)


@router.post("/data-spaces")
async def create_data_space(
    request: DataSpaceRequest,
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request),
):
    try:
        return await blocking_func_to_async(
            service.create_data_space,
            principal,
            request.name,
            request.description,
            request.workbook_ids,
        )
    except Exception as exc:
        raise http_error(exc) from exc


@router.patch("/data-spaces/{space_id}")
async def update_data_space(
    space_id: str,
    request: DataSpaceRequest,
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request),
):
    try:
        return await blocking_func_to_async(
            service.update_data_space,
            space_id,
            principal,
            request.name,
            request.description,
            request.workbook_ids,
        )
    except Exception as exc:
        raise http_error(exc) from exc


@router.delete("/data-spaces/{space_id}")
async def delete_data_space(
    space_id: str,
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request),
):
    try:
        await blocking_func_to_async(service.delete_data_space, space_id, principal)
        return {"space_id": space_id, "deleted": True}
    except Exception as exc:
        raise http_error(exc) from exc


@router.get("/dataset-relations")
async def list_dataset_relations(
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request),
):
    return await blocking_func_to_async(service.list_dataset_relations, principal)


@router.post("/dataset-relations")
async def create_dataset_relation(
    request: DatasetRelationRequest,
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request),
):
    try:
        return await blocking_func_to_async(
            service.create_dataset_relation, principal, request.model_dump()
        )
    except Exception as exc:
        raise http_error(exc) from exc


@router.delete("/dataset-relations/{relation_id}")
async def delete_dataset_relation(
    relation_id: str,
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request),
):
    try:
        await blocking_func_to_async(
            service.delete_dataset_relation, principal, relation_id
        )
        return {"relation_id": relation_id, "deleted": True}
    except Exception as exc:
        raise http_error(exc) from exc


def _agent_conversation_dict(conv: AgentConversation, service=None) -> dict:
    context = conv.context_state()
    if not context:
        try:
            model = resolve_model_config(service.config,conv.model_name) if service else {}
        except ValueError:
            model = {}
        context = {'total_tokens':int(model.get('context_window_tokens') or ((service.config.agent or {}) if service else {}).get('context_window_tokens',32000)),
                   'used_tokens':estimate_tokens(conv.history_messages()),'estimated':True,'compressions':0,
                   'conversation_total_tokens':conv.get_budget_usage().get('estimated_tokens',0),'auto_compress':True}
    return {
        "conv_id": conv.conv_id,
        "owner_id": conv.owner_id,
        "scope_type": conv.scope_type,
        "scope": conv.scope_json,
        "sources": conv.source_snapshot,
        "title": conv.title,
        "model_name": conv.model_name,
        "model_label": conv.model_label,
        "model_id": conv.model_id,
        "context": context,
        "messages": [dict(message, content='该历史回复包含未执行的工具指令，已隐藏。请重新提问。', protocol_invalid=True)
                     if message.get('role') == 'ai' and has_tool_protocol(message.get('content')) else message
                     for message in conv.messages if message.get('role') not in {'context','context_checkpoint'}],
    }


@router.get("/agent/conversations")
async def list_agent_conversations(
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request),
):
    """List global Agent conversations for the authenticated principal."""

    from ..models.agent import AgentConversationDao

    rows = AgentConversationDao().list(principal)
    return [
        {
            "conv_id": row.conv_id,
            "scope_type": row.scope_type,
            "scope": json.loads(row.scope_json or "{}"),
            "sources": json.loads(row.source_snapshot_json or "[]"),
            "title": row.title,
            "pinned": bool(row.pinned),
            "model_name": row.model_name,
            "model_label": row.model_label,
            "model_id": row.model_id,
            "created_at": row.gmt_created.isoformat() if row.gmt_created else None,
            "updated_at": row.gmt_modified.isoformat() if row.gmt_modified else None,
        }
        for row in rows
    ]


@router.get("/agent/conversations/{conv_id}")
async def get_agent_conversation(
    conv_id: str,
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request),
):
    conv = AgentConversation.load(conv_id, principal)
    if not conv:
        raise HTTPException(status_code=404, detail="Agent conversation not found")
    return _agent_conversation_dict(conv,service)


@router.delete("/agent/conversations/{conv_id}")
async def delete_agent_conversation(
    conv_id: str,
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request),
):
    from ..models.agent import AgentConversationDao

    if not AgentConversationDao().get(conv_id, principal):
        raise HTTPException(status_code=404, detail="Agent conversation not found")
    from ..service.lifecycle import cleanup
    try:
        result = await blocking_func_to_async(cleanup, service, conv_id=conv_id, owner_id=principal)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"conv_id": conv_id, "deleted": True, **result}


@router.post("/agent/ask")
async def ask_multi_workbook(
    request: MultiWorkbookAskRequest,
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request),
):
    """Owner-scoped multi-workbook Agent with isolated per-source SQL."""
    run_started = time.monotonic()

    general_chat = request.scope.mode != 'space' and not request.scope.workbook_ids
    if not general_chat and not getattr(service.config, "multi_workbook_enabled", True):
        raise HTTPException(
            status_code=503,
            detail="Multi-workbook Agent is disabled by configuration",
        )

    if request.scope.mode == "space" and not request.scope.space_id:
        raise HTTPException(
            status_code=422,
            detail="scope.space_id is required when scope.mode is space",
        )
    run_id = request.conv_id or uuid.uuid4().hex
    if not service.try_acquire_agent_run(run_id, request.scope.workbook_ids):
        raise HTTPException(
            status_code=429,
            detail="Agent concurrency limit reached; retry later",
            headers={"Retry-After": "5"},
        )
    try:
        catalogs = [] if general_chat else await blocking_func_to_async(
            service.list_workbook_catalog, principal, limit=200
        )
        conv = None
        if request.conv_id:
            conv = AgentConversation.load(request.conv_id, principal)
            if not conv:
                raise HTTPException(status_code=404, detail="Agent conversation not found")
        llm_config = resolve_model_config(service.config, request.model or (conv.model_name if conv else None))
        llm = ExcelLLM(llm_config)
        model_context = {
            "model_name": llm_config.get("name") or request.model or llm_config.get("model") or "agent",
            "model_label": llm_config.get("label") or llm_config.get("name") or request.model or "agent",
            "model_id": getattr(llm, "model_name", None) or llm_config.get("model") or request.model or "agent",
            "analysis_route": "general" if general_chat else "agent",
        }
        max_loops = int((service.config.agent or {}).get("max_loops", 10))
        if request.conv_id:
            history = conv.history_messages()
        else:
            conv = AgentConversation.create(
                principal,
                request.scope.mode,
                request.scope.model_dump(),
                title=request.question[:80],
            )
            history = []
        budget = AgentBudget.from_config(service.config.agent, prior=conv.get_budget_usage())
        llm.deadline = budget.started + budget.max_seconds
        service.bind_agent_run(run_id, conv.conv_id, request.scope.workbook_ids)
        run_id = conv.conv_id
        scope = request.scope.model_dump()
        if 'attachments' in conv.scope_json:
            attachments = conv.scope_json['attachments']
            ready_ids = {a['workbook_id'] for a in attachments if a['status'] == 'ready'}
            if general_chat and not ready_ids:
                scope['mode'] = 'selected'
            if scope['mode'] != 'selected' or not set(scope['workbook_ids']).issubset(ready_ids):
                raise HTTPException(422, detail='Query scope must contain only ready conversation attachments')
            scope['attachments'] = attachments
        conv.set_scope(scope)
        if not any(m.get('role') == 'user' for m in conv.messages):
            from ..models.agent import AgentConversationDao
            conv.title = request.question[:80]
            AgentConversationDao().update(conv.conv_id, principal, {'title': conv.title})
        conv.set_context(
            model_name=model_context["model_name"],
            model_label=model_context["model_label"],
            model_id=model_context["model_id"],
        )
        conv.start_new_round()
        conv.add_user(request.question, meta=model_context)
    except Exception as exc:
        service.release_agent_run(run_id)
        raise http_error(exc) from exc

    try:
        if request.scope.mode == "space":
            space = await blocking_func_to_async(
                service.get_data_space, request.scope.space_id, principal
            )
            selected_ids = space.get("workbook_ids") or []
        else:
            selected_ids = request.scope.workbook_ids if request.scope.mode == "selected" else None
        if not general_chat and selected_ids is not None and not selected_ids:
            raise HTTPException(status_code=422, detail="Selected data space contains no workbooks")
        relation_ids = selected_ids
        relations = [] if general_chat else await blocking_func_to_async(
            service.list_dataset_relations, principal, relation_ids
        )
        service.bind_agent_run(run_id, conv.conv_id, selected_ids or [c['workbook_id'] for c in catalogs])
    except Exception as exc:
        service.release_agent_run(run_id)
        raise http_error(exc) from exc

    def generate():
        last_context = None
        answer_attachments = []
        try:
            yield _sse({
                "type": "session",
                "conv_id": conv.conv_id,
                "done": False,
                **model_context,
                "scope_mode": request.scope.mode,
            })
            if general_chat:
                conv.set_snapshot([])
                events = run_general_chat(llm, request.question, history, budget,
                    total_tokens=int(llm_config.get('context_window_tokens') or (service.config.agent or {}).get('context_window_tokens', 32000)),
                    output_reserve=int(llm_config.get('max_tokens') or getattr(llm, 'max_tokens', 4096)),
                    prior_compressions=int(conv.context_state().get('compressions', 0)))
            else:
                events = run_multi_workbook_agent(
                llm,
                service,
                owner_id=principal,
                question=request.question,
                catalogs=catalogs,
                selected_workbook_ids=selected_ids,
                history_messages=history,
                relations=relations,
                max_selected_sources=getattr(
                    service.config, "multi_workbook_max_selected", 5
                ),
                max_queried_sources=getattr(
                    service.config, "multi_workbook_max_queried", 3
                ),
                max_loops=max_loops,
                sql_max_rows=request.max_rows,
                budget=budget,
                conversation_id=conv.conv_id,
                context_window_tokens=int(llm_config.get('context_window_tokens') or (service.config.agent or {}).get('context_window_tokens',32000)),
                context_output_reserve=int(llm_config.get('max_tokens') or getattr(llm,'max_tokens',4096)),
                context_auto_compress=bool((service.config.agent or {}).get('context_auto_compress',True)),
                context_prior_compressions=int(conv.context_state().get('compressions',0)),
            )
            for event in events:
                if event.get('type') == 'context.checkpoint':
                    conv.add_context_checkpoint(event['snapshot'])
                    continue
                if event.get('type') == 'context.usage':
                    last_context = event['context']
                if event.get("type") == "scope.resolved":
                    conv.set_snapshot(event.get("sources") or [])
                    answer_attachments = [dict(s) for s in event.get('sources') or []]
                elif event.get("type") in {
                    "source.inspect",
                    "source.query.result",
                    "source.query.error",
                    "aggregate.result",
                    "catalog.candidates",
                    "step.done",
                }:
                    conv.add_tool(
                        str(event.get("type")),
                        json.dumps(event, ensure_ascii=False)[:120_000],
                        detail=event,
                    )
                if event.get("type") == "done":
                    timing = {'elapsed_ms': round((time.monotonic()-run_started)*1000),
                              'completed_at': datetime.now(timezone.utc).isoformat()}
                    if event.get("full_answer"):
                        conv.add_ai(event["full_answer"], meta={'timing': timing, 'attachments': answer_attachments,
                            'sources': event.get('sources') or []})
                    event = {
                        **event,
                        "timing": timing,
                        "attachments": answer_attachments,
                        "conv_id": conv.conv_id,
                        "analysis_route": "agent",
                        **model_context,
                    }
                yield _sse(event)
        except Exception as exc:
            logger.exception("multi-workbook ask failed")
            yield _sse({
                "type": "error",
                "error": str(exc),
                "done": True,
                "analysis_route": "agent",
                **model_context,
            })
        finally:
            try:
                if last_context:
                    conv.add_context_usage(last_context)
                conv.add_budget_usage(budget.conversation_usage())
            except Exception:
                logger.exception('Failed to persist multi-workbook budget')
            service.release_agent_run(run_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity",
        },
    )


@router.get("/workbooks/{workbook_id}/analysis-profile")
async def get_analysis_profile(
    workbook_id: str,
    service: WorkbookImportService = Depends(get_service),
    _principal: str = Depends(require_workbook_owner),
):
    """Return the deterministic generic profile used by the small route."""
    try:
        return await blocking_func_to_async(build_workbook_profile, service, workbook_id)
    except Exception as exc:
        raise http_error(exc) from exc


def _owned_agent_report(service, conv_id: str, report_id: str, principal: str) -> str:
    if not AgentConversation.load(conv_id, principal):
        raise HTTPException(status_code=404, detail='Agent conversation not found')
    path = tools.agent_report_file_path(service, conv_id, report_id)
    if not path:
        raise HTTPException(status_code=404, detail='Report not found')
    return path


@router.get('/agent/conversations/{conv_id}/reports/{report_id}')
async def get_agent_report(conv_id: str, report_id: str,
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request)):
    path = _owned_agent_report(service, conv_id, report_id, principal)
    return FileResponse(path, media_type='text/html; charset=utf-8', headers={
        'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox allow-modals",
        'X-Content-Type-Options':'nosniff', 'Referrer-Policy':'no-referrer',
    })


@router.get('/agent/conversations/{conv_id}/reports/{report_id}/pdf')
async def get_agent_report_pdf(conv_id: str, report_id: str,
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(authenticate_request)):
    path = _owned_agent_report(service, conv_id, report_id, principal)
    return _render_report_pdf(path, service)


@router.get("/workbooks/{workbook_id}/reports/{report_id}")
async def get_report(
    workbook_id: str,
    report_id: str,
    service: WorkbookImportService = Depends(get_service),
    _principal: str = Depends(require_workbook_owner),
):
    path = tools.report_file_path(service, workbook_id, report_id)
    if not path:
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(
        path,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": (
                "default-src 'none'; base-uri 'none'; form-action 'none'; "
                "style-src 'unsafe-inline'; frame-ancestors 'self'; sandbox allow-modals"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/workbooks/{workbook_id}/reports/{report_id}/pdf")
async def get_report_pdf(
    workbook_id: str,
    report_id: str,
    service: WorkbookImportService = Depends(get_service),
    _principal: str = Depends(require_workbook_owner),
):
    """Render a stored passive HTML report as a downloadable PDF.

    Browser-side ``window.print`` is kept as a frontend fallback, but it is
    not a download and is blocked by some iframe/CSP combinations.  Rendering
    on the server makes the PDF action deterministic and preserves the exact
    stored report as its source.
    """
    path = tools.report_file_path(service, workbook_id, report_id)
    if not path:
        raise HTTPException(status_code=404, detail="Report not found")
    return _render_report_pdf(path, service)


def _render_report_pdf(path: str, service: WorkbookImportService):
    try:
        from weasyprint import CSS, HTML
        from weasyprint.text.fonts import FontConfiguration

        html_path = Path(path)
        html_source = html_path.read_text(encoding="utf-8")
        font_path = _report_pdf_font_path(service)
        emoji_font_path = _report_pdf_emoji_font_path()
        font_config = FontConfiguration()
        stylesheets = []
        if font_path:
            logger.info(
                "Using report PDF fonts: cjk=%s emoji=%s",
                font_path,
                emoji_font_path or "fallback system symbols",
            )
            stylesheets.append(
                CSS(
                    string=_report_pdf_font_css(font_path, emoji_font_path),
                    base_url=str(html_path.parent),
                    font_config=font_config,
                )
            )
        elif _CJK_TEXT_RE.search(html_source):
            raise RuntimeError(
                "No Chinese font is available for PDF rendering. Set "
                f"{_REPORT_PDF_FONT_ENV} to a local .ttf/.otf/.ttc font "
                "(for example Noto Sans CJK or Microsoft YaHei)."
            )
        pdf_bytes = HTML(
            filename=str(html_path),
            base_url=str(html_path.parent),
        ).write_pdf(stylesheets=stylesheets, font_config=font_config)
    except (ImportError, OSError) as exc:  # pragma: no cover - packaging failure
        raise HTTPException(
            status_code=503,
            detail="PDF support is unavailable; install WeasyPrint and its native Pango/GObject libraries",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to render report PDF: %s", path)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to render report PDF: {exc}",
        ) from exc

    filename = f"{Path(path).stem}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="report.pdf"; '
                f"filename*=UTF-8''{quote(filename)}"
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ------------------------------------------------------------------ models

@router.get("/models")
async def list_models(
    service: WorkbookImportService = Depends(get_service),
):
    """List the configured LLMs (excel.llms + default excel.llm, de-duplicated)."""
    try:
        default = service.config.llm or {}
        default_name = default.get("name") or default.get("model") or "default"
        seen = set()
        items = []
        # Default model first.
        items.append({
            "name": default_name,
            "label": default.get("label") or default.get("model") or "Default",
            "default": True,
            "analysis_route": "agent",
            "legacy_analysis_route": select_analysis_route(default)["route"],
            "context_window_tokens":int(default.get('context_window_tokens') or (service.config.agent or {}).get('context_window_tokens',32000)),
            "output_reserve_tokens":int(default.get('max_tokens',4096)),
        })
        seen.add(default_name)
        for m in service.config.llms:
            name = m.get("name") or m.get("model")
            if not name or name in seen:
                continue
            seen.add(name)
            items.append({
                "name": name,
                "label": m.get("label") or m.get("model"),
                "default": False,
                "analysis_route": "agent",
                "legacy_analysis_route": select_analysis_route(m)["route"],
                "context_window_tokens":int(m.get('context_window_tokens') or (service.config.agent or {}).get('context_window_tokens',32000)),
                "output_reserve_tokens":int(m.get('max_tokens',4096)),
            })
        return items
    except Exception as exc:
        raise http_error(exc) from exc


# ------------------------------------------------------------------ conversations

@router.get(
    "/workbooks/{workbook_id}/conversations",
    response_model=List[ConversationSummary],
)
async def list_conversations(
    workbook_id: str,
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(require_workbook_owner),
):
    """List all conversations for a workbook (most recent first)."""
    try:
        return ExcelWorkbookConversation.list_workbook_conversations(
            workbook_id, owner_id=principal
        )
    except Exception as exc:
        raise http_error(exc) from exc


@router.get(
    "/workbooks/{workbook_id}/conversations/{conv_id}",
    response_model=ConversationDetail,
)
async def get_conversation(
    workbook_id: str,
    conv_id: str,
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(require_workbook_owner),
):
    """Get the full message list of one conversation."""
    try:
        conv = ExcelWorkbookConversation.load(
            conv_id, workbook_id, owner_id=principal
        )
        if not conv:
            raise ValueError(f"Conversation not found: {conv_id}")
        return ConversationDetail(
            conv_id=conv.conv_id,
            workbook_id=conv.workbook_id,
            **conv.model_context(),
            messages=[
                ConversationMessage(**m) for m in conv.messages
            ],
        )
    except Exception as exc:
        raise http_error(exc) from exc


@router.delete("/workbooks/{workbook_id}/conversations/{conv_id}")
async def delete_conversation(
    workbook_id: str,
    conv_id: str,
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(require_workbook_owner),
):
    """Delete one conversation."""
    try:
        conv = ExcelWorkbookConversation.load(
            conv_id, workbook_id, owner_id=principal
        )
        if not conv:
            raise ValueError(f"Conversation not found: {conv_id}")
        conv.delete()
        return {"status": "deleted", "conv_id": conv_id}
    except Exception as exc:
        raise http_error(exc) from exc
