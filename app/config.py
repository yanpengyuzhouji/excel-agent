"""Microservice configuration loaded from config.yaml.

Replaces dbgpt_serve.workbook.config.ServeConfig (which depended on
dbgpt.configs.model_config.PILOT_PATH and dbgpt_serve.core.BaseServeConfig).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

_CONFIG_PATH = Path(os.environ.get('EXCEL_CONFIG_PATH') or Path(__file__).resolve().parent.parent / "config.yaml")


@dataclass
class AppConfig:
    """Flat view of the parts of config.yaml this service needs."""

    host: str = "127.0.0.1"
    port: int = 8090
    storage_path: str = "data/workbook_imports"
    preview_page_size: int = 100
    query_row_limit: int = 500
    # Import guardrails: reject over-large uploads / over-tall sheets before the
    # parser can exhaust memory (defaults keep a config without them working).
    max_import_bytes: int = 200 * 1024 * 1024
    max_import_rows: int = 100_000
    max_import_columns: int = 512
    max_import_cells: int = 2_000_000
    max_cell_chars: int = 32_767
    max_total_text_chars: int = 64 * 1024 * 1024
    max_uncompressed_bytes: int = 256 * 1024 * 1024
    max_zip_ratio: float = 200.0
    max_zip_entries: int = 10_000
    max_merged_cells: int = 200_000
    max_xml_entry_bytes: int = 64 * 1024 * 1024
    max_concurrent_imports: int = 2
    sql_timeout_ms: int = 5_000
    sql_vm_steps: int = 2_000_000
    max_concurrent_sql: int = 4
    max_concurrent_agent_runs: int = 2
    multi_workbook_enabled: bool = True
    multi_workbook_max_selected: int = 5
    multi_workbook_max_queried: int = 3
    llm: Dict[str, Any] = field(default_factory=dict)
    llms: List[Dict[str, Any]] = field(default_factory=list)
    agent: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    def resolved_storage_path(self) -> str:
        raw = self.storage_path
        if not os.path.isabs(raw):
            raw = str(Path(self._config_dir) / raw)
        Path(raw).mkdir(parents=True, exist_ok=True)
        return raw

    @property
    def _config_dir(self) -> Path:
        return _CONFIG_PATH.parent


_config_cache: Optional[Dict[str, Any]] = None


def load_config() -> Dict[str, Any]:
    global _config_cache
    if _config_cache is None:
        try:
            from dotenv import load_dotenv

            load_dotenv(_CONFIG_PATH.parent / ".env")
        except ImportError:
            pass
        with open(_CONFIG_PATH, "r", encoding="utf-8") as stream:
            _config_cache = yaml.safe_load(stream) or {}
        server = _config_cache.setdefault('server', {})
        for key, env in [('storage_path', 'EXCEL_STORAGE_PATH'), ('host', 'EXCEL_HOST'), ('port', 'EXCEL_PORT')]:
            if os.environ.get(env):
                server[key] = os.environ[env]
        if 'EXCEL_REQUIRE_AUTH' in os.environ:
            _config_cache.setdefault('security', {})['require_auth'] = os.environ['EXCEL_REQUIRE_AUTH'].lower() == 'true'
        llm = _config_cache.setdefault('excel', {}).setdefault('llm', {})
        for key, env in [('base_url', 'EXCEL_LLM_BASE_URL'), ('model', 'EXCEL_LLM_MODEL')]:
            if os.environ.get(env):
                llm[key] = os.environ[env]
        if os.environ.get('EXCEL_LLM_API_KEY'):
            llm['api_key_env'] = 'EXCEL_LLM_API_KEY'
    return _config_cache


def build_app_config(raw: Optional[Dict[str, Any]] = None) -> AppConfig:
    raw = raw or load_config()
    server = raw.get("server", {})
    excel = raw.get("excel", {})
    return AppConfig(
        host=server.get("host", "127.0.0.1"),
        port=int(server.get("port", 8090)),
        storage_path=server.get("storage_path", "data/workbook_imports"),
        preview_page_size=int(server.get("preview_page_size", 100)),
        query_row_limit=int(server.get("query_row_limit", 500)),
        max_import_bytes=int(server.get("max_import_bytes", 200 * 1024 * 1024)),
        max_import_rows=int(server.get("max_import_rows", 100_000)),
        max_import_columns=int(server.get("max_import_columns", 512)),
        max_import_cells=int(server.get("max_import_cells", 2_000_000)),
        max_cell_chars=int(server.get("max_cell_chars", 32_767)),
        max_total_text_chars=int(
            server.get("max_total_text_chars", 64 * 1024 * 1024)
        ),
        max_uncompressed_bytes=int(
            server.get("max_uncompressed_bytes", 256 * 1024 * 1024)
        ),
        max_zip_ratio=float(server.get("max_zip_ratio", 200.0)),
        max_zip_entries=int(server.get("max_zip_entries", 10_000)),
        max_merged_cells=int(server.get("max_merged_cells", 200_000)),
        max_xml_entry_bytes=int(
            server.get("max_xml_entry_bytes", 64 * 1024 * 1024)
        ),
        max_concurrent_imports=int(server.get("max_concurrent_imports", 2)),
        sql_timeout_ms=int(server.get("sql_timeout_ms", 5_000)),
        sql_vm_steps=int(server.get("sql_vm_steps", 2_000_000)),
        max_concurrent_sql=int(server.get("max_concurrent_sql", 4)),
        max_concurrent_agent_runs=int(
            server.get("max_concurrent_agent_runs", 2)
        ),
        llm=excel.get("llm", {}),
        llms=excel.get("llms", []),
        agent=excel.get("agent", {}),
        multi_workbook_enabled=bool(
            excel.get("agent", {}).get("multi_workbook_enabled", True)
        ),
        multi_workbook_max_selected=int(
            excel.get("agent", {}).get("max_selected_workbooks", 5)
        ),
        multi_workbook_max_queried=int(
            excel.get("agent", {}).get("max_queried_workbooks", 3)
        ),
        raw=raw,
    )
