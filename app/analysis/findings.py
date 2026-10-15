"""Rule-based facts and findings for the generic analysis route."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


def _finding_id(index: int) -> str:
    return f"FND-{index:03d}"


def derive_findings(profile: Dict[str, Any], facts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Derive auditable findings; every finding points to one or more facts."""
    fact_list = list(facts)
    fact_by_key = {
        (item.get("evidence", {}).get("table"), item.get("evidence", {}).get("column"), item.get("kind")): item
        for item in fact_list
    }
    findings: List[Dict[str, Any]] = []
    if not profile.get("tables"):
        findings.append(
            {
                "finding_id": _finding_id(len(findings) + 1),
                "severity": "high",
                "title": "没有可分析的数据表",
                "summary": "当前工作簿没有可供通用分析的业务数据表。",
                "evidence": [],
                "confidence": 1.0,
            }
        )
        return findings

    for table in profile.get("tables") or []:
        table_name = table.get("table")
        for column in table.get("columns") or []:
            null_rate = float(column.get("null_rate") or 0.0)
            quality_fact = fact_by_key.get((table_name, column.get("name"), "column_quality"))
            evidence = [quality_fact["fact_id"]] if quality_fact else []
            if null_rate >= 0.5:
                findings.append(
                    {
                        "finding_id": _finding_id(len(findings) + 1),
                        "severity": "high",
                        "title": f"字段“{column.get('label') or column.get('name')}”缺失较多",
                        "summary": f"该字段缺失/占位记录占比为 {null_rate:.1%}，会影响按该字段汇总。",
                        "evidence": evidence,
                        "confidence": 1.0,
                        "scope": {"table": table_name, "column": column.get("name")},
                    }
                )
            elif null_rate >= 0.2:
                findings.append(
                    {
                        "finding_id": _finding_id(len(findings) + 1),
                        "severity": "medium",
                        "title": f"字段“{column.get('label') or column.get('name')}”存在部分缺失",
                        "summary": f"该字段缺失/占位记录占比为 {null_rate:.1%}，建议确认填报规则。",
                        "evidence": evidence,
                        "confidence": 1.0,
                        "scope": {"table": table_name, "column": column.get("name")},
                    }
                )
            distinct_rate = float(column.get("distinct_rate") or 0.0)
            if column.get("semantic_type") == "identifier_candidate" and distinct_rate >= 0.95 and null_rate < 0.05:
                findings.append(
                    {
                        "finding_id": _finding_id(len(findings) + 1),
                        "severity": "info",
                        "title": f"字段“{column.get('label') or column.get('name')}”具备候选标识特征",
                        "summary": "字段非空且唯一率较高，可作为后续关联分析的候选标识；这不是已确认的主键。",
                        "evidence": evidence,
                        "confidence": 0.85,
                        "scope": {"table": table_name, "column": column.get("name")},
                    }
                )
            if column.get("semantic_type") == "numeric_candidate" and float(column.get("numeric_rate_sample") or 0.0) < 0.8:
                findings.append(
                    {
                        "finding_id": _finding_id(len(findings) + 1),
                        "severity": "medium",
                        "title": f"数值候选字段“{column.get('label') or column.get('name')}”含非数值内容",
                        "summary": "样本中存在无法直接转换为数值的内容，合计和排序前需要确认转换规则。",
                        "evidence": evidence,
                        "confidence": 0.75,
                        "scope": {"table": table_name, "column": column.get("name")},
                    }
                )

    domains = profile.get("candidate_domains") or []
    if domains:
        best = domains[0]
        findings.append(
            {
                "finding_id": _finding_id(len(findings) + 1),
                "severity": "info",
                "title": f"工作簿存在“{best.get('name')}”领域候选特征",
                "summary": "该标签由表名、字段名和值样本匹配得到，仅用于选择可选分析模板，不能替代业务确认。",
                "evidence": [],
                "confidence": float(best.get("score") or 0.0),
                "scope": {"candidate_domain": best.get("name"), "matched_keywords": best.get("matched_keywords", [])},
            }
        )
    return findings

