"""Multi-workbook Agent loop with server-enforced source isolation."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Generator, Iterable, List, Optional, Sequence, Tuple

from .budget import AgentBudget, BudgetExceeded
from .react_loop import (
    _encode_untrusted_json,
    build_untrusted_data_message,
    parse_react,
    public_answer,
)

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 20
MAX_SELECTED_SOURCES = 5
MAX_QUERIED_SOURCES = 3
MAX_INSPECT_TABLES = 20
MAX_OBSERVATION_CHARS = 120_000


MULTI_WORKBOOK_PROMPT = """You are the multi-workbook Excel data Agent.
You answer only from authorized workbook data supplied in the conversation.
Each workbook is an isolated SQLite database. Never use ATTACH, never invent a
database path, and never query a workbook that is not in the authorized source
list. You must use the exact table and physical column names from inspect results.

Available actions:
Action: search_workbooks
Action Input: {"query":"keywords"}

Action: inspect_workbook
Action Input: {"workbook_id":"..."}

Action: query_workbook
Action Input: {"workbook_id":"...","sql":"SELECT ..."}

Action: aggregate_results
Action Input: {"operation":"union"} or {"operation":"sum","group_by":["name"],"value_column":"total_quantity"}

Action: terminate
Action Input: {"result":"final answer"}

Use one action at a time. For questions involving multiple workbooks, query each
source independently and compare only the returned results. Every numeric claim
in the final answer must be supported by a query result. If a source fails, say
the result is partial and identify the failed source. Do not expose private
reasoning or repeat tool protocol in the final answer.
"""


def _bounded(value: Any, limit: int = 500) -> str:
    value = "" if value is None else str(value).replace("\x00", "")
    return value if len(value) <= limit else value[:limit] + "…"


def _tokens(value: str) -> List[str]:
    folded = str(value or "").casefold()
    parts = [item for item in re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]{2,}", folded)]
    if not parts and folded:
        parts = [folded]
    return parts[:30]


def search_catalogs(catalogs: Sequence[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """Rank already owner-filtered catalog rows without exposing other owners."""

    terms = _tokens(query)
    scored: List[Tuple[int, Dict[str, Any]]] = []
    for item in catalogs:
        catalog = item.get("catalog") or {}
        haystack = " ".join(
            [
                str(item.get("display_name") or ""),
                str(item.get("source_file") or ""),
                str(item.get("catalog_text") or ""),
                json.dumps(catalog, ensure_ascii=False),
            ]
        ).casefold()
        score = sum(3 if term in haystack else 0 for term in terms)
        if query and str(query).casefold() in haystack:
            score += 5
        scored.append((score, item))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("display_name") or "")))
    return [item for _score, item in scored[:MAX_CANDIDATES]]


def _source_header(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "workbook_id": snapshot["workbook_id"],
        "display_name": snapshot.get("display_name") or snapshot["workbook_id"],
        "import_id": snapshot["import_id"],
        "version": snapshot["version"],
    }


def _catalog_context(catalogs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    for item in catalogs:
        catalog = item.get("catalog") or {}
        result.append(
            {
                "workbook_id": item.get("workbook_id"),
                "display_name": item.get("display_name"),
                "source_file": item.get("source_file"),
                "active_import_id": item.get("active_import_id"),
                "active_version": item.get("active_version"),
                "tables": [
                    {
                        "table_name": table.get("table_name"),
                        "display_name": table.get("display_name"),
                        "row_count": table.get("row_count"),
                        "columns": table.get("columns", [])[:100],
                    }
                    for table in (catalog.get("tables") or [])[:MAX_INSPECT_TABLES]
                ],
            }
        )
    return result


def _inspect_source(
    service: Any,
    snapshot: Dict[str, Any],
    catalog: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    catalog_payload = (catalog or {}).get("catalog") or {}
    tables = []
    for table in (catalog_payload.get("tables") or [])[:MAX_INSPECT_TABLES]:
        table_name = str(table.get("table_name") or "")
        if not table_name:
            continue
        try:
            result = service.execute_readonly_sql_snapshot(
                snapshot, f'SELECT * FROM "{table_name.replace(chr(34), chr(34) * 2)}" LIMIT 5', max_rows=5
            )
            tables.append(
                {
                    "table_name": table_name,
                    "display_name": table.get("display_name") or table_name,
                    "row_count": table.get("row_count"),
                    "columns": table.get("columns") or [],
                    "sample_columns": result.columns,
                    "sample_rows": result.rows,
                }
            )
        except Exception as exc:
            tables.append(
                {
                    "table_name": table_name,
                    "display_name": table.get("display_name") or table_name,
                    "row_count": table.get("row_count"),
                    "columns": table.get("columns") or [],
                    "sample_error": _bounded(exc, 300),
                }
            )
    return {"source": _source_header(snapshot), "tables": tables}


def _observation(value: Any) -> str:
    encoded = _encode_untrusted_json(value)
    return encoded[:MAX_OBSERVATION_CHARS]


def _aggregate_results(
    results: Sequence[Dict[str, Any]], action_input: Dict[str, Any]
) -> Dict[str, Any]:
    """Aggregate already materialized result rows without cross-database SQL."""

    if not results:
        return {"error": "No query results are available to aggregate"}
    operation = str(action_input.get("operation") or "union").casefold()
    if operation in {"union", "union_by_workbook", "compare"}:
        columns: List[str] = []
        for result in results:
            for column in result.get("columns") or []:
                if column not in columns:
                    columns.append(str(column))
        columns = columns[:100]
        rows = []
        for result in results:
            source = result.get("source") or {}
            indexes = {str(column): index for index, column in enumerate(result.get("columns") or [])}
            for raw_row in (result.get("rows") or [])[:500]:
                values = list(raw_row)
                rows.append(
                    [source.get("workbook_id"), source.get("display_name")]
                    + [values[indexes[column]] if column in indexes and indexes[column] < len(values) else None for column in columns]
                )
        return {
            "operation": "union",
            "columns": ["workbook_id", "workbook_name"] + columns,
            "rows": rows[:1000],
            "row_count": min(len(rows), 1000),
            "truncated": len(rows) > 1000,
            "sources": [result.get("source") for result in results],
        }
    if operation in {"sum", "group_sum", "sum_by_group"}:
        group_by = [str(item) for item in (action_input.get("group_by") or [])][:10]
        value_column = str(action_input.get("value_column") or "")
        if not value_column:
            return {"error": "value_column is required for sum aggregation"}
        grouped: Dict[Tuple[Any, ...], float] = {}
        for result in results:
            columns = [str(item) for item in result.get("columns") or []]
            indexes = {column: index for index, column in enumerate(columns)}
            if value_column not in indexes or any(column not in indexes for column in group_by):
                continue
            for raw_row in result.get("rows") or []:
                try:
                    value = float(raw_row[indexes[value_column]])
                except (TypeError, ValueError, IndexError):
                    continue
                key = tuple(raw_row[indexes[column]] for column in group_by)
                grouped[key] = grouped.get(key, 0.0) + value
        rows = [list(key) + [value] for key, value in sorted(grouped.items(), key=lambda item: str(item[0]))]
        return {
            "operation": "sum",
            "columns": group_by + [value_column],
            "rows": rows[:1000],
            "row_count": min(len(rows), 1000),
            "truncated": len(rows) > 1000,
            "sources": [result.get("source") for result in results],
        }
    return {"error": "Unsupported aggregation operation"}


def run_multi_workbook_agent(
    llm: Any,
    service: Any,
    *,
    owner_id: str,
    question: str,
    catalogs: Sequence[Dict[str, Any]],
    selected_workbook_ids: Optional[Sequence[str]] = None,
    history_messages: Optional[Sequence[Dict[str, str]]] = None,
    relations: Optional[Sequence[Dict[str, Any]]] = None,
    max_selected_sources: int = MAX_SELECTED_SOURCES,
    max_queried_sources: int = MAX_QUERIED_SOURCES,
    max_loops: int = 10,
    sql_max_rows: int = 500,
    budget: Optional[AgentBudget] = None,
) -> Generator[Dict[str, Any], None, None]:
    """Yield SSE-ready events for an owner-scoped multi-workbook run."""

    catalog_by_id = {
        str(item.get("workbook_id")): item
        for item in catalogs
        if item.get("workbook_id")
    }
    selected_ids = [str(item) for item in (selected_workbook_ids or [])]
    if selected_ids:
        if len(selected_ids) > max(1, int(max_selected_sources)):
            raise ValueError(
                f"Maximum selected workbooks is {max(1, int(max_selected_sources))}"
            )
        unknown = [item for item in selected_ids if item not in catalog_by_id]
        if unknown:
            raise ValueError("One or more selected workbooks are not authorized")
        allowed_catalogs = [catalog_by_id[item] for item in selected_ids]
    else:
        allowed_catalogs = list(catalogs)[:MAX_CANDIDATES]
    if not allowed_catalogs:
        raise ValueError("No authorized workbook catalogs are available")

    snapshots: Dict[str, Dict[str, Any]] = {}
    for item in allowed_catalogs:
        workbook_id = str(item["workbook_id"])
        snapshots[workbook_id] = service.active_database_snapshot(
            workbook_id, owner_id=owner_id
        )
    allowlist = set(snapshots)
    queried_sources: set[str] = set()
    inspected_sources: set[str] = set()
    failed_sources: set[str] = set()
    if budget is None:
        budget = AgentBudget()

    yield {
        "type": "scope.resolved",
        "mode": "selected" if selected_ids else "auto",
        "sources": [_source_header(snapshots[item]) for item in snapshots],
        "relations": list(relations or []),
    }
    yield {
        "type": "catalog.candidates",
        "candidates": _catalog_context(allowed_catalogs),
        "relations": list(relations or []),
    }

    messages: List[Dict[str, str]] = [{"role": "system", "content": MULTI_WORKBOOK_PROMPT}]
    for item in history_messages or []:
        role = item.get("role")
        content = str(item.get("content") or "")
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    messages.append(
        {
            "role": "user",
            "content": build_untrusted_data_message(
                "authorized-workbook-catalog",
                _observation(
                    {
                        "question": question,
                        "sources": _catalog_context(allowed_catalogs),
                        "relations": list(relations or []),
                    }
                ),
            ),
        }
    )
    last_results: List[Dict[str, Any]] = []

    for round_num in range(1, max(1, int(max_loops)) + 1):
        yield {"type": "status", "status": "thinking", "round": round_num}
        try:
            budget.before_llm(messages)
            reply = llm.generate_chat(messages)
            budget.after_llm(reply)
        except BudgetExceeded as exc:
            yield {"type": "error", "error": str(exc), "done": True}
            return
        except Exception as exc:
            logger.exception("multi-workbook Agent LLM call failed")
            yield {"type": "error", "error": str(exc), "done": True}
            return

        action, action_input = parse_react(reply)
        action_lower = str(action or "").casefold()
        if action_lower == "search_workbooks":
            query = str(action_input.get("query") or question)
            matches = search_catalogs(allowed_catalogs, query)
            observation = {"query": query, "candidates": _catalog_context(matches)}
            yield {"type": "catalog.candidates", "candidates": observation["candidates"]}
        elif action_lower in {"inspect_workbook", "inspect_source"}:
            workbook_id = str(action_input.get("workbook_id") or "")
            if workbook_id not in allowlist:
                observation = {"error": "Workbook is outside the authorized source scope"}
            else:
                inspected_sources.add(workbook_id)
                observation = _inspect_source(
                    service, snapshots[workbook_id], catalog_by_id.get(workbook_id)
                )
                yield {"type": "source.selected", "source": observation["source"]}
                yield {"type": "source.inspect", **observation}
        elif action_lower in {"query_workbook", "sql_query"}:
            workbook_id = str(action_input.get("workbook_id") or "")
            if not workbook_id and len(allowlist) == 1:
                workbook_id = next(iter(allowlist))
            sql = str(action_input.get("sql") or "").strip()
            if workbook_id not in allowlist:
                observation = {"error": "Workbook is outside the authorized source scope"}
            elif workbook_id not in queried_sources and len(queried_sources) >= max(1, int(max_queried_sources)):
                observation = {
                    "error": f"Maximum queried workbooks is {max(1, int(max_queried_sources))}"
                }
            elif not sql:
                observation = {"error": "SQL is required"}
            else:
                try:
                    yield {
                        "type": "source.query.start",
                        "source": _source_header(snapshots[workbook_id]),
                        "sql": sql,
                    }
                    budget.before_tool("sql_query")
                    result = service.execute_readonly_sql_snapshot(
                        snapshots[workbook_id], sql, max_rows=sql_max_rows
                    )
                    queried_sources.add(workbook_id)
                    source = _source_header(snapshots[workbook_id])
                    observation = {
                        "source": source,
                        "sql": result.sql,
                        "columns": result.columns,
                        "rows": result.rows,
                        "row_count": result.row_count,
                        "truncated": result.truncated,
                    }
                    last_results.append(observation)
                    yield {"type": "source.query.result", **observation}
                except Exception as exc:
                    failed_sources.add(workbook_id)
                    observation = {
                        "source": _source_header(snapshots[workbook_id]),
                        "error": _bounded(exc, 500),
                    }
                    yield {"type": "source.query.error", **observation}
        elif action_lower in {"aggregate_results", "aggregate", "merge_results"}:
            observation = _aggregate_results(last_results, action_input)
            if "error" not in observation:
                yield {"type": "aggregate.result", **observation}
        elif action_lower == "terminate":
            final = public_answer(action_input.get("result", ""))
            if not final:
                final = "分析已完成，但模型未提供可公开的最终答案。"
            yield {
                "type": "done",
                "done": True,
                "full_answer": final,
                "sources": [_source_header(snapshots[item]) for item in queried_sources],
                "failed_sources": [
                    _source_header(snapshots[item]) for item in failed_sources
                ],
                "partial": bool(failed_sources),
            }
            return
        elif action is None and public_answer(reply):
            final = public_answer(reply)
            yield {
                "type": "done",
                "done": True,
                "full_answer": final,
                "sources": [_source_header(snapshots[item]) for item in queried_sources],
                "failed_sources": [
                    _source_header(snapshots[item]) for item in failed_sources
                ],
                "partial": bool(failed_sources),
            }
            return
        else:
            observation = {
                "error": "Invalid action. Use search_workbooks, inspect_workbook, query_workbook, or terminate."
            }

        messages.append({"role": "assistant", "content": reply})
        messages.append(
            {
                "role": "user",
                "content": build_untrusted_data_message(
                    "multi-workbook-observation", _observation(observation)
                ),
            }
        )

    yield {
        "type": "error",
        "error": f"Multi-workbook Agent reached the maximum of {max_loops} rounds",
        "done": True,
    }
