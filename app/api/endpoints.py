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
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from urllib.parse import quote

from ..agent import tools
from ..agent.budget import AgentBudget
from ..agent.react_loop import run_react
from ..analysis.profile import build_workbook_profile
from ..analysis.routing import select_analysis_route
from ..analysis.small_route import run_small_route
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


@router.post("/workbooks/{workbook_id}/ask")
async def ask_workbook(
    workbook_id: str,
    request: AskRequest,
    service: WorkbookImportService = Depends(get_service),
    principal: str = Depends(require_workbook_owner),
):
    """SSE ReAct Text2SQL + HTML report loop (with optional session history)."""
    service.active_database_path(workbook_id)  # 404 early if no active DB
    if not service.try_acquire_agent_run():
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
        llm_config = service.config.llm
        if request.model:
            for item in service.config.llms:
                if _matches_model_config(item, request.model):
                    llm_config = item
                    break
        route = select_analysis_route(
            llm_config, requested_route=request.analysis_route
        )
        llm = ExcelLLM(llm_config) if route["route"] == "large" else None
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
        # Large keeps the existing model-driven ReAct path.  Small uses the
        # deterministic profile/template/report path and does not ask a small
        # model to invent SQL or HTML.
        structured_report = False
        max_loops = int((service.config.agent or {}).get("max_loops", 8))
        budget = AgentBudget.from_config(
            service.config.agent, prior=conv.get_budget_usage()
        )
    except BaseException:
        service.release_agent_run()
        raise
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
            if route["route"] == "small":
                events = run_small_route(
                    service,
                    workbook_id,
                    request.question,
                    title=request.question[:80] if "报告" in request.question else None,
                    on_tool_step_full=_record_tool_step_full,
                )
            else:
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
            if final_answer:
                conv.add_ai_message(final_answer)
                logger.info("ask done conv=%s answer=%r", conv.conv_id, final_answer[:200])
            elif conv.messages:
                # Still persist the round even if no answer (e.g. error).
                pass
            service.release_agent_run()

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
                "frame-ancestors 'self'; sandbox allow-modals"
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
    except ImportError as exc:  # pragma: no cover - packaging failure
        raise HTTPException(
            status_code=503,
            detail="PDF support is unavailable; install the weasyprint dependency",
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
        default_name = default.get("model") or default.get("name") or "default"
        seen = set()
        items = []
        # Default model first.
        items.append({
            "name": default_name,
            "label": default.get("label") or default.get("model") or "Default",
            "default": True,
            "analysis_route": select_analysis_route(default)["route"],
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
                "analysis_route": select_analysis_route(m)["route"],
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
