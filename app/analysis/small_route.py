"""Deterministic route used by smaller models.

The first version intentionally does not ask the selected small model to write
SQL or HTML.  It gives the model-sized route a reliable baseline: the same
profile/facts can later be handed to a small model for wording, while the
report remains valid when that model is unavailable or emits malformed JSON.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Generator, Optional

from ..agent import tools
from .pipeline import build_analysis_bundle
from .report import render_report_html


_REPORT_WORDS = (
    "报告", "报表", "分析", "统计", "汇总", "分布", "质量", "完整性", "趋势", "图表",
    "report", "analysis", "summary",
)


def _is_report_request(question: str) -> bool:
    value = str(question or "").casefold()
    return any(word.casefold() in value for word in _REPORT_WORDS)


def _overview_answer(bundle: Dict[str, Any], question: str) -> str:
    facts = {str(item.get("fact_id")): item for item in bundle.get("facts") or []}
    rows = facts.get("FCT-OVERVIEW-ROWS", {}).get("value", 0)
    tables = facts.get("FCT-OVERVIEW-TABLES", {}).get("value", 0)
    columns = facts.get("FCT-OVERVIEW-COLUMNS", {}).get("value", 0)
    if any(token in question for token in ("多少行", "几行", "记录数", "记录有多少")):
        return f"当前工作簿共有 {rows} 条记录（{tables} 张数据表、{columns} 个字段）。"
    return f"通用画像已完成：{tables} 张数据表、{rows} 条记录、{columns} 个字段。"


def run_small_route(
    service: Any,
    workbook_id: str,
    question: str,
    *,
    title: Optional[str] = None,
    on_tool_step_full: Optional[Any] = None,
) -> Generator[Dict[str, Any], None, None]:
    """Yield SSE-shaped events for the small-model template route."""
    yield {"type": "status", "status": "profiling", "done": False}
    bundle = build_analysis_bundle(service, workbook_id)
    profile = bundle["profile"]
    yield {
        "type": "profile",
        "profile_version": profile.get("profile_version"),
        "table_count": profile.get("table_count", 0),
        "row_count": profile.get("row_count", 0),
        "column_count": profile.get("column_count", 0),
        "candidate_domains": profile.get("candidate_domains", [])[:5],
        "warnings": profile.get("warnings", [])[:10],
    }
    yield {
        "type": "plan.update",
        "tasks": [
            {"content": "生成通用数据画像", "status": "completed", "priority": "high"},
            {"content": "执行标准质量与分布模板", "status": "completed", "priority": "high"},
            {"content": "生成可复核规则洞察", "status": "completed", "priority": "medium"},
            {"content": "渲染固定主题报告", "status": "in_progress" if _is_report_request(question) else "cancelled", "priority": "medium"},
        ],
    }
    for finding in bundle["findings"][:20]:
        yield {
            "type": "finding",
            "finding_id": finding.get("finding_id"),
            "severity": finding.get("severity"),
            "title": finding.get("title"),
            "evidence": finding.get("evidence", []),
        }
    if not _is_report_request(question):
        yield {
            "type": "done",
            "done": True,
            "full_answer": _overview_answer(bundle, question),
            "report_url": None,
            "report_title": None,
        }
        return

    html = render_report_html(bundle["report"])
    report_title = title or bundle["report"].get("title") or "工作簿通用数据分析报告"
    step_id = "step-1"
    yield {"type": "step.start", "step": 1, "id": step_id,
           "title": "html_interpreter", "detail": "Action/Observation"}
    yield {"type": "step.meta", "id": step_id,
           "action": "html_interpreter",
           "action_input": {"title": report_title}}
    report = tools.html_interpreter(service, workbook_id, html, report_title)
    if "error" in report:
        yield {"type": "step.chunk", "id": step_id,
               "output_type": "error", "content": report["error"]}
        yield {"type": "step.done", "id": step_id, "status": "failed"}
        yield {"type": "error", "error": report["error"], "done": True}
        return
    stored_html = report.get("html", html)
    yield {"type": "step.chunk", "id": step_id,
           "output_type": "code", "content": stored_html}
    yield {"type": "report_spec", "report": bundle["report"]}
    yield {
        "type": "html",
        "title": report["title"],
        "report_id": report["report_id"],
        "url": report["url"],
        "html": stored_html,
    }
    yield {"type": "step.chunk", "id": step_id,
           "output_type": "html",
           "content": {
               "title": report["title"],
               "report_id": report["report_id"],
               "url": report["url"],
               "html": stored_html,
           }}
    yield {"type": "step.done", "id": step_id, "status": "done"}
    if on_tool_step_full is not None:
        on_tool_step_full(
            f"html_interpreter: {report_title}",
            json.dumps(
                {"ok": True, "report_id": report["report_id"]},
                ensure_ascii=False,
            ),
            {
                "action": "html_interpreter",
                "title": report_title,
                "html": {
                    "url": report["url"],
                    "title": report["title"],
                    "report_id": report["report_id"],
                    "html": stored_html,
                },
            },
        )
    yield {
        "type": "done",
        "done": True,
        "full_answer": _overview_answer(bundle, question) + " 已生成固定主题报告。",
        "report_url": report["url"],
        "report_title": report["title"],
    }
