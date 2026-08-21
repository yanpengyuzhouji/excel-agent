import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.analysis.pipeline import build_analysis_bundle
from app.analysis.report import render_report_html, validate_report_spec
from app.analysis.routing import select_analysis_route
from app.analysis.small_route import run_small_route


class _AnalysisService:
    def __init__(self, database_path, storage_path):
        self.database_path = str(database_path)
        self.config = SimpleNamespace(resolved_storage_path=lambda: str(storage_path))

    def active_database_path(self, _workbook_id):
        return self.database_path

    def execute_readonly_sql(self, _workbook_id, sql, max_rows=200):
        connection = sqlite3.connect(self.database_path)
        try:
            cursor = connection.execute(sql)
            columns = [item[0] for item in cursor.description or []]
            rows = cursor.fetchmany(max_rows + 1)
        finally:
            connection.close()
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        return SimpleNamespace(
            sql=sql,
            columns=columns,
            rows=[list(row) for row in rows],
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=0,
        )


def _make_database(directory):
    path = Path(directory) / "database.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sheet1 (status TEXT, amount TEXT)")
    connection.executemany("INSERT INTO sheet1 VALUES (?, ?)", [("ok", "1"), ("ok", "2"), ("/", "/")])
    manifest = [{
        "sheet": "Sheet1",
        "table_name": "sheet1",
        "columns": [
            {"physical_name": "status", "display_name": "状态", "sql_type": "TEXT", "enabled": True},
            {"physical_name": "amount", "display_name": "金额", "sql_type": "TEXT", "enabled": True},
        ],
        "row_count": 3,
    }]
    connection.execute("CREATE TABLE _dbgpt_import_manifest (schema_json TEXT)")
    connection.execute("INSERT INTO _dbgpt_import_manifest VALUES (?)", (json.dumps(manifest, ensure_ascii=False),))
    connection.commit()
    connection.close()
    return path


class AnalysisRouteTests(unittest.TestCase):
    def test_route_selection_prefers_explicit_metadata_and_30b_boundary(self):
        self.assertEqual("large", select_analysis_route({"name": "Qwen3-4B"}, requested_route="large")["route"])
        self.assertEqual("small", select_analysis_route({"name": "Qwen3-14B"})["route"])
        self.assertEqual("large", select_analysis_route({"name": "Qwen3-30B"})["route"])
        self.assertEqual("large", select_analysis_route({"name": "deepseek-v4-flash"})["route"])

    def test_bundle_report_references_only_known_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = _make_database(directory)
            service = _AnalysisService(database_path, directory)
            bundle = build_analysis_bundle(service, "book")
            html = render_report_html(bundle["report"])

        self.assertEqual([], validate_report_spec(bundle["report"], bundle["facts"], bundle["findings"]))
        self.assertIn("FCT-OVERVIEW-ROWS", html)
        self.assertIn("evidence", html)
        self.assertNotIn("<script", html.lower())
        errors = validate_report_spec(
            {"template": "generic-analysis", "title": "x", "sections": []},
            bundle["facts"],
            bundle["findings"],
        )
        self.assertTrue(errors)

    def test_small_route_renders_fixed_report_without_llm(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = _make_database(directory)
            service = _AnalysisService(database_path, directory)
            details = []
            events = list(run_small_route(
                service,
                "book",
                "请生成通用数据分析报告",
                on_tool_step_full=lambda *args: details.append(args),
            ))
            report_events = [event for event in events if event.get("type") == "html"]
            done = [event for event in events if event.get("type") == "done"][-1]
            report_files = list(Path(directory).glob("book/reports/*.html"))
            report_content = report_files[0].read_text(encoding="utf-8")

        self.assertEqual(1, len(report_events))
        self.assertEqual(
            ["step.start", "step.meta", "step.chunk", "step.chunk", "step.done"],
            [event.get("type") for event in events if event.get("type", "").startswith("step.")],
        )
        self.assertEqual(
            report_events[0]["html"], report_content
        )
        self.assertEqual(1, len(details))
        self.assertEqual(details[0][2]["html"]["html"], report_content)
        self.assertIn("/reports/", done["report_url"])
        self.assertTrue(report_files)


if __name__ == "__main__":
    unittest.main()
