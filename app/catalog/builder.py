"""Build bounded, searchable catalog payloads from an active SQLite database."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


MAX_TABLES = 200
MAX_COLUMNS = 200
MAX_TEXT_CHARS = 80_000


def _text(value: Any, limit: int = 500) -> str:
    value = "" if value is None else str(value).replace("\x00", "")
    return value if len(value) <= limit else value[:limit] + "…"


def _quote_identifier(value: str) -> str:
    if not value or not value.replace("_", "a").isalnum() or not (
        value[0].isalpha() or value[0] == "_"
    ):
        raise ValueError(f"Unsafe catalog identifier: {value!r}")
    return '"' + value.replace('"', '""') + '"'


def _load_manifest(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    try:
        row = connection.execute(
            "SELECT schema_json FROM _dbgpt_import_manifest LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        row = None
    if not row or not row[0]:
        rows = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE '_dbgpt%' "
            "AND name != 'project_metadata' ORDER BY name"
        ).fetchall()
        return [{"table_name": item[0], "columns": []} for item in rows]
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        value = []
    return value if isinstance(value, list) else []


def _physical_columns(
    connection: sqlite3.Connection,
    table_name: str,
    columns: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    declared = [item for item in columns if item.get("enabled", True)]
    if declared:
        return declared[:MAX_COLUMNS]
    rows = connection.execute(
        f"PRAGMA table_info({_quote_identifier(table_name)})"
    ).fetchall()
    return [
        {
            "physical_name": row[1],
            "display_name": row[1],
            "sql_type": row[2] or "TEXT",
            "enabled": True,
        }
        for row in rows
        if not str(row[1]).startswith("_")
    ][:MAX_COLUMNS]


def build_catalog_payload(
    database_path: str,
    *,
    workbook_id: str,
    display_name: str,
    source_file: Optional[str],
    import_id: str,
    version: int,
) -> Dict[str, Any]:
    """Return a bounded metadata payload and deterministic schema digest."""

    uri = f"file:{Path(database_path).resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    try:
        tables: List[Dict[str, Any]] = []
        for manifest_table in _load_manifest(connection)[:MAX_TABLES]:
            table_name = _text(manifest_table.get("table_name"), 256)
            if not table_name:
                continue
            try:
                row_count = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}"
                    ).fetchone()[0]
                )
            except sqlite3.Error:
                # A stale/partially-built manifest must not make catalog rows
                # look valid.  Skip the unavailable physical table.
                continue
            columns = []
            for column in _physical_columns(
                connection, table_name, manifest_table.get("columns") or []
            ):
                physical_name = _text(column.get("physical_name"), 256)
                if not physical_name or physical_name.startswith("_"):
                    continue
                columns.append(
                    {
                        "physical_name": physical_name,
                        "display_name": _text(
                            column.get("display_name") or physical_name, 300
                        ),
                        "sql_type": _text(column.get("sql_type") or "TEXT", 32),
                        "mixed_numeric": bool(column.get("mixed_numeric")),
                        'semantic_type':_text(column.get('semantic_type'),32),
                        'unit_hint':_text(column.get('unit_hint'),300),
                        'number_formats':[_text(f,100) for f in column.get('number_formats',[])[:5]],
                    }
                )
            tables.append(
                {
                    "table_name": table_name,
                    "display_name": _text(
                        manifest_table.get("sheet") or table_name, 300
                    ),
                    "row_count": row_count,
                    "columns": columns,
                    'source_record_count':manifest_table.get('source_record_count'),
                    'excluded_row_count':manifest_table.get('excluded_row_count',0),
                    'parser':manifest_table.get('parser') or {},
                    'ingestion_notes':[{'code':_text(n.get('code'),80),'message':_text(n.get('message'),500)}
                        for n in manifest_table.get('ingestion_notes',[])[:20]],
                }
            )
    finally:
        connection.close()

    payload = {
        "workbook_id": workbook_id,
        "display_name": _text(display_name, 500),
        "source_file": _text(source_file, 500),
        "import_id": import_id,
        "version": int(version),
        "tables": tables,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    text_parts = [payload["display_name"], payload["source_file"]]
    for table in tables:
        text_parts.extend([table["table_name"], table["display_name"]])
        for column in table["columns"]:
            text_parts.extend([column["physical_name"], column["display_name"]])
    catalog_text = " ".join(item for item in text_parts if item)
    return {
        "payload": payload,
        "catalog_json": encoded[:MAX_TEXT_CHARS],
        "catalog_text": catalog_text[:MAX_TEXT_CHARS],
        "schema_digest": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }
