"""Materialize an approved workbook draft into a self-contained SQLite file.

Ported verbatim from
DB-GPT packages/dbgpt-serve/src/dbgpt_serve/workbook/service/materializer.py
(PLACEHOLDER_VALUES now imported from .constants).  Pure sqlite3, no dbgpt deps.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict

from .constants import PLACEHOLDER_VALUES


def quote_identifier(value: str) -> str:
    if not value or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return f'"{value}"'


def build_database(
    draft: Dict[str, Any],
    database_path: str,
    *,
    import_id: str,
    workbook_id: str,
    version: int,
    file_name: str,
    source_hash: str,
) -> None:
    """Build a complete database or raise without leaving a committed partial DB."""

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE project_metadata ("
            "key TEXT NOT NULL, physical_name TEXT, value TEXT, "
            "source_sheet TEXT, source_cell TEXT)"
        )
        connection.executemany(
            "INSERT INTO project_metadata VALUES (?, ?, ?, ?, ?)",
            [
                (
                    item.get("key"),
                    item.get("physical_name"),
                    None if item.get("value") is None else str(item.get("value")),
                    item.get("source_sheet"),
                    item.get("source_cell"),
                )
                for item in draft.get("metadata", [])
            ],
        )
        connection.execute(
            "CREATE TABLE _dbgpt_import_manifest ("
            "import_id TEXT, workbook_id TEXT, version INTEGER, source_file TEXT, "
            "source_hash TEXT, schema_json TEXT, imported_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE _dbgpt_source_lineage ("
            "table_name TEXT, source_sheet TEXT, source_cell TEXT, source_row INTEGER, "
            "kind TEXT, detail_json TEXT)"
        )

        schema_manifest = []
        lineage_rows = []
        for sheet in draft.get("sheets", []):
            if not sheet.get("enabled") or sheet.get("kind") != "data":
                continue
            table_name = sheet["table_name"]
            physical_columns = []
            selected_columns = []
            for index, column in enumerate(sheet["columns"]):
                if not column.get("enabled"):
                    continue
                selected_columns.append((index, column))
                name = column["physical_name"]
                sql_type = column.get("sql_type", "TEXT").upper()
                if column.get("mixed_numeric"):
                    numeric_type = (
                        "REAL" if sql_type not in {"INTEGER", "REAL"} else sql_type
                    )
                    physical_columns.extend(
                        [(f"{name}_value", numeric_type), (f"{name}_raw", "TEXT")]
                    )
                else:
                    physical_columns.append((name, sql_type))
            physical_columns.extend(
                [
                    ("_source_sheet", "TEXT"),
                    ("_source_row", "INTEGER"),
                    ("_import_version", "INTEGER"),
                ]
            )
            ddl = ", ".join(
                f"{quote_identifier(name)} {sql_type}"
                for name, sql_type in physical_columns
            )
            connection.execute(f"CREATE TABLE {quote_identifier(table_name)} ({ddl})")
            placeholders = ",".join("?" for _ in physical_columns)
            insert_sql = (
                f"INSERT INTO {quote_identifier(table_name)} VALUES ({placeholders})"
            )
            insert_rows = []
            for row in sheet.get("rows", []):
                values = []
                for index, column in selected_columns:
                    raw = row["values"][index] if index < len(row["values"]) else None
                    if column.get("mixed_numeric"):
                        normalized = None
                        if (
                            raw is not None
                            and str(raw).strip().lower() not in PLACEHOLDER_VALUES
                        ):
                            try:
                                normalized = float(raw)
                                if column.get("sql_type") == "INTEGER":
                                    normalized = int(normalized)
                            except (TypeError, ValueError):
                                normalized = None
                        values.extend([normalized, None if raw is None else str(raw)])
                    elif raw is None:
                        values.append(None)
                    elif column.get("sql_type") == "INTEGER":
                        values.append(int(float(raw)))
                    elif column.get("sql_type") == "REAL":
                        values.append(float(raw))
                    else:
                        values.append(str(raw))
                values.extend([sheet["name"], row["source_row"], version])
                insert_rows.append(tuple(values))
                if row.get("hidden"):
                    lineage_rows.append(
                        (
                            table_name,
                            sheet["name"],
                            None,
                            row["source_row"],
                            "hidden_row",
                            json.dumps({"hidden": True}, ensure_ascii=False),
                        )
                    )
            connection.executemany(insert_sql, insert_rows)

            for coordinate, formula in sheet.get("formulas", {}).items():
                source_row = int(re.sub(r"\D", "", coordinate))
                if source_row < int(sheet.get("data_start_row") or 1):
                    continue
                lineage_rows.append(
                    (
                        table_name,
                        sheet["name"],
                        coordinate,
                        source_row,
                        "formula",
                        json.dumps(formula, ensure_ascii=False),
                    )
                )
            for coordinate, merge in sheet.get("merge_lineage", {}).items():
                if coordinate != merge.get("source_cell"):
                    lineage_rows.append(
                        (
                            table_name,
                            sheet["name"],
                            coordinate,
                            int(re.sub(r"\D", "", coordinate)),
                            "merged_fill",
                            json.dumps(merge, ensure_ascii=False),
                        )
                    )
            schema_manifest.append(
                {
                    "sheet": sheet["name"],
                    "table_name": table_name,
                    "columns": sheet["columns"],
                    "row_count": len(insert_rows),
                }
            )

        connection.executemany(
            "INSERT INTO _dbgpt_source_lineage VALUES (?, ?, ?, ?, ?, ?)",
            lineage_rows,
        )
        connection.execute(
            "INSERT INTO _dbgpt_import_manifest "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                import_id,
                workbook_id,
                version,
                file_name,
                source_hash,
                json.dumps(schema_manifest, ensure_ascii=False),
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
