"""Structured report contract and fixed HTML renderer.

The renderer follows the frontend-design direction for this product: a quiet
"data fieldwork ledger" with graphite ink, amber/teal signals, a restrained
serif display face, and a visible evidence rail.  The visual identity is fixed
in code; user/workbook text is escaped and never becomes markup or CSS.
"""

from __future__ import annotations

from html import escape
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set


REPORT_VERSION = 1
ALLOWED_TEMPLATES = {"executive", "data-quality", "generic-analysis"}
REQUIRED_SECTION_IDS = {"overview", "quality", "distributions", "findings"}


def _fact_ids(facts: Iterable[Mapping[str, Any]]) -> Set[str]:
    return {str(item.get("fact_id")) for item in facts if item.get("fact_id")}


def _finding_ids(findings: Iterable[Mapping[str, Any]]) -> Set[str]:
    return {str(item.get("finding_id")) for item in findings if item.get("finding_id")}


def validate_report_spec(
    report: Mapping[str, Any],
    facts: Sequence[Mapping[str, Any]],
    findings: Sequence[Mapping[str, Any]],
) -> List[str]:
    """Return validation errors instead of silently dropping unsupported data."""
    errors: List[str] = []
    known_facts = _fact_ids(facts)
    known_findings = _finding_ids(findings)
    template = report.get("template")
    if template not in ALLOWED_TEMPLATES:
        errors.append(f"unsupported template: {template!r}")
    if not str(report.get("title") or "").strip():
        errors.append("title is required")
    sections = report.get("sections")
    if not isinstance(sections, list):
        errors.append("sections must be a list")
        sections = []
    section_ids = {str(item.get("id")) for item in sections if isinstance(item, Mapping)}
    missing_sections = REQUIRED_SECTION_IDS - section_ids
    if missing_sections:
        errors.append("missing sections: " + ", ".join(sorted(missing_sections)))
    for index, item in enumerate(report.get("kpis") or []):
        if not isinstance(item, Mapping):
            errors.append(f"kpis[{index}] must be an object")
            continue
        source = item.get("fact_id")
        if source and source not in known_facts:
            errors.append(f"kpis[{index}] references unknown fact {source!r}")
    for index, section in enumerate(sections):
        if not isinstance(section, Mapping):
            errors.append(f"sections[{index}] must be an object")
            continue
        for source in section.get("fact_ids") or []:
            if source not in known_facts:
                errors.append(f"sections[{index}] references unknown fact {source!r}")
    for index, item in enumerate(report.get("findings") or []):
        if not isinstance(item, Mapping):
            errors.append(f"findings[{index}] must be an object")
            continue
        source = item.get("finding_id")
        if source and source not in known_findings:
            errors.append(f"findings[{index}] references unknown finding {source!r}")
        for fact_id in item.get("evidence") or []:
            if fact_id not in known_facts:
                errors.append(f"findings[{index}] references unknown fact {fact_id!r}")
    return errors


def build_report_spec(
    profile: Mapping[str, Any],
    facts: Sequence[Mapping[str, Any]],
    findings: Sequence[Mapping[str, Any]],
    *,
    title: str = "工作簿通用数据分析报告",
) -> Dict[str, Any]:
    """Create a complete report spec using only computed facts/findings."""
    fact_by_id = {str(item.get("fact_id")): item for item in facts}
    overview_facts = [
        fact_by_id.get("FCT-OVERVIEW-ROWS"),
        fact_by_id.get("FCT-OVERVIEW-TABLES"),
        fact_by_id.get("FCT-OVERVIEW-COLUMNS"),
    ]
    overview_facts = [item for item in overview_facts if item]
    quality_facts = [item for item in facts if item.get("kind") == "column_quality"]
    distribution_facts = [item for item in facts if item.get("kind") == "distribution"]
    overview_values = {str(item.get("fact_id")): item.get("value") for item in overview_facts}
    summary = (
        f"本报告对 {int(overview_values.get('FCT-OVERVIEW-TABLES') or 0)} 张数据表、"
        f"{int(overview_values.get('FCT-OVERVIEW-ROWS') or 0)} 条记录和 "
        f"{int(overview_values.get('FCT-OVERVIEW-COLUMNS') or 0)} 个字段进行了通用画像。"
        "结论只引用已验证的字段质量与分布事实。"
    )
    quality_rows = []
    for fact in quality_facts:
        value = fact.get("value") or {}
        evidence = fact.get("evidence") or {}
        quality_rows.append(
            {
                "table": evidence.get("table", ""),
                "column": evidence.get("column", ""),
                "label": fact.get("label", ""),
                "null_rate": value.get("null_rate", 0),
                "distinct_rate": value.get("distinct_rate", 0),
                "numeric_rate": value.get("numeric_rate_sample", 0),
                "fact_id": fact.get("fact_id"),
            }
        )
    distribution_rows = []
    for fact in distribution_facts:
        evidence = fact.get("evidence") or {}
        distribution_rows.append(
            {
                "label": fact.get("label", ""),
                "table": evidence.get("table", ""),
                "column": evidence.get("column", ""),
                "values": fact.get("value") or [],
                "fact_id": fact.get("fact_id"),
            }
        )
    spec = {
        "report_version": REPORT_VERSION,
        "template": "generic-analysis",
        "title": str(title or "工作簿通用数据分析报告")[:120],
        "eyebrow": "DATA FIELDWORK / GENERIC PROFILE",
        "summary": summary,
        "kpis": [
            {"label": item["label"], "value": item["value"], "fact_id": item["fact_id"]}
            for item in overview_facts
        ],
        "sections": [
            {
                "id": "overview",
                "title": "这份表里有什么",
                "description": "先确认规模，再决定需要深入核查的字段。",
                "items": [
                    {"label": item["label"], "value": item["value"], "fact_id": item["fact_id"]}
                    for item in overview_facts
                ],
                "fact_ids": [item["fact_id"] for item in overview_facts],
            },
            {
                "id": "quality",
                "title": "字段质量",
                "description": "缺失率包含空值、空白和约定占位符；占位符不会被当成零。",
                "rows": quality_rows,
                "fact_ids": [item["fact_id"] for item in quality_facts],
            },
            {
                "id": "distributions",
                "title": "高频值与分布线索",
                "description": "这里只展示模板查询得到的前五个非缺失值，不代表业务排名的完整全集。",
                "rows": distribution_rows,
                "fact_ids": [item["fact_id"] for item in distribution_facts],
            },
            {
                "id": "findings",
                "title": "需要关注的地方",
                "description": "规则发现是可复核的线索，不会替代业务人员确认。",
                "fact_ids": [fact_id for item in findings for fact_id in item.get("evidence") or []],
            },
        ],
        "findings": [
            {
                "finding_id": item.get("finding_id"),
                "severity": item.get("severity", "info"),
                "title": item.get("title", ""),
                "summary": item.get("summary", ""),
                "evidence": list(item.get("evidence") or []),
            }
            for item in findings
        ],
        "recommendations": [
            {
                "priority": "high" if item.get("severity") == "high" else "medium",
                "text": (
                    f"核对“{item.get('title', '')}”对应的填报或业务规则，"
                    "确认后再用于汇总、筛选或采购/设备等领域计算。"
                ),
                "finding_id": item.get("finding_id"),
            }
            for item in findings
            if item.get("severity") in {"high", "medium"}
        ],
    }
    errors = validate_report_spec(spec, facts, findings)
    if errors:
        raise ValueError("Invalid generated report spec: " + "; ".join(errors))
    return spec


def _esc(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def _ratio(value: Any) -> str:
    try:
        return f"{float(value):.1%}"
    except (TypeError, ValueError):
        return "—"


def _severity_class(value: Any) -> str:
    return value if value in {"high", "medium", "low", "info"} else "info"


def render_report_html(report: Mapping[str, Any]) -> str:
    """Render a validated report spec into self-contained safe HTML."""
    sections = report.get("sections") or []
    kpis = report.get("kpis") or []
    findings = report.get("findings") or []
    recommendations = report.get("recommendations") or []
    kpi_html = "".join(
        "<div class=\"kpi\"><div class=\"kpi-value\">"
        + _esc(item.get("value"))
        + "</div><div class=\"kpi-label\">"
        + _esc(item.get("label"))
        + "</div><div class=\"evidence\">"
        + _esc(item.get("fact_id"))
        + "</div></div>"
        for item in kpis
    )
    rendered_sections: List[str] = []
    for section in sections:
        section_id = str(section.get("id") or "")
        body = ""
        if section_id == "overview":
            body = "<div class=\"overview-list\">" + "".join(
                "<div class=\"overview-item\"><span>"
                + _esc(item.get("label"))
                + "</span><strong>"
                + _esc(item.get("value"))
                + "</strong><small>"
                + _esc(item.get("fact_id"))
                + "</small></div>"
                for item in section.get("items") or []
            ) + "</div>"
        elif section_id == "quality":
            rows = "".join(
                "<tr><td>"
                + _esc(item.get("label"))
                + "</td><td>"
                + _ratio(item.get("null_rate"))
                + "</td><td>"
                + _ratio(item.get("distinct_rate"))
                + "</td><td>"
                + _ratio(item.get("numeric_rate"))
                + "</td><td class=\"evidence\">"
                + _esc(item.get("fact_id"))
                + "</td></tr>"
                for item in section.get("rows") or []
            )
            body = (
                "<div class=\"table-wrap\"><table><thead><tr>"
                "<th>字段</th><th>缺失率</th><th>唯一率</th><th>样本数值率</th><th>证据</th>"
                "</tr></thead><tbody>" + rows + "</tbody></table></div>"
            )
        elif section_id == "distributions":
            cards = []
            for item in section.get("rows") or []:
                values = item.get("values") or []
                maximum = max([int(value.get("count") or 0) for value in values] or [1])
                bars = "".join(
                    "<div class=\"bar-row\"><span>"
                    + _esc(value.get("value"))
                    + "</span><i class=\"bar-fill w"
                    + str(min(100, max(0, int(round((int(value.get("count") or 0) / maximum) * 100 / 10) * 10))))
                    + "\"></i><b>"
                    + _esc(value.get("count"))
                    + "</b></div>"
                    for value in values
                )
                cards.append(
                    "<div class=\"distribution-card\"><h3>"
                    + _esc(item.get("label"))
                    + "</h3><div class=\"evidence\">"
                    + _esc(item.get("fact_id"))
                    + "</div>"
                    + (bars or "<p class=\"muted\">没有可展示的非缺失值</p>")
                    + "</div>"
                )
            body = "<div class=\"distribution-grid\">" + "".join(cards) + "</div>"
        elif section_id == "findings":
            body = "<div class=\"finding-list\">" + "".join(
                "<article class=\"finding finding-"
                + _severity_class(item.get("severity"))
                + "\"><div class=\"finding-rail\"></div><div><span class=\"badge\">"
                + _esc(item.get("severity"))
                + "</span><h3>"
                + _esc(item.get("title"))
                + "</h3><p>"
                + _esc(item.get("summary"))
                + "</p><div class=\"evidence\">"
                + _esc(", ".join(str(value) for value in item.get("evidence") or []) or "规则画像")
                + "</div></div></article>"
                for item in findings
            ) + "</div>"
        else:
            body = "<p class=\"muted\">该章节暂无内容。</p>"
        rendered_sections.append(
            "<section id=\""
            + _esc(section_id)
            + "\"><div class=\"section-head\"><span class=\"section-mark\"></span><div><h2>"
            + _esc(section.get("title"))
            + "</h2><p>"
            + _esc(section.get("description"))
            + "</p></div></div>"
            + body
            + "</section>"
        )
    recommendation_html = "".join(
        "<li><span class=\"priority priority-"
        + _esc(item.get("priority"))
        + "\">"
        + _esc(item.get("priority"))
        + "</span><span>"
        + _esc(item.get("text"))
        + "</span><small class=\"evidence\">"
        + _esc(item.get("finding_id"))
        + "</small></li>"
        for item in recommendations
    )
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>""" + _esc(report.get("title")) + """</title>
<style>
.bar-fill{display:block;height:8px;background:linear-gradient(90deg,#238b82,#6ab9a5);min-width:2px}.w0{width:2%}.w10{width:10%}.w20{width:20%}.w30{width:30%}.w40{width:40%}.w50{width:50%}.w60{width:60%}.w70{width:70%}.w80{width:80%}.w90{width:90%}.w100{width:100%}
:root{--ink:#17232b;--muted:#6f7d82;--paper:#f3f1eb;--panel:#fffdf8;--line:#d8d6cc;--amber:#d98c35;--teal:#238b82;--red:#b95549;--blue:#486c88;--shadow:0 16px 40px rgba(23,35,43,.08)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font-family:"Noto Sans SC","PingFang SC",system-ui,sans-serif;line-height:1.6}.shell{max-width:1180px;margin:0 auto;padding:32px 22px 72px}.hero{display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:28px;align-items:end;padding:26px 0 32px;border-bottom:1px solid var(--ink)}.eyebrow,.evidence,.badge,.priority{font-family:"IBM Plex Mono","SFMono-Regular",Consolas,monospace;letter-spacing:.08em;text-transform:uppercase;font-size:11px}.eyebrow{color:var(--teal);font-weight:700}.hero h1{font-family:Georgia,"Noto Serif SC",serif;font-size:clamp(34px,6vw,72px);font-weight:500;line-height:1.02;letter-spacing:-.05em;margin:16px 0 20px;max-width:820px}.hero-summary{color:var(--muted);max-width:680px;margin:0}.hero-stamp{border-left:4px solid var(--amber);padding:14px 0 14px 16px}.hero-stamp strong{display:block;font-family:"IBM Plex Mono",monospace;font-size:28px;line-height:1.1}.hero-stamp span{color:var(--muted);font-size:13px}.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:24px 0 42px}.kpi{background:var(--ink);color:#f8f5ed;padding:20px 18px;min-height:132px;box-shadow:var(--shadow)}.kpi:nth-child(2){background:var(--teal)}.kpi:nth-child(3){background:var(--amber);color:var(--ink)}.kpi-value{font-family:"IBM Plex Mono",monospace;font-size:32px;font-weight:700;letter-spacing:-.06em}.kpi-label{font-size:13px;margin-top:8px}.evidence{color:#8b9897;margin-top:10px;letter-spacing:.03em;text-transform:none}.kpi .evidence{color:rgba(255,255,255,.62)}.kpi:nth-child(3) .evidence{color:rgba(23,35,43,.68)}section{padding:30px 0;border-top:1px solid var(--line)}.section-head{display:flex;gap:14px;align-items:flex-start;margin-bottom:20px}.section-mark{display:block;width:9px;height:42px;background:var(--teal);flex:0 0 auto}.section-head h2{font-family:Georgia,"Noto Serif SC",serif;font-size:28px;font-weight:500;line-height:1.1;margin:0}.section-head p{color:var(--muted);margin:6px 0 0;font-size:14px}.overview-list{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.overview-item{background:var(--panel);border:1px solid var(--line);padding:16px}.overview-item span,.overview-item small{display:block;color:var(--muted);font-size:13px}.overview-item strong{display:block;font-family:"IBM Plex Mono",monospace;font-size:26px;margin:8px 0}.overview-item small{font-family:"IBM Plex Mono",monospace;font-size:10px}.table-wrap{overflow-x:auto;background:var(--panel);border:1px solid var(--line)}table{width:100%;border-collapse:collapse;min-width:650px}th,td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--line);font-size:13px}th{font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--muted);font-weight:500;text-transform:uppercase}tr:last-child td{border-bottom:0}.distribution-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.distribution-card{background:var(--panel);border:1px solid var(--line);padding:18px}.distribution-card h3{font-size:15px;margin:0 0 2px}.bar-row{display:grid;grid-template-columns:minmax(80px,1fr) 2fr 34px;gap:10px;align-items:center;font-size:12px;margin-top:12px}.bar-row span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.bar-row i{display:block;height:8px;background:linear-gradient(90deg,var(--teal),#6ab9a5);width:var(--bar);min-width:2px}.bar-row b{font-family:"IBM Plex Mono",monospace;text-align:right}.finding-list{display:grid;gap:10px}.finding{display:grid;grid-template-columns:8px 1fr;background:var(--panel);border:1px solid var(--line)}.finding-rail{background:var(--blue)}.finding-high .finding-rail{background:var(--red)}.finding-medium .finding-rail{background:var(--amber)}.finding h3{margin:12px 16px 2px;font-size:16px}.finding p{margin:0 16px;color:var(--muted);font-size:14px}.finding .evidence{margin:8px 16px 14px}.badge{display:inline-block;margin:14px 16px 0;color:var(--blue);font-weight:700}.finding-high .badge{color:var(--red)}.finding-medium .badge{color:#a56721}.recommendations{background:var(--ink);color:#f8f5ed;padding:20px 24px;margin-top:20px}.recommendations h2{font-family:Georgia,"Noto Serif SC",serif;font-size:24px;font-weight:500;margin:0 0 10px}.recommendations ul{padding:0;margin:0;list-style:none}.recommendations li{display:grid;grid-template-columns:72px 1fr auto;gap:12px;padding:10px 0;border-top:1px solid rgba(255,255,255,.18);align-items:center;font-size:14px}.recommendations li:first-child{border-top:0}.priority{color:#f0b15c}.recommendations .evidence{color:#91a6a5;margin:0}.muted{color:var(--muted)}:focus-visible{outline:3px solid var(--amber);outline-offset:3px}@media(max-width:760px){.shell{padding:20px 16px 48px}.hero{grid-template-columns:1fr;gap:18px}.hero h1{font-size:44px}.kpis,.overview-list,.distribution-grid{grid-template-columns:1fr}.recommendations li{grid-template-columns:60px 1fr}.recommendations li .evidence{grid-column:2}.bar-row{grid-template-columns:90px 1fr 30px}}@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
</style></head><body><main class="shell">
<header class="hero"><div><div class="eyebrow">""" + _esc(report.get("eyebrow")) + """</div><h1>""" + _esc(report.get("title")) + """</h1><p class="hero-summary">""" + _esc(report.get("summary")) + """</p></div>
<div class="hero-stamp"><strong>evidence<br>first</strong><span>每条结论都保留可复核来源</span></div></header>
<div class="kpis">""" + kpi_html + """</div>
""" + "".join(rendered_sections) + """
<aside class="recommendations"><h2>建议的下一步</h2><ul>""" + (recommendation_html or "<li><span class=\"muted\">—</span><span>当前没有高/中优先级规则建议。</span></li>") + """</ul></aside>
</main></body></html>"""
