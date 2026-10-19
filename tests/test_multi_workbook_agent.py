import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.agent.multi_workbook import (
    _aggregate_results,
    run_multi_workbook_agent,
    search_catalogs,
)
from app.catalog.builder import build_catalog_payload
from app.api.schemas import SQLQueryResponse
from app.service.service import WorkbookImportService
from app.agent.budget import AgentBudget


class CatalogBuilderTests(unittest.TestCase):
    def test_catalog_contains_manifest_schema_without_business_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.sqlite"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE purchase_items (code TEXT, qty REAL)")
                connection.execute("INSERT INTO purchase_items VALUES ('M1', 2)")
                connection.execute("CREATE TABLE _dbgpt_import_manifest (schema_json TEXT)")
                connection.execute(
                    "INSERT INTO _dbgpt_import_manifest VALUES (?)",
                    (
                        json.dumps(
                            [
                                {
                                    "sheet": "采购明细",
                                    "table_name": "purchase_items",
                                    "columns": [
                                        {
                                            "physical_name": "code",
                                            "display_name": "物料编码",
                                            "sql_type": "TEXT",
                                            "enabled": True,
                                        },
                                        {
                                            "physical_name": "qty",
                                            "display_name": "数量",
                                            "sql_type": "REAL",
                                            "enabled": True,
                                        },
                                    ],
                                }
                            ],
                            ensure_ascii=False,
                        ),
                    ),
                )
                connection.commit()

            result = build_catalog_payload(
                str(path),
                workbook_id="wb-1",
                display_name="采购目录",
                source_file="采购.xlsx",
                import_id="import-1",
                version=2,
            )

        self.assertEqual("wb-1", result["payload"]["workbook_id"])
        self.assertEqual(1, len(result["payload"]["tables"]))
        self.assertEqual(1, result["payload"]["tables"][0]["row_count"])
        self.assertIn("物料编码", result["catalog_text"])
        self.assertEqual(64, len(result["schema_digest"]))
        self.assertNotIn("M1", result["catalog_json"])

    def test_search_only_ranks_the_already_authorized_catalog_set(self):
        catalogs = [
            {"workbook_id": "allowed", "display_name": "采购目录", "catalog": {"tables": []}},
            {"workbook_id": "other", "display_name": "人事台账", "catalog": {"tables": []}},
        ]
        matches = search_catalogs(catalogs, "采购")
        self.assertEqual("allowed", matches[0]["workbook_id"])
        self.assertEqual({"allowed", "other"}, {item["workbook_id"] for item in matches})


class MultiWorkbookAgentTests(unittest.TestCase):
    def test_final_answer_streams_after_query_without_exposing_terminate(self):
        replies = iter(['Action: query_workbook\nAction Input: {"workbook_id":"a","sql":"SELECT SUM(amount) AS total FROM table_1"}',
                        'Action: terminate\nAction Input: {"result":"总额{{result-1:0:total}}万元"}'])
        streamed_messages = []
        stream_finished = []
        class LLM:
            def generate_chat(self, messages):
                return next(replies)
            def generate_chat_stream(self, messages):
                streamed_messages.append(messages)
                yield '查询总额为7万元。' + '这是基于当前工作簿的查询结果。'*8
                yield '没有重复查询。'
                stream_finished.append(True)
        service = SimpleNamespace(
            active_database_snapshot=lambda wid, owner_id=None: {'workbook_id':wid, 'import_id':'i', 'version':1},
            execute_readonly_sql_snapshot=lambda snapshot, sql, max_rows=500: SQLQueryResponse(
                sql=sql, columns=['total'], rows=[[7]], row_count=1, truncated=False, elapsed_ms=.1))
        budget = AgentBudget()
        events = []
        for event in run_multi_workbook_agent(LLM(), service, owner_id='owner', question='分析金额',
                catalogs=[{'workbook_id':'a'}], selected_workbook_ids=['a'], budget=budget):
            events.append(event)
            if event['type'] == 'answer.delta' and not any(e['type']=='answer.delta' for e in events[:-1]):
                self.assertFalse(stream_finished, 'First delta must precede stream completion')
        deltas = ''.join(e['text'] for e in events if e['type']=='answer.delta')
        self.assertEqual(events[-1]['full_answer'], deltas)
        self.assertNotIn('Action:', deltas)
        self.assertEqual(budget.llm_calls, 2)
        self.assertEqual(budget.sql_calls, 1)
        self.assertFalse(streamed_messages, 'Validated numeric answers must not be rewritten by another model call')
        self.assertEqual('总额7万元', deltas)

    def test_final_stream_failure_is_not_saved_as_success(self):
        class LLM:
            def generate_chat(self, messages):
                return 'Action: terminate\nAction Input: {"result":"没有查询"}'
            def generate_chat_stream(self, messages):
                yield '回答片段'
                raise TimeoutError('test timeout')
        service = SimpleNamespace(active_database_snapshot=lambda wid, owner_id=None:
            {'workbook_id':wid, 'import_id':'i', 'version':1})
        events = list(run_multi_workbook_agent(LLM(), service, owner_id='owner', question='说明',
            catalogs=[{'workbook_id':'a'}]))
        self.assertEqual(events[-1]['type'], 'error')
        self.assertFalse(any(e['type']=='done' for e in events))

    def test_search_tool_respects_budget(self):
        llm = SimpleNamespace(generate_chat=lambda messages:
            'Action: search_workbooks\nAction Input: {"query":"count"}')
        service = SimpleNamespace(active_database_snapshot=lambda wid, owner_id=None:
            {'workbook_id':wid, 'import_id':'i', 'version':1})
        events = list(run_multi_workbook_agent(llm, service, owner_id='owner', question='count',
            catalogs=[{'workbook_id':'a'}], budget=AgentBudget(max_tool_calls=0)))
        self.assertEqual('error', events[-1]['type'])
        self.assertIn('tool-call budget', events[-1]['error'])
    def test_empty_selected_scope_never_expands_to_auto(self):
        with self.assertRaisesRegex(ValueError, 'empty'):
            list(run_multi_workbook_agent(SimpleNamespace(), SimpleNamespace(),
                owner_id='owner', question='count', catalogs=[{'workbook_id':'a'}],
                selected_workbook_ids=[]))

    def test_current_question_is_an_instruction_and_tool_trace_is_retried(self):
        captured = []
        replies = iter(['Action Input: {"sql":"SELECT 1"}',
                        'Action: terminate\nAction Input: {"result":"No data queried"}'])
        class LLM:
            def generate_chat(self, messages):
                captured.append(list(messages))
                return next(replies)
        service = SimpleNamespace(active_database_snapshot=lambda wid, owner_id=None:
            {'workbook_id':wid, 'import_id':'i', 'version':1})
        events = list(run_multi_workbook_agent(LLM(), service, owner_id='owner', question='count',
            catalogs=[{'workbook_id':'a'}], max_loops=2))
        self.assertEqual({'role':'user','content':'count'}, captured[0][-1])
        self.assertEqual(2, len(captured))
        self.assertEqual('No data queried', events[-1]['full_answer'])

    def test_aggregate_propagates_truncated_inputs(self):
        result = _aggregate_results([{'source':{'workbook_id':'a'}, 'columns':['total'],
            'rows':[[2]], 'truncated':True}], {'operation':'union'})
        self.assertTrue(result['truncated'])

    def test_controlled_aggregate_keeps_source_provenance(self):
        result = _aggregate_results(
            [
                {
                    "source": {"workbook_id": "a", "display_name": "A"},
                    "columns": ["name", "total"],
                    "rows": [["阀门", 2]],
                },
                {
                    "source": {"workbook_id": "b", "display_name": "B"},
                    "columns": ["name", "total"],
                    "rows": [["阀门", 3]],
                },
            ],
            {"operation": "union"},
        )
        self.assertEqual(["workbook_id", "workbook_name", "name", "total"], result["columns"])
        self.assertEqual([["a", "A", "阀门", 2], ["b", "B", "阀门", 3]], result["rows"])
        self.assertEqual({"a", "b"}, {item["workbook_id"] for item in result["sources"]})

    def test_failed_source_is_explicitly_marked_partial(self):
        class LLM:
            def __init__(self):
                self.replies = iter([
                    'Action: query_workbook\nAction Input: {"workbook_id":"a","sql":"SELECT 1"}',
                    'Action: terminate\nAction Input: {"result":"部分结果"}',
                ])

            def generate_chat(self, _messages):
                return next(self.replies)

        class Service:
            def active_database_snapshot(self, workbook_id, owner_id=None):
                return {"workbook_id": workbook_id, "display_name": workbook_id,
                        "import_id": "i-" + workbook_id, "version": 1,
                        "database_path": __file__}

            def execute_readonly_sql_snapshot(self, snapshot, sql, max_rows=500):
                if snapshot["workbook_id"] == "a":
                    raise RuntimeError("source unavailable")
                return SQLQueryResponse(sql=sql, columns=["x"], rows=[[1]], row_count=1,
                                        truncated=False, elapsed_ms=0.1)

        events = list(run_multi_workbook_agent(
            LLM(), Service(), owner_id="owner", question="统计",
            catalogs=[{"workbook_id": "a", "display_name": "A", "catalog": {}}],
            selected_workbook_ids=["a"],
        ))
        done = next(item for item in events if item["type"] == "done")
        self.assertTrue(done["partial"])
        self.assertEqual("a", done["failed_sources"][0]["workbook_id"])

    def test_agent_queries_each_isolated_source_and_returns_provenance(self):
        class LLM:
            def __init__(self):
                self.replies = iter(
                    [
                        'Action: query_workbook\nAction Input: '
                        '{"workbook_id":"allowed","sql":"SELECT 1 AS total"}',
                        'Action: terminate\nAction Input: {"result":"共有 1 条。"}',
                    ]
                )

            def generate_chat(self, _messages):
                return next(self.replies)

        class Service:
            def active_database_snapshot(self, workbook_id, owner_id=None):
                self.owner_id = owner_id
                return {
                    "workbook_id": workbook_id,
                    "display_name": "采购目录",
                    "import_id": "import-1",
                    "version": 1,
                    "database_path": __file__,
                }

            def execute_readonly_sql_snapshot(self, _snapshot, sql, max_rows=500):
                return SQLQueryResponse(
                    sql=sql,
                    columns=["total"],
                    rows=[[1]],
                    row_count=1,
                    truncated=False,
                    elapsed_ms=0.1,
                )

        catalogs = [
            {
                "workbook_id": "allowed",
                "display_name": "采购目录",
                "active_import_id": "import-1",
                "active_version": 1,
                "catalog": {"tables": []},
            }
        ]
        events = list(
            run_multi_workbook_agent(
                LLM(),
                Service(),
                owner_id="owner",
                question="统计采购数量",
                catalogs=catalogs,
                selected_workbook_ids=["allowed"],
            )
        )
        result = next(item for item in events if item["type"] == "source.query.result")
        done = next(item for item in events if item["type"] == "done")
        self.assertEqual("allowed", result["source"]["workbook_id"])
        self.assertEqual("import-1", result["source"]["import_id"])
        self.assertEqual("allowed", done["sources"][0]["workbook_id"])

    def test_selected_scope_rejects_workbooks_outside_allowlist(self):
        service = SimpleNamespace()
        catalogs = [{"workbook_id": "allowed", "display_name": "A", "catalog": {}}]
        with self.assertRaisesRegex(ValueError, "not authorized"):
            list(
                run_multi_workbook_agent(
                    SimpleNamespace(),
                    service,
                    owner_id="owner",
                    question="统计",
                    catalogs=catalogs,
                    selected_workbook_ids=["forbidden"],
                )
            )


if __name__ == "__main__":
    unittest.main()
