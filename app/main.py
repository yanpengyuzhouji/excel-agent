"""FastAPI application entrypoint for the Excel workbook service."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api.endpoints import router
from . import __version__
from .api.attachments import router as attachments_router
from .config import build_app_config
from .db import ensure_tables
from .security import ApiKeyAuthenticator
from .service.service import WorkbookImportService


def _setup_logging() -> None:
    """Log to both console and a rotating file under <storage>/logs/.

    Keeps console output for interactive use and persists a rotating
    excel-agent.log so every question/answer/tool-call is auditable.
    """
    root = logging.getLogger()
    fmt = logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)s | %(message)s"
    )
    # Console handler (as before) — only add once.
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)
    # Rotating file handler under <storage>/logs/excel-agent.log.
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        try:
            storage = build_app_config().resolved_storage_path()
            log_dir = Path(storage) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                str(log_dir / "excel-agent.log"),
                maxBytes=10 * 1024 * 1024,   # 10 MB
                backupCount=5,
                encoding="utf-8",
            )
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except Exception as exc:  # pragma: no cover - logging must never crash startup
            logging.getLogger(__name__).warning(
                "Failed to configure file logging: %s", exc
            )
    root.setLevel(logging.INFO)


logger = logging.getLogger(__name__)

# Configure file + console logging at import time (uvicorn.run reconfigures
# the root logger afterwards, so also re-apply inside lifespan).
_setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    cfg = build_app_config()
    authenticator = ApiKeyAuthenticator.from_config(cfg.raw)
    authenticator.ensure_configured()
    cfg.resolved_storage_path()
    service = WorkbookImportService(config=cfg)
    service.init_app()
    ensure_tables()
    from .core import blocking_func_to_async
    from .service.lifecycle import recover_pending, cleanup_failed_imports
    await blocking_func_to_async(recover_pending, service)
    await blocking_func_to_async(cleanup_failed_imports, service)
    app.state.excel_service = service
    app.state.authenticator = authenticator
    logger.info("Excel workbook service ready; storage=%s", cfg.resolved_storage_path())
    yield


app = FastAPI(
    title="Excel Agent",
    description=(
        "Session Excel attachments -> automatic validated SQLite import -> "
        "multi-workbook read-only analysis and optional HTML reports"
    ),
    version=__version__,
    lifespan=lifespan,
)

app.include_router(router, prefix="/api/v1")
app.include_router(attachments_router, prefix="/api/v1")

# Optional minimal example frontend served at /ui (single-file, no build).
_UI_DIR = Path(__file__).resolve().parent.parent / "examples" / "frontend"
if _UI_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")
