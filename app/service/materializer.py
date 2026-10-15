"""Materialize an approved workbook draft into a self-contained SQLite file.

Ported verbatim from
DB-GPT packages/dbgpt-serve/src/dbgpt_serve/workbook/service/materializer.py
(PLACEHOLDER_VALUES now imported from .constants).  Pure sqlite3, no dbgpt deps.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any, Dict

from .constants import PLACEHOLDER_VALUES


def _convert_value(raw: Any, sql_type: str) -> Any:
    """Honor the declared public type; REAL uses decimal shortest-roundtrip parity.

    0.1 is accepted, but extra significant digits, overflow and underflow are
    rejected. SQLite REAL arithmetic/aggregates remain approximate. Currency
    values obey the same declared type rules; no currency syntax is stripped.
    """
    if sql_type not in {"INTEGER", "REAL", "TEXT"}:
        raise ValueError(f"Unsupported materializer type: {sql_type}")
    if raw is None:
        return None
    if sql_type == "TEXT":
        return str(raw)
    text = str(raw).strip()
    if text.lower() in PLACEHOLDER_VALUES:
        return None
    if re.match(r"^[+-]?0\d", text):
        raise ValueError(f"Numeric conversion would erase leading zeros: {raw!r}")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid {sql_type} value: {raw!r}") from exc
    if not value.is_finite():
        raise ValueError(f"Nonfinite {sql_type} value: {raw!r}")
    if sql_type == "INTEGER":
        if value != value.to_integral_value() or not -(2**63) <= value < 2**63:
            raise ValueError(f"Not an exact signed64 INTEGER: {raw!r}")
        return int(value)
    result = float(value)
    if (not math.isfinite(result) or Decimal(str(result)) != value or
            (value == value.to_integral_value() and Decimal.from_float(result) != value)):
        raise ValueError(f"Lossy REAL conversion: {raw!r}")
    return result


def _column_letter(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _row_values(row, selected_columns):
    values = []
    for index, column in selected_columns:
        raw = row["values"][index] if index < len(row["values"]) else None
        sql_type = column.get("sql_type", "TEXT").upper()
        if column.get("mixed_numeric"):
            numeric_type = sql_type if sql_type in {"INTEGER", "REAL"} else "REAL"
            values.extend([_convert_value(raw, numeric_type), None if raw is None else str(raw)])
        else:
            values.append(_convert_value(raw, sql_type))
    return values


def _source_facts(sheet):
    """Sheet facts are authoritative; row facts may repeat them identically."""
    facts = dict(sheet.get("cells") or {})
    for row in sheet.get("rows", []):
        source_row = row["source_row"]
        for letter, detail in (row.get("cells") or {}).items():
            coordinate = f"{letter}{source_row}"
            if coordinate in facts and facts[coordinate] != detail:
                raise ValueError(f"Conflicting source facts: {coordinate}")
            facts[coordinate] = detail
        originals = row.get("raw_values", row["values"])
        for index, value in enumerate(originals, 1):
            coordinate = f"{_column_letter(index)}{source_row}"
            merge = sheet.get('merge_lineage',{}).get(coordinate)
            if 'raw_values' not in row and merge and merge.get('source_cell') != coordinate:
                # Legacy drafts retained effective fills plus explicit merge lineage.
                value = None
            facts.setdefault(coordinate, {"raw_value": value, "value": value,
                                         "source_cell": coordinate})
    for row in sheet.get("excluded_rows", []):
        for index, value in enumerate(row.get("values", []), 1):
            coordinate = f"{_column_letter(index)}{row['source_row']}"
            facts.setdefault(coordinate, {"raw_value": value, "value": value,
                                         "source_cell": coordinate})
    for coordinate, fact in facts.items():
        if not re.fullmatch(r"[A-Z]+[1-9][0-9]*", coordinate) or not isinstance(fact, dict):
            raise ValueError(f"Invalid source coordinate/fact: {coordinate}")
    return facts


def _validate_source(sheet, selected_columns, facts):
    rows = sheet.get("rows", [])
    excluded = sheet.get("excluded_rows", [])
    if ("source_record_count" in sheet and
            sheet["source_record_count"] != len(rows) + len(excluded)):
        raise ValueError("Source record count does not reconcile")
    source_rows = [row["source_row"] for row in rows + excluded]
    if any(type(r) is not int or r < 1 for r in source_rows) or len(set(source_rows)) != len(source_rows):
        raise ValueError("Invalid or duplicate source row")
    letters = [column.get("source_column") or _column_letter(index + 1)
               for index, column in selected_columns]
    if len(set(letters)) != len(letters):
        raise ValueError("Duplicate enabled source column")
    classifications = sheet.get('source_classifications')
    if classifications is not None:
        if 'unclassified' in classifications.values():
            raise ValueError('Unclassified source region requires restructuring')
        fact_rows = {int(re.search(r'\d+',cell)[0]) for cell, fact in facts.items()
                     if fact.get('raw_value') not in (None,'')}
        if not fact_rows.issubset({int(r) for r in classifications}):
            raise ValueError('Unclassified source rows remain outside the selected table')
        for row in rows:
            if classifications.get(str(row['source_row'])) != 'data':
                raise ValueError('Business row classification does not reconcile')
    for row in rows:
        for index, column in selected_columns:
            letter = column.get("source_column") or _column_letter(index + 1)
            coordinate = f"{letter}{row['source_row']}"
            edited = row.get("edited_columns", [])
            if any(key in edited for key in
                   (column.get("column_id"), letter, column["physical_name"]) if key is not None):
                continue
            fact = facts.get(coordinate)
            if fact is None:
                raise ValueError(f"Missing source fact: {coordinate}")
            original = fact.get("value", fact.get("raw_value"))
            merge = sheet.get("merge_lineage", {}).get(coordinate)
            if merge and merge.get("source_cell") != coordinate:
                if original not in (None, ""):
                    raise ValueError(f"Merge would overwrite source fact: {coordinate}")
                original = merge["value"]
                anchor = facts.get(merge.get("source_cell"))
                if anchor is not None and anchor.get("value") != original:
                    raise ValueError(f"Merge anchor mismatch: {coordinate}")
            probe = {"values": [original]}
            expected = _row_values(probe, [(0, column)])
            actual_raw = row["values"][index] if index < len(row["values"]) else None
            display_fact = anchor if merge and merge.get('source_cell') != coordinate and anchor is not None else fact
            if (not column.get("mixed_numeric") and
                    column.get("sql_type", "TEXT").upper() == "TEXT" and
                    isinstance(original,(int,float)) and not isinstance(original,bool) and
                    re.fullmatch(r'-?\d+',str(actual_raw)) and
                    Decimal(str(actual_raw)) == Decimal(str(original)) and
                    (display_fact.get('display_value') is None or str(actual_raw)==str(display_fact['display_value']))):
                continue
            actual = _row_values({"values": [actual_raw]}, [(0, column)])
            if expected != actual:
                raise ValueError(f"Source value mismatch: {coordinate}")


def _digest(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, allow_nan=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


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
        connection.execute(
            "CREATE TABLE _dbgpt_source_cells ("
            "table_name TEXT NOT NULL, source_sheet TEXT NOT NULL, "
            "source_cell TEXT NOT NULL, detail_json TEXT NOT NULL, "
            "PRIMARY KEY (table_name, source_sheet, source_cell))"
        )

        schema_manifest = []
        lineage_rows = []
        source_cells = []
        table_checks = []
        for sheet in draft.get("sheets", []):
            facts = _source_facts(sheet)
            _validate_source(sheet, [], facts)
            source_cells.extend(
                (sheet.get("table_name") or "", sheet["name"], coordinate,
                 json.dumps(detail, ensure_ascii=False, allow_nan=False, sort_keys=True))
                for coordinate, detail in facts.items()
            )
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
                    if sql_type not in {"INTEGER", "REAL", "TEXT"}:
                        raise ValueError(f"Unsupported materializer type: {sql_type}")
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
            _validate_source(sheet, selected_columns, facts)
            for row in sheet.get("rows", []):
                values = _row_values(row, selected_columns)
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
            for excluded in sheet.get("excluded_rows", []):
                lineage_rows.append((table_name, sheet["name"], None,
                                     excluded["source_row"], "excluded_row",
                                     json.dumps(excluded, ensure_ascii=False, allow_nan=False)))

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
                    "columns": [
                        dict(column, **({"numeric_precision_note":
                            "SQLite REAL values and aggregates are approximate; "
                            "original values are preserved in _dbgpt_source_cells."}
                            if (column.get("sql_type", "TEXT").upper() == "REAL" or
                                (column.get("mixed_numeric") and column.get("sql_type", "TEXT").upper() != "INTEGER"))
                            else {}))
                        for column in sheet["columns"]
                    ],
                    "row_count": len(insert_rows),
                    "parser": draft.get("parser", {}),
                    "source_record_count": sheet.get("source_record_count", len(insert_rows) + len(sheet.get("excluded_rows", []))),
                    "excluded_row_count": len(sheet.get("excluded_rows", [])),
                    "ingestion_notes": sheet.get("issues", []) + [
                        note for note in (draft.get('validation') or {}).get('warnings',[])
                        if note.get('sheet_id') == sheet.get('sheet_id')],
                    "conversion_schema": [
                        {"name": name, "sql_type": sql_type}
                        for name, sql_type in physical_columns
                    ],
                }
            )
            table_checks.append((sheet, selected_columns, physical_columns, schema_manifest[-1]))

        connection.executemany(
            "INSERT INTO _dbgpt_source_cells VALUES (?, ?, ?, ?)", source_cells
        )
        connection.executemany(
            "INSERT INTO _dbgpt_source_lineage VALUES (?, ?, ?, ?, ?, ?)",
            lineage_rows,
        )
        # Rebuild expectations from the draft, never from insert SQL/parameter lists.
        # Explicit SELECT names detect changed INSERT column order as well as values.
        for sheet, selected_columns, physical_columns, manifest in table_checks:
            table = quote_identifier(sheet["table_name"])
            declared = [(r[1], r[2]) for r in connection.execute(f"PRAGMA table_info({table})")]
            if declared != physical_columns:
                raise ValueError(f"Schema reconciliation failed: {table}")
            names = ", ".join(quote_identifier(name) for name, _ in physical_columns)
            stored = connection.execute(
                f"SELECT {names} FROM {table} ORDER BY _source_row"
            ).fetchall()
            expected = [tuple(_row_values(row, selected_columns) +
                              [sheet["name"], row["source_row"], version])
                        for row in sorted(sheet.get("rows", []), key=lambda r: r["source_row"])]
            if stored != expected:
                raise ValueError(f"Value/coordinate reconciliation failed: {table}")
            storage_types = ", ".join(f"typeof({quote_identifier(name)})" for name, _ in physical_columns)
            actual_types = connection.execute(
                f"SELECT {storage_types} FROM {table} ORDER BY _source_row"
            ).fetchall()
            expected_types = [tuple("null" if value is None else sql_type.lower()
                                    for value, (_, sql_type) in zip(row, physical_columns))
                              for row in expected]
            if actual_types != expected_types:
                raise ValueError(f"Storage type reconciliation failed: {table}")
            manifest["reconciliation"] = {
                "row_count": len(stored), "value_count": len(stored) * (len(physical_columns) - 3),
                "source_record_count": sheet.get("source_record_count", len(expected) + len(sheet.get("excluded_rows", []))),
                "excluded_row_count": len(sheet.get("excluded_rows", [])),
                "sha256": _digest(stored),
            }
        stored_facts = connection.execute(
            "SELECT table_name, source_sheet, source_cell, detail_json "
            "FROM _dbgpt_source_cells ORDER BY table_name, source_sheet, source_cell"
        ).fetchall()
        if stored_facts != sorted(source_cells, key=lambda r: r[:3]):
            raise ValueError("Source fact reconciliation failed")
        stored_lineage = connection.execute(
            "SELECT table_name, source_sheet, source_cell, source_row, kind, detail_json "
            "FROM _dbgpt_source_lineage ORDER BY rowid"
        ).fetchall()
        if stored_lineage != lineage_rows:
            raise ValueError("Lineage reconciliation failed")
        for manifest in schema_manifest:
            manifest["reconciliation"].update(
                source_cell_count=sum(r[0] == manifest["table_name"] for r in stored_facts),
                source_cells_sha256=_digest([r for r in stored_facts if r[0] == manifest["table_name"]]),
            )
        expected_metadata = [
            (item.get("key"), item.get("physical_name"),
             None if item.get("value") is None else str(item["value"]),
             item.get("source_sheet"), item.get("source_cell"))
            for item in draft.get("metadata", [])
        ]
        if connection.execute(
            "SELECT key, physical_name, value, source_sheet, source_cell "
            "FROM project_metadata ORDER BY rowid"
        ).fetchall() != expected_metadata:
            raise ValueError("Metadata reconciliation failed")
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
        if connection.execute(
            "SELECT import_id, workbook_id, version, source_file, source_hash, schema_json "
            "FROM _dbgpt_import_manifest"
        ).fetchall() != [(import_id, workbook_id, version, file_name, source_hash,
                         json.dumps(schema_manifest, ensure_ascii=False))]:
            raise ValueError("Manifest reconciliation failed")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
