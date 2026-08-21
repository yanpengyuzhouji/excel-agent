import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app.agent.react_loop import (
    default_report_title,
    parse_react,
    public_answer,
    run_react,
)
from app.agent.workflow_prompt import build_workflow_prompt


class QwenLLM:
    def __init__(self, replies, captured_messages=None):
        self.replies = iter(replies)
        self.captured_messages = captured_messages

    def generate_chat(self, messages):
        if self.captured_messages is not None:
            self.captured_messages.append(messages)
        return next(self.replies)


class ReportService:
    def __init__(self, storage, result=None):
        self.storage = storage
        self.result = result
        self.config = SimpleNamespace(resolved_storage_path=lambda: storage)

    def active_database_path(self, _workbook_id):
        return str(Path(self.storage) / "book.sqlite")

    def execute_readonly_sql(self, _workbook_id, _sql, max_rows=200):
        return SimpleNamespace(**self.result)


class QwenReportRecoveryTests(unittest.TestCase):
    def test_missing_report_title_uses_workbook_domain(self):
        self.assertEqual("设备材料清单分析报告", default_report_title("生成制药设备材料清单报告"))
        self.assertEqual("采购目录分析报告", default_report_title("生成2025年采购目录分析报告"))

    def test_qwen_and_deepseek_use_the_same_workflow_prompt(self):
        self.assertEqual(build_workflow_prompt(False), build_workflow_prompt(True))
        prompt = build_workflow_prompt(False)
        self.assertIn("For greetings, thanks, capability questions", prompt)
        self.assertIn("`html_interpreter`. This is the ONLY way", prompt)
        self.assertNotIn("Structured local-model report mode", prompt)

    def test_qwen3_thinking_block_does_not_interfere_with_sql_action(self):
        action, action_input = parse_react(
            '<think>先分析：Action: terminate</think>\n'
            'Action: sql_query\nAction Input: {"sql":"SELECT COUNT(*) FROM sheet1"}'
        )
        self.assertEqual("sql_query", action)
        self.assertEqual("SELECT COUNT(*) FROM sheet1", action_input["sql"])

    def test_qwen3_thinking_block_is_not_exposed_in_public_answer(self):
        self.assertEqual(
            "已完成 SQL 问答。",
            public_answer("<think>private reasoning</think>\n已完成 SQL 问答。"),
        )

    def test_xml_todowrite_is_parsed_as_a_tool_call(self):
        action, action_input = parse_react(
            "我先规划分析步骤。\n<todowrite>\n"
            '{"todos":[{"content":"探索数据库表结构","status":"in_progress",'
            '"priority":"high"}]}\n</todowrite>'
        )
        self.assertEqual("todowrite", action)
        self.assertEqual("探索数据库表结构", action_input["todos"][0]["content"])

    def test_xml_todowrite_does_not_end_a_report_request_without_html(self):
        with TemporaryDirectory() as storage:
            self._database(storage)
            service = ReportService(storage, {
                "columns": ["类别"], "rows": [["导轨"]],
                "row_count": 1, "truncated": False,
            })
            events = list(run_react(
                QwenLLM([
                    "开始分析。<todowrite>{\"todos\":[{\"content\":\"探索数据\"}]}"
                    "</todowrite>",
                    'Action: html_interpreter\nAction Input: '
                    '{"title":"类别统计","html":"<h1>导轨</h1>"}',
                    'Action: terminate\nAction Input: {"result":"报告已生成"}',
                ]),
                service, "book", "生成分析报告", max_loops=3,
            ))
            html_events = [event for event in events if event.get("type") == "html"]
            self.assertEqual(1, len(html_events))
            self.assertEqual("报告已生成", events[-1]["full_answer"])

    def test_legacy_large_prompt_keeps_free_form_sql_exploration(self):
        prompt = build_workflow_prompt(True)
        self.assertIn("You can use aggregation", prompt)
        self.assertIn("direct answer/no report", prompt)
        self.assertNotIn("SQL Analysis Protocol (mandatory)", prompt)
        self.assertNotIn("every UNION/UNION ALL branch MUST include a literal", prompt)

    def test_common_mode_accepts_qwen_html_on_the_same_path_as_deepseek(self):
        with TemporaryDirectory() as storage:
            self._database(storage)
            service = ReportService(storage, {
                "columns": ["unit", "non_empty_count", "null_count"],
                "rows": [["进线单元", 10, 0], ["计量单元", 5, 5]],
                "row_count": 2, "truncated": False,
            })
            events = list(run_react(
                QwenLLM([
                    'Action: sql_query\nAction Input: {"sql":"SELECT 1"}',
                    'Action: html_interpreter\nAction Input: '
                    '{"title":"完整性报告","html":"<h1>计量单元有5个空值</h1>"}',
                    'Action: terminate\nAction Input: {"result":"报告已生成"}',
                ]),
                service, "book", "生成完整性分析报告", max_loops=3,
            ))
            self.assertEqual(1, len([e for e in events if e.get("type") == "html"]))
            done = next(event for event in events if event.get("type") == "done")
            self.assertEqual("报告已生成", done["full_answer"])
            report = next((Path(storage) / "book" / "reports").glob("*.html"))
            self.assertIn("计量单元有5个空值", report.read_text(encoding="utf-8"))

    def _database(self, storage):
        path = Path(storage) / "book.sqlite"
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE _dbgpt_import_manifest (schema_json TEXT)"
            )
            connection.execute(
                "INSERT INTO _dbgpt_import_manifest VALUES ('[]')"
            )

    def test_multiline_html_json_keeps_structure_newlines_and_html(self):
        reply = (
            "Thought: make a report\nAction: html_interpreter\nAction Input: {\n"
            '  "title": "统计报告",\n'
            '  "html": "<!doctype html>\n<html><body><p>数量</p></body></html>"\n'
            "}"
        )
        action, action_input = parse_react(reply)
        self.assertEqual("html_interpreter", action)
        self.assertIn("<html>", action_input["html"])
        self.assertIn("\n", action_input["html"])

    def test_missing_html_after_sql_uses_safe_query_report(self):
        with TemporaryDirectory() as storage:
            self._database(storage)
            service = ReportService(
                storage,
                {
                    "columns": ["类别", "出现次数"],
                    "rows": [["<塑壳断路器>", 1], ["导轨", 7]],
                    "row_count": 2,
                    "truncated": False,
                },
            )
            events = list(
                run_react(
                    QwenLLM(
                        [
                            'Action: sql_query\nAction Input: {"sql":"SELECT 1"}',
                            'Action: html_interpreter\nAction Input: {"title":"类别统计"}',
                            'Action: terminate\nAction Input: {"result":"已生成报告"}',
                        ]
                    ),
                    service,
                    "book",
                    "按类别统计并生成报告",
                    max_loops=3,
                )
            )
            html_events = [event for event in events if event.get("type") == "html"]
            self.assertEqual(1, len(html_events))
            self.assertFalse(any(event.get("type") == "error" for event in events))
            report_path = next((Path(storage) / "book" / "reports").glob("*.html"))
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("类别统计", report)
            self.assertIn("&lt;塑壳断路器&gt;", report)
            self.assertIn("导轨", report)

    def test_legacy_report_spec_action_is_retried_into_common_html_path(self):
        with TemporaryDirectory() as storage:
            self._database(storage)
            service = ReportService(
                storage,
                {
                    "columns": ["类别", "出现次数"],
                    "rows": [["<塑壳断路器>", 1], ["导轨", 7]],
                    "row_count": 2,
                    "truncated": False,
                },
            )
            events = list(
                run_react(
                    QwenLLM(
                        [
                            'Action: sql_query\nAction Input: {"sql":"SELECT 1"}',
                            'Action: report_spec\nAction Input: {"title":"类别统计","summary":"导轨出现最多","highlights":["导轨"]}',
                            'Action: html_interpreter\nAction Input: {"title":"类别统计","html":"<h1>导轨出现最多</h1>"}',
                            'Action: terminate\nAction Input: {"result":"已生成报告"}',
                        ]
                    ),
                    service,
                    "book",
                    "按类别统计并生成报告",
                    max_loops=4,
                )
            )
            html_events = [event for event in events if event.get("type") == "html"]
            self.assertEqual(1, len(html_events))
            report_path = next((Path(storage) / "book" / "reports").glob("*.html"))
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("导轨出现最多", report)
            self.assertNotIn("<script", report.lower())

    def test_legacy_terminate_keeps_the_model_result_unchanged(self):
        with TemporaryDirectory() as storage:
            self._database(storage)
            service = ReportService(storage, {
                "columns": ["类别"], "rows": [["导轨"]],
                "row_count": 1, "truncated": False,
            })
            events = list(run_react(
                QwenLLM([
                    'Action: sql_query\nAction Input: {"sql":"SELECT 1"}',
                    'Action: terminate\nAction Input: {"result":"统计已完成"}',
                ]), service, "book", "生成统计报告", max_loops=2,
            ))
            self.assertEqual(0, len([e for e in events if e.get("type") == "html"]))
            done = next(event for event in events if event.get("type") == "done")
            self.assertIsNone(done["report_url"])
            self.assertEqual("统计已完成", done["full_answer"])

    def test_qwen3_sql_qa_with_thinking_output(self):
        with TemporaryDirectory() as storage:
            self._database(storage)
            service = ReportService(
                storage,
                {
                    "columns": ["物料数"],
                    "rows": [[10]],
                    "row_count": 1,
                    "truncated": False,
                },
            )
            events = list(
                run_react(
                    QwenLLM(
                        [
                            '<think>需要查询行数</think>\n'
                            'Action: sql_query\nAction Input: '
                            '{"sql":"SELECT COUNT(*) AS 物料数 FROM sheet1"}',
                            '<think>查询结果足够</think>\n'
                            'Action: terminate\nAction Input: '
                            '{"result":"共有 10 条物料记录。"}',
                        ]
                    ),
                    service,
                    "book",
                    "有多少条物料记录？",
                    max_loops=2,
                )
            )
            sql = next(event for event in events if event.get("type") == "sql")
            done = next(event for event in events if event.get("type") == "done")
            self.assertIn("COUNT(*)", sql["sql"])
            self.assertEqual("共有 10 条物料记录。", done["full_answer"])

    def test_qwen3_report_with_thinking_output(self):
        with TemporaryDirectory() as storage:
            self._database(storage)
            service = ReportService(
                storage,
                {
                    "columns": ["类别", "出现次数"],
                    "rows": [["导轨", 7]],
                    "row_count": 1,
                    "truncated": False,
                },
            )
            events = list(
                run_react(
                    QwenLLM(
                        [
                            '<think>先查询类别统计</think>\n'
                            'Action: sql_query\nAction Input: {"sql":"SELECT 1"}',
                            '<think>生成 HTML 报告</think>\n'
                            'Action: html_interpreter\nAction Input: '
                            '{"title":"类别统计","html":"<h1>导轨出现最多</h1>"}',
                            '<think>报告已生成</think>\n'
                            'Action: terminate\nAction Input: '
                            '{"result":"已生成类别统计报告。"}',
                        ]
                    ),
                    service,
                    "book",
                    "生成类别统计报告",
                    max_loops=3,
                )
            )
            html = next(event for event in events if event.get("type") == "html")
            done = next(event for event in events if event.get("type") == "done")
            report = next((Path(storage) / "book" / "reports").glob("*.html"))
            content = report.read_text(encoding="utf-8")
            self.assertEqual("已生成类别统计报告。", done["full_answer"])
            self.assertEqual("类别统计", html["title"])
            self.assertEqual(html["html"], content)
            self.assertIn("导轨出现最多", content)
            self.assertNotIn("<think>", content)

    def test_full_sql_observation_reaches_model_and_report_fallback(self):
        with TemporaryDirectory() as storage:
            self._database(storage)
            rows = [[f"row-{index:03d}", index] for index in range(80)]
            service = ReportService(
                storage,
                {
                    "columns": ["编号", "值"],
                    "rows": rows,
                    "row_count": len(rows),
                    "truncated": False,
                },
            )
            captured = []
            details = []
            events = list(
                run_react(
                    QwenLLM(
                        [
                            'Action: sql_query\nAction Input: {"sql":"SELECT * FROM sheet1"}',
                            'Action: html_interpreter\nAction Input: {"title":"全量报告"}',
                            'Action: terminate\nAction Input: {"result":"已生成报告"}',
                        ],
                        captured,
                    ),
                    service,
                    "book",
                    "生成全量报告",
                    max_loops=3,
                    on_tool_step_full=lambda _action, _observation, detail: details.append(detail),
                )
            )

            second_call = captured[1]
            observation = next(
                message["content"]
                for message in second_call
                if "<untrusted-sql-observation>" in message.get("content", "")
            )
            self.assertIn("row-079", observation)
            self.assertEqual(80, len(details[0]["table"]["rows"]))
            report_path = next((Path(storage) / "book" / "reports").glob("*.html"))
            self.assertIn("row-079", report_path.read_text(encoding="utf-8"))
            self.assertFalse(any(event.get("type") == "error" for event in events))


if __name__ == "__main__":
    unittest.main()
