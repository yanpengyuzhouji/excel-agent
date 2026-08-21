"""Standard, deterministic metrics for every workbook shape.

The template layer receives a profile whose table and column names came from
the reviewed import manifest.  It only creates identifiers from that
allow-list and delegates execution to the service's read-only SQL sandbox.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence


MAX_TOP_VALUES = 5
MAX_DISTRIBUTION_COLUMNS = 120


def _fact_id(*parts: Any) -> str:
    value = "-".join(str(part) for part in parts)
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return ("FCT-" + value.upper())[:180]


def _quote_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value or ""):
        raise ValueError(f"Unsafe SQL identifier in standard template: {value!r}")
    return f'"{value}"'


def _sql_literal(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _missing_predicate(expression: str, tokens: Sequence[str]) -> str:
    return (
        f"{expression} IS NULL OR TRIM(CAST({expression} AS TEXT)) = '' "
        "OR LOWER(TRIM(CAST({0} AS TEXT))) IN ({1})".format(
            expression, ",".join(_sql_literal(token) for token in tokens)
        )
    )


def _value_expression(column: Dict[str, Any]) -> str:
    name = str(column.get("name") or "")
    # Mixed numeric fields keep the source text in *_raw so '/' and other
    # placeholders are not confused with zero.  A standard distribution must
    # group the source representation, not the parsed sidecar.
    if column.get("mixed_numeric"):
        name = f"{name}_raw"
    return _quote_identifier(name)


def run_standard_templates(
    service: Any,
    workbook_id: str,
    profile: Dict[str, Any],
    *,
    max_rows: int = 50,
) -> List[Dict[str, Any]]:
    """Execute the common overview/quality/distribution templates."""
    facts: List[Dict[str, Any]] = []
    facts.append(
        {
            "fact_id": _fact_id("overview", "rows"),
            "kind": "metric",
            "label": "总记录数",
            "value": int(profile.get("row_count") or 0),
            "evidence": {"profile": "row_count"},
        }
    )
    facts.append(
        {
            "fact_id": _fact_id("overview", "tables"),
            "kind": "metric",
            "label": "数据表数量",
            "value": int(profile.get("table_count") or 0),
            "evidence": {"profile": "table_count"},
        }
    )
    facts.append(
        {
            "fact_id": _fact_id("overview", "columns"),
            "kind": "metric",
            "label": "字段数量",
            "value": int(profile.get("column_count") or 0),
            "evidence": {"profile": "column_count"},
        }
    )
    distribution_columns_seen = 0
    for table in profile.get("tables") or []:
        table_name = str(table.get("table") or "")
        facts.append(
            {
                "fact_id": _fact_id("table", table_name, "rows"),
                "kind": "table_metric",
                "label": f"{table.get('display_name') or table_name}记录数",
                "value": int(table.get("row_count") or 0),
                "evidence": {"table": table_name, "metric": "COUNT(*)"},
            }
        )
        for column in table.get("columns") or []:
            name = str(column.get("name") or "")
            facts.append(
                {
                    "fact_id": _fact_id("quality", table_name, name),
                    "kind": "column_quality",
                    "label": f"{table.get('display_name') or table_name}.{column.get('label') or name}质量",
                    "value": {
                        key: column.get(key)
                        for key in (
                            "row_count",
                            "sql_null_count",
                            "blank_count",
                            "placeholder_count",
                            "null_count",
                            "non_null_count",
                            "null_rate",
                            "distinct_count",
                            "distinct_rate",
                            "numeric_count",
                            "numeric_rate_sample",
                        )
                    },
                    "evidence": {"table": table_name, "column": name},
                }
            )
            distribution_columns_seen += 1
            if distribution_columns_seen > MAX_DISTRIBUTION_COLUMNS:
                continue
            expression = _value_expression(column)
            tokens = list(column.get("missing_tokens") or [])
            if not tokens:
                tokens = ["", "null", "none"]
            predicate = _missing_predicate(expression, tokens)
            sql = (
                f"SELECT {expression} AS value, COUNT(*) AS value_count "
                f"FROM {_quote_identifier(table_name)} WHERE NOT ({predicate}) "
                f"GROUP BY {expression} ORDER BY value_count DESC, value ASC "
                f"LIMIT {max(1, min(int(max_rows), MAX_TOP_VALUES))}"
            )
            try:
                result = service.execute_readonly_sql(workbook_id, sql, max_rows=MAX_TOP_VALUES)
                top_values = [
                    {"value": str(row[0]), "count": int(row[1])}
                    for row in result.rows
                    if len(row) >= 2
                ]
            except Exception as exc:
                top_values = []
                facts.append(
                    {
                        "fact_id": _fact_id("template-error", table_name, name),
                        "kind": "template_error",
                        "label": f"{table_name}.{name}分布模板错误",
                        "value": str(exc)[:300],
                        "evidence": {"table": table_name, "column": name, "sql": sql},
                    }
                )
            facts.append(
                {
                    "fact_id": _fact_id("distribution", table_name, name),
                    "kind": "distribution",
                    "label": f"{table.get('display_name') or table_name}.{column.get('label') or name}高频值",
                    "value": top_values,
                    "evidence": {"table": table_name, "column": name, "sql": sql},
                }
            )
    return facts
