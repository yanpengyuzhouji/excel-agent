import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app.agent.budget import AgentBudget, BudgetExceeded
from app.agent.react_loop import run_react
from app.core import ResourceBusyError
from app.service.service import WorkbookImportService


class DatasetDao:
    def get_by_workbook_id(self, _workbook_id):
        return SimpleNamespace(active_import_id="import-1")


class ImportDao:
    def __init__(self, database_path):
        self.entity = SimpleNamespace(
            import_id="import-1", workbook_id="book", database_path=database_path
        )

    def get_by_import_id(self, _import_id):
        return self.entity


def service_for(database_path, **overrides):
    values = {
        "max_concurrent_imports": 1,
        "max_concurrent_sql": 1,
        "max_concurrent_agent_runs": 1,
        "query_row_limit": 10,
        "sql_timeout_ms": 5_000,
        "sql_vm_steps": 2_000_000,
    }
    values.update(overrides)
    return WorkbookImportService(
        config=SimpleNamespace(**values),
        dataset_dao=DatasetDao(),
        import_dao=ImportDao(database_path),
    )


class SqlBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database_path = str(Path(self.temp.name) / "book.sqlite")
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("CREATE TABLE numbers (value INTEGER)")
            connection.executemany(
                "INSERT INTO numbers VALUES (?)", [(1,), (2,), (3,)]
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_fast_read_succeeds_and_config_caps_returned_rows(self):
        service = service_for(self.database_path, query_row_limit=2)
        result = service.execute_readonly_sql(
            "book", "SELECT value FROM numbers ORDER BY value", max_rows=100
        )
        self.assertEqual([[1], [2]], result.rows)
        self.assertTrue(result.truncated)

    def test_recursive_query_is_interrupted_by_vm_step_budget(self):
        service = service_for(self.database_path, sql_vm_steps=1_000)
        sql = (
            "WITH RECURSIVE cnt(x) AS (VALUES(1) UNION ALL "
            "SELECT x+1 FROM cnt WHERE x < 10000000) SELECT sum(x) FROM cnt"
        )
        with self.assertRaisesRegex(ValueError, "VM step limit"):
            service.execute_readonly_sql("book", sql)

    def test_recursive_query_is_interrupted_by_wall_clock_budget(self):
        service = service_for(
            self.database_path, sql_timeout_ms=1, sql_vm_steps=100_000_000
        )
        sql = (
            "WITH RECURSIVE cnt(x) AS (VALUES(1) UNION ALL "
            "SELECT x+1 FROM cnt WHERE x < 10000000) SELECT sum(x) FROM cnt"
        )
        with self.assertRaisesRegex(ValueError, "timeout"):
            service.execute_readonly_sql("book", sql)

    def test_sql_concurrency_is_rejected_without_waiting(self):
        service = service_for(self.database_path)
        self.assertTrue(service._sql_slots.acquire(blocking=False))
        try:
            with self.assertRaises(ResourceBusyError):
                service.execute_readonly_sql("book", "SELECT 1")
        finally:
            service._sql_slots.release()


class RepeatingTodoLLM:
    def __init__(self):
        self.calls = 0

    def generate_chat(self, _messages):
        self.calls += 1
        return (
            'Thought: plan\nAction: todowrite\n'
            'Action Input: {"todos":[{"content":"x"}]}'
        )


class AgentBudgetTests(unittest.TestCase):
    def test_individual_llm_tool_sql_and_token_budgets_are_hard_limits(self):
        budget = AgentBudget(
            max_llm_calls=1, max_tool_calls=1, max_sql_calls=1,
            max_total_tokens=10, max_prompt_chars=100, max_seconds=10,
        )
        budget.before_llm([{"content": "1234"}])
        budget.after_llm("1234")
        with self.assertRaisesRegex(BudgetExceeded, "LLM-call"):
            budget.before_llm([])
        budget.before_tool("sql_query")
        with self.assertRaisesRegex(BudgetExceeded, "tool-call"):
            budget.before_tool("sql_query")
        token_budget = AgentBudget(max_total_tokens=1, max_prompt_chars=100)
        with self.assertRaisesRegex(BudgetExceeded, "token budget"):
            token_budget.before_llm([{"content": "12345678"}])
        prompt_budget = AgentBudget(max_prompt_chars=3)
        with self.assertRaisesRegex(BudgetExceeded, "prompt budget"):
            prompt_budget.before_llm([{"content": "1234"}])
        time_budget = AgentBudget(max_seconds=1)
        time_budget.started = 0
        with self.assertRaisesRegex(BudgetExceeded, "time budget"):
            time_budget.before_tool("todowrite")
        conversation_budget = AgentBudget.from_config(
            {"max_conversation_sql_calls": 1},
            prior={"sql_calls": 1},
        )
        with self.assertRaisesRegex(BudgetExceeded, "Conversation SQL-call"):
            conversation_budget.before_tool("sql_query")

    def test_react_stops_before_executing_tool_over_budget(self):
        llm = RepeatingTodoLLM()
        with TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "empty.sqlite")
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "CREATE TABLE _dbgpt_import_manifest (schema_json TEXT)"
                )
                connection.execute(
                    "INSERT INTO _dbgpt_import_manifest VALUES ('[]')"
                )
            budget = AgentBudget(
                max_llm_calls=5, max_tool_calls=1, max_sql_calls=1,
                max_total_tokens=100_000, max_prompt_chars=300_000,
            )
            events = list(run_react(
                llm, service_for(database_path), "book", "question",
                max_loops=5, budget=budget,
            ))
        self.assertEqual("error", events[-1]["type"])
        self.assertIn("tool-call budget", events[-1]["error"])
        self.assertEqual(1, budget.tool_calls)

    def test_agent_run_concurrency_is_bounded(self):
        service = service_for(":memory:")
        self.assertTrue(service.try_acquire_agent_run())
        self.assertFalse(service.try_acquire_agent_run())
        service.release_agent_run()
        self.assertTrue(service.try_acquire_agent_run())
        service.release_agent_run()


if __name__ == "__main__":
    unittest.main()
