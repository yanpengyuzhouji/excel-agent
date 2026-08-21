"""Whitelisted ReAct tools.  Each returns a JSON-serializable dict that is
delivered to the LLM as the Observation and (partially) surfaced to the
frontend via SSE.

Mirrors DB-GPT agentic_data_api.py tools but executes via the workbook
service's read-only SQL sandbox (no arbitrary code execution).
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from html import escape
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from .html_sanitizer import sanitize_report_html

logger = logging.getLogger(__name__)

_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n]')


def _safe_filename(title: str, max_len: int = 40) -> str:
    clean = _INVALID_FILENAME_CHARS.sub("_", title or "report")
    clean = clean.strip(" _.")
    return clean[:max_len] or "report"


def build_query_report(
    title: str,
    columns: Any,
    rows: Any,
    row_count: int = 0,
    truncated: bool = False,
    summary: str = "",
    highlights: Any = None,
) -> str:
    """Build a safe HTML report from the last successful SQL result.

    This is a deterministic recovery path for small local models that call
    ``html_interpreter`` but omit or corrupt the HTML JSON field. Values are
    escaped as text and never treated as markup.
    """

    column_names = [str(value) for value in (columns or [])][:200]
    table_rows = list(rows or [])[:500]
    header = "".join(f"<th>{escape(column)}</th>" for column in column_names)
    body = []
    for row in table_rows:
        values = list(row or [])
        cells = "".join(
            f"<td>{escape(str(values[index])) if index < len(values) else ''}</td>"
            for index in range(len(column_names))
        )
        body.append(f"<tr>{cells}</tr>")
    clipped = "（结果已按查询上限截断）" if truncated else ""
    summary_html = f'<p class="summary">{escape(str(summary))}</p>' if summary else ""
    highlight_items = highlights if isinstance(highlights, (list, tuple)) else []
    highlights_html = ""
    if highlight_items:
        highlights_html = "<h2>重点</h2><ul>" + "".join(
            f"<li>{escape(str(item))}</li>" for item in list(highlight_items)[:8]
        ) + "</ul>"
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>"
        + escape(str(title or "分析报告"))
        + "</title><style>body{font-family:system-ui,sans-serif;padding:24px;"
        "color:#1f2937}table{border-collapse:collapse;width:100%;font-size:14px}"
        "th,td{border:1px solid #d1d5db;padding:8px;text-align:left}"
        "th{background:#f3f4f6}caption{text-align:left;font-size:20px;"
        "font-weight:600;margin-bottom:12px}summary{margin:8px 0 16px}"
        "h2{font-size:16px;margin-top:20px}</style></head><body>"
        + summary_html + highlights_html + "<table>"
        f"<caption>{escape(str(title or '分析报告'))}（{int(row_count)} 行）{clipped}</caption>"
        f"<thead><tr>{header}</tr></thead><tbody>{''.join(body)}</tbody>"
        "</table></body></html>"
    )


def normalize_report_spec(value: Any) -> Dict[str, Any]:
    """Normalize a model-produced report specification before rendering."""
    source = value if isinstance(value, dict) else {}
    title = str(source.get("title") or "分析报告").strip()[:120] or "分析报告"
    summary = str(source.get("summary") or "").strip()[:1000]
    raw_highlights = source.get("highlights") or []
    if not isinstance(raw_highlights, (list, tuple)):
        raw_highlights = [raw_highlights]
    highlights = [str(item).strip()[:300] for item in list(raw_highlights)[:8] if str(item).strip()]
    return {"title": title, "summary": summary, "highlights": highlights}


def build_query_answer(result: Optional[Dict[str, Any]]) -> str:
    """Create a compact public answer when a local model omits terminate.result."""
    if not result:
        return "分析已完成，但没有可公开的查询结果。"
    columns = [str(value) for value in (result.get("columns") or [])][:20]
    rows = list(result.get("rows") or [])[:20]
    if not columns or not rows:
        return "查询已完成，但没有返回数据。"
    lines = [" | ".join(columns), " | ".join("---" for _ in columns)]
    for row in rows:
        values = list(row or [])
        lines.append(" | ".join(str(values[i]) if i < len(values) else "" for i in range(len(columns))))
    suffix = "（结果已截取部分展示）" if result.get("truncated") else ""
    return "查询结果如下：\n" + "\n".join(lines) + suffix


def build_message_report(title: str, message: str) -> str:
    """Build a safe text-only report when no SQL table is available."""

    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><title>"
        + escape(str(title or "分析报告"))
        + "</title></head><body><h1>"
        + escape(str(title or "分析报告"))
        + "</h1><p>"
        + escape(str(message or "暂无可渲染的报告内容。"))
        + "</p></body></html>"
    )


def sql_query(
    service: Any,
    workbook_id: str,
    sql: str,
    max_rows: int = 500,
) -> Dict[str, Any]:
    """Run read-only SQL; on failure return {error} instead of raising."""
    try:
        result = service.execute_readonly_sql(
            workbook_id, sql, max_rows=max_rows
        )
        return {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "truncated": result.truncated,
        }
    except Exception as exc:
        return {"error": str(exc)}


def html_interpreter(
    service: Any,
    workbook_id: str,
    html: str,
    title: str = "Report",
) -> Dict[str, Any]:
    """Persist the HTML report and return its served URL."""
    storage = Path(service.config.resolved_storage_path())
    report_dir = storage / workbook_id / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_id = f"{uuid.uuid4().hex[:12]}_{_safe_filename(title)}"
    target = report_dir / f"{report_id}.html"
    try:
        content = html or ""
        if "\\n" in content:
            content = content.replace("\\n", "\n")
        if "\\t" in content:
            content = content.replace("\\t", "\t")
        persisted_html = sanitize_report_html(content)
        target.write_text(persisted_html, encoding="utf-8")
    except Exception as exc:
        return {"error": f"Failed to write report: {exc}"}
    return {
        "report_id": report_id,
        "title": title,
        "url": (
            f"/api/v1/workbooks/{quote(workbook_id, safe='')}/reports/"
            f"{quote(report_id, safe='')}"
        ),
        "path": str(target),
        # Keep live SSE rendering byte-for-byte aligned with the stored file.
        # This is still passive HTML; scripts/remote resources were removed by
        # sanitize_report_html before it is exposed to a client.
        "html": persisted_html,
    }


def todowrite(todos: Any) -> Dict[str, Any]:
    """Echo the parsed todo list back (no persistence)."""
    items: list = []
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except Exception:
            todos = None
    if isinstance(todos, dict):
        todos = todos.get("todos", [])
    if isinstance(todos, list):
        for item in todos:
            if isinstance(item, dict):
                items.append(
                    {
                        "content": str(item.get("content", "")),
                        "status": str(item.get("status", "pending")),
                        "priority": str(item.get("priority", "medium")),
                    }
                )
    return {"todos": items}


def report_file_path(service: Any, workbook_id: str, report_id: str) -> Optional[str]:
    storage = Path(service.config.resolved_storage_path())
    candidate = storage / workbook_id / "reports" / f"{report_id}.html"
    if candidate.exists():
        return str(candidate)
    return None
