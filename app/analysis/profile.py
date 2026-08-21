"""Generic workbook profiling and semantic hints.

This module does not try to identify a workbook's business meaning with an
LLM.  It computes exact low-level quality metrics in SQLite and adds explicit
candidate labels based on a small, auditable vocabulary.  Candidate labels are
signals only; downstream rules must never treat them as proven business facts.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..service.constants import PLACEHOLDER_VALUES


PROFILE_VERSION = 1
DEFAULT_SAMPLE_SIZE = 200
MAX_EXAMPLES = 5

_MISSING_TOKENS = {
    "",
    "null",
    "none",
    "n/a",
    "na",
    "nan",
    *{str(value).strip().lower() for value in PLACEHOLDER_VALUES},
}

_SEMANTIC_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "identifier": (
        "编码",
        "编号",
        "id",
        "code",
        "no",
        "number",
        "identifier",
        "标识",
    ),
    "numeric_candidate": (
        "数量",
        "金额",
        "价格",
        "成本",
        "预算",
        "总额",
        "qty",
        "quantity",
        "amount",
        "price",
        "cost",
        "count",
        "数值",
    ),
    "date_candidate": (
        "日期",
        "时间",
        "月份",
        "年月",
        "date",
        "time",
        "month",
        "year",
    ),
    "category_candidate": (
        "类别",
        "分类",
        "类型",
        "单位",
        "状态",
        "等级",
        "来源",
        "category",
        "type",
        "unit",
        "status",
        "level",
    ),
    "description_candidate": (
        "描述",
        "名称",
        "说明",
        "备注",
        "内容",
        "description",
        "name",
        "remark",
        "note",
        "text",
    ),
}

_DOMAIN_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "material": (
        "物料",
        "材料",
        "零件",
        "配件",
        "bom",
        "material",
        "part",
        "component",
    ),
    "procurement": (
        "采购",
        "招标",
        "批次",
        "供应商",
        "合同",
        "采购",
        "purchase",
        "procurement",
        "supplier",
        "tender",
    ),
    "equipment": (
        "设备",
        "装置",
        "仪器",
        "机组",
        "型号",
        "equipment",
        "device",
        "model",
    ),
    "project": (
        "项目",
        "工程",
        "计划",
        "任务",
        "project",
        "plan",
        "task",
    ),
    "organization": (
        "部门",
        "单位",
        "组织",
        "公司",
        "人员",
        "姓名",
        "department",
        "organization",
        "company",
        "person",
        "owner",
    ),
    "finance": (
        "金额",
        "价格",
        "成本",
        "预算",
        "收入",
        "支出",
        "金额",
        "finance",
        "price",
        "cost",
        "budget",
        "revenue",
    ),
}


def _quote_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value or ""):
        raise ValueError(f"Unsafe SQLite identifier in profile: {value!r}")
    return f'"{value}"'


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _normalized(value: Any) -> str:
    return _text(value).casefold()


def _is_missing(value: Any) -> bool:
    return _normalized(value) in _MISSING_TOKENS


def _is_number(value: Any) -> bool:
    if value is None or _is_missing(value):
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    candidate = _text(value).replace(",", "")
    return bool(re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", candidate))


def _semantic_hints(label: str, physical_name: str, sql_type: str, values: Sequence[Any]) -> Dict[str, Any]:
    haystack = f"{label} {physical_name}".casefold()
    scores = {
        role: sum(1 for keyword in keywords if keyword.casefold() in haystack)
        for role, keywords in _SEMANTIC_KEYWORDS.items()
    }
    role = max(scores, key=scores.get) if scores else "text"
    score = int(scores.get(role, 0))
    non_missing = [value for value in values if not _is_missing(value)]
    numeric_rate = (
        sum(1 for value in non_missing if _is_number(value)) / len(non_missing)
        if non_missing
        else 0.0
    )
    if numeric_rate >= 0.9 and role not in {"identifier", "date_candidate"}:
        semantic_type = "numeric_candidate"
    elif role == "date_candidate":
        semantic_type = "date_candidate"
    elif role == "identifier":
        semantic_type = "identifier_candidate"
    elif role == "category_candidate":
        semantic_type = "category_candidate"
    elif role == "description_candidate":
        semantic_type = "description_candidate"
    elif str(sql_type).upper() in {"INTEGER", "REAL", "NUMERIC", "FLOAT", "DOUBLE"}:
        semantic_type = "numeric_candidate"
    else:
        semantic_type = "text"
    return {
        "semantic_type": semantic_type,
        "semantic_keyword_score": score,
        "numeric_rate_sample": round(numeric_rate, 4),
    }


def _manifest(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    row = connection.execute(
        "SELECT schema_json FROM _dbgpt_import_manifest LIMIT 1"
    ).fetchone()
    if not row or not row[0]:
        tables = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE '_dbgpt%' "
            "AND name != 'project_metadata' ORDER BY name"
        ).fetchall()
        return [{"table_name": item[0], "columns": []} for item in tables]
    try:
        decoded = json.loads(row[0])
    except (TypeError, ValueError):
        decoded = []
    if isinstance(decoded, list) and decoded:
        return decoded
    # A legacy/partially-built database can contain the manifest table before
    # its schema row is populated.  It is still a valid profile input: inspect
    # business tables rather than silently returning an empty workbook.
    tables = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE '_dbgpt%' "
        "AND name != 'project_metadata' ORDER BY name"
    ).fetchall()
    return [{"table_name": item[0], "columns": []} for item in tables]


def _column_specs(connection: sqlite3.Connection, table: str, manifest_columns: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return enabled logical columns, filling gaps from SQLite PRAGMA."""
    if manifest_columns:
        specs = [item for item in manifest_columns if item.get("enabled", True)]
        if specs:
            return specs
    rows = connection.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    return [
        {
            "physical_name": row[1],
            "display_name": row[1],
            "sql_type": row[2] or "TEXT",
            "enabled": True,
        }
        for row in rows
        if not str(row[1]).startswith("_")
    ]


def _source_expression(column: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    name = str(column.get("physical_name") or "")
    if column.get("mixed_numeric"):
        raw_name = f"{name}_raw"
        return _quote_identifier(raw_name), raw_name
    return _quote_identifier(name), name


def _profile_column(
    connection: sqlite3.Connection,
    table: str,
    column: Dict[str, Any],
    sample_size: int,
) -> Dict[str, Any]:
    name = str(column.get("physical_name") or "")
    label = str(column.get("display_name") or column.get("source_name") or name)
    expression, source_name = _source_expression(column)
    table_sql = _quote_identifier(table)
    missing_sql = (
        f"{expression} IS NULL OR TRIM(CAST({expression} AS TEXT)) = '' "
        "OR LOWER(TRIM(CAST({0} AS TEXT))) IN ({1})"
    ).format(expression, ",".join("?" for _ in _MISSING_TOKENS))
    missing_params = sorted(_MISSING_TOKENS)
    total = int(connection.execute(f"SELECT COUNT(*) FROM {table_sql}").fetchone()[0])
    missing = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table_sql} WHERE {missing_sql}", missing_params
        ).fetchone()[0]
    )
    sql_null_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table_sql} WHERE {expression} IS NULL"
        ).fetchone()[0]
    )
    blank_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table_sql} WHERE {expression} IS NOT NULL "
            "AND TRIM(CAST({0} AS TEXT)) = ''".format(expression)
        ).fetchone()[0]
    )
    placeholder_tokens = sorted(_MISSING_TOKENS - {""})
    placeholder_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table_sql} WHERE {expression} IS NOT NULL "
            "AND LOWER(TRIM(CAST({0} AS TEXT))) IN ({1})".format(
                expression, ",".join("?" for _ in placeholder_tokens)
            ),
            placeholder_tokens,
        ).fetchone()[0]
    )
    non_missing = max(total - missing, 0)
    distinct = int(
        connection.execute(
            f"SELECT COUNT(DISTINCT CASE WHEN NOT ({missing_sql}) "
            f"THEN TRIM(CAST({expression} AS TEXT)) END) FROM {table_sql}",
            missing_params,
        ).fetchone()[0]
    )
    sample_rows = connection.execute(
        f"SELECT {expression} FROM {table_sql} LIMIT ?", (max(1, int(sample_size)),)
    ).fetchall()
    values = [row[0] for row in sample_rows]
    examples: List[str] = []
    for value in values:
        if _is_missing(value):
            continue
        rendered = _text(value)
        if rendered and rendered not in examples:
            examples.append(rendered[:120])
        if len(examples) >= MAX_EXAMPLES:
            break
    top_value_rows = connection.execute(
        f"SELECT TRIM(CAST({expression} AS TEXT)) AS value, COUNT(*) AS value_count "
        f"FROM {table_sql} WHERE NOT ({missing_sql}) "
        f"GROUP BY TRIM(CAST({expression} AS TEXT)) "
        "ORDER BY value_count DESC, value ASC LIMIT 5",
        missing_params,
    ).fetchall()
    hints = _semantic_hints(label, name, str(column.get("sql_type") or "TEXT"), values)
    # If the source was materialized into a numeric sidecar, its availability is
    # stronger evidence than a text sample, while the raw column remains the
    # evidence for missing/placeholder quality.
    if column.get("mixed_numeric"):
        value_name = f"{name}_value"
        value_sql = _quote_identifier(value_name)
        numeric_count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {table_sql} WHERE {value_sql} IS NOT NULL"
            ).fetchone()[0]
        )
    else:
        numeric_count = sum(1 for value in values if _is_number(value))
    return {
        "name": name,
        "label": label,
        "sql_type": str(column.get("sql_type") or "TEXT").upper(),
        "source_name": source_name,
        "mixed_numeric": bool(column.get("mixed_numeric")),
        "semantic_type": hints["semantic_type"],
        "semantic_keyword_score": hints["semantic_keyword_score"],
        "row_count": total,
        "null_count": missing,
        "sql_null_count": sql_null_count,
        "blank_count": blank_count,
        "placeholder_count": placeholder_count,
        "non_null_count": non_missing,
        "null_rate": round(missing / total, 6) if total else 0.0,
        "distinct_count": distinct,
        "distinct_rate": round(distinct / non_missing, 6) if non_missing else 0.0,
        "numeric_count": numeric_count,
        "numeric_rate_sample": hints["numeric_rate_sample"],
        "sample_size": len(values),
        "examples": examples,
        "top_values": [
            {"value": str(value), "count": int(value_count)}
            for value, value_count in top_value_rows
        ],
        "missing_tokens": sorted(_MISSING_TOKENS),
    }


def _domain_candidates(table_names: Iterable[str], columns: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    text = " ".join(table_names)
    for column in columns:
        text += " " + str(column.get("label") or "") + " " + str(column.get("name") or "")
        text += " " + " ".join(str(item) for item in column.get("examples") or [])
    folded = text.casefold()
    scores: List[Dict[str, Any]] = []
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        hits = [keyword for keyword in keywords if keyword.casefold() in folded]
        if hits:
            # The score is intentionally bounded and interpretable: it is a
            # candidate ranking, not a probability or a business assertion.
            score = min(1.0, 0.2 * len(set(hits)))
            scores.append({"name": domain, "score": round(score, 4), "matched_keywords": sorted(set(hits))})
    return sorted(scores, key=lambda item: (-item["score"], item["name"]))


def build_workbook_profile(
    service: Any,
    workbook_id: str,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> Dict[str, Any]:
    """Build a bounded, deterministic profile for an active workbook."""
    database_path = str(service.active_database_path(workbook_id))
    uri = f"file:{Path(database_path).resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    config = getattr(service, "config", None)
    timeout_ms = max(1, int(getattr(config, "sql_timeout_ms", 5_000)))
    max_vm_steps = max(1, int(getattr(config, "sql_vm_steps", 2_000_000)))
    started = time.perf_counter()
    vm_steps = 0

    def progress_handler() -> int:
        nonlocal vm_steps
        vm_steps += 1_000
        if vm_steps >= max_vm_steps:
            return 1
        if (time.perf_counter() - started) * 1000 >= timeout_ms:
            return 1
        return 0

    connection.set_progress_handler(progress_handler, 1_000)
    try:
        table_profiles: List[Dict[str, Any]] = []
        all_columns: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []
        manifest = _manifest(connection)
        for table in manifest:
            table_name = str(table.get("table_name") or "")
            if not table_name:
                continue
            try:
                columns = _column_specs(connection, table_name, table.get("columns") or [])
                row_count = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}"
                    ).fetchone()[0]
                )
                profile_columns = [
                    _profile_column(connection, table_name, column, sample_size)
                    for column in columns
                ]
                for item in profile_columns:
                    item["table"] = table_name
                all_columns.extend(profile_columns)
                table_profiles.append(
                    {
                        "table": table_name,
                        "display_name": table.get("sheet") or table_name,
                        "row_count": row_count,
                        "column_count": len(profile_columns),
                        "columns": profile_columns,
                    }
                )
            except Exception as exc:
                warnings.append({"table": table_name, "code": "PROFILE_FAILED", "message": str(exc)[:300]})
        domains = _domain_candidates((item["table"] for item in table_profiles), all_columns)
        return {
            "profile_version": PROFILE_VERSION,
            "workbook_id": workbook_id,
            "database_path": database_path,
            "table_count": len(table_profiles),
            "row_count": sum(item["row_count"] for item in table_profiles),
            "column_count": sum(item["column_count"] for item in table_profiles),
            "tables": table_profiles,
            "candidate_domains": domains,
            "warnings": warnings,
        }
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()
