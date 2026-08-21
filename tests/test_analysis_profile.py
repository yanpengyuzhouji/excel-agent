import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.analysis.profile import build_workbook_profile


class _ProfileService:
    def __init__(self, database_path):
        self.database_path = database_path

    def active_database_path(self, _workbook_id):
        return self.database_path


class WorkbookProfileTests(unittest.TestCase):
    def test_profile_counts_placeholders_and_emits_generic_hints(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "database.sqlite"
            connection = sqlite3.connect(database_path)
            connection.execute(
                "CREATE TABLE material_rows ("
                "item_name TEXT, quantity_value REAL, quantity_raw TEXT, "
                "material_code TEXT, event_date TEXT, _source_sheet TEXT, "
                "_source_row INTEGER, _import_version INTEGER)"
            )
            connection.executemany(
                "INSERT INTO material_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("断路器", 2, "2", "M-1", "2026-01-01", "物料明细", 2, 1),
                    ("电缆", None, "/", "M-2", "2026-01-02", "物料明细", 3, 1),
                    ("断路器", 3, "3", "M-1", "", "物料明细", 4, 1),
                    ("", None, "", None, None, "物料明细", 5, 1),
                ],
            )
            manifest = [
                {
                    "sheet": "物料明细",
                    "table_name": "material_rows",
                    "columns": [
                        {"physical_name": "item_name", "display_name": "物料描述", "sql_type": "TEXT", "enabled": True},
                        {"physical_name": "quantity", "display_name": "数量", "sql_type": "REAL", "mixed_numeric": True, "enabled": True},
                        {"physical_name": "material_code", "display_name": "物料编码", "sql_type": "TEXT", "enabled": True},
                        {"physical_name": "event_date", "display_name": "日期", "sql_type": "TEXT", "enabled": True},
                    ],
                    "row_count": 4,
                }
            ]
            connection.execute(
                "CREATE TABLE _dbgpt_import_manifest (schema_json TEXT)"
            )
            connection.execute(
                "INSERT INTO _dbgpt_import_manifest VALUES (?)",
                (__import__("json").dumps(manifest, ensure_ascii=False),),
            )
            connection.commit()
            connection.close()

            profile = build_workbook_profile(_ProfileService(str(database_path)), "book-1")

        self.assertEqual(1, profile["table_count"])
        self.assertEqual(4, profile["row_count"])
        columns = {item["name"]: item for item in profile["tables"][0]["columns"]}
        self.assertEqual(1, columns["item_name"]["null_count"])
        self.assertEqual(2, columns["quantity"]["null_count"])
        self.assertEqual(2, columns["quantity"]["numeric_count"])
        self.assertEqual("numeric_candidate", columns["quantity"]["semantic_type"])
        self.assertEqual(2, columns["material_code"]["distinct_count"])
        self.assertIn("material", {item["name"] for item in profile["candidate_domains"]})

    def test_profile_falls_back_to_sqlite_schema_without_manifest_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "database.sqlite"
            connection = sqlite3.connect(database_path)
            connection.execute("CREATE TABLE sheet1 (status TEXT, amount REAL)")
            connection.executemany("INSERT INTO sheet1 VALUES (?, ?)", [("ok", 1.5), ("/", None)])
            connection.execute("CREATE TABLE _dbgpt_import_manifest (schema_json TEXT)")
            connection.execute("INSERT INTO _dbgpt_import_manifest VALUES (?)", ("[]",))
            connection.commit()
            connection.close()

            profile = build_workbook_profile(_ProfileService(str(database_path)), "book-2")

        self.assertEqual("status", profile["tables"][0]["columns"][0]["name"])
        self.assertEqual(1, profile["tables"][0]["columns"][0]["null_count"])
        self.assertEqual(1, profile["tables"][0]["columns"][1]["numeric_count"])


if __name__ == "__main__":
    unittest.main()
