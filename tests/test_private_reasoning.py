import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app import db
from app.agent.react_loop import public_answer, run_react
from app.conversation import ExcelWorkbookConversation
from app.models.conversation import WorkbookConversationDao, WorkbookMessageDao


class SequencedLLM:
    def __init__(self):
        self.replies = iter(
            [
                'Thought: SECRET_CHAIN\nAction: sql_query\n'
                'Action Input: {"sql":"SELECT 1 AS value"}',
                'Thought: SECRET_FINISH\nAction: terminate\n'
                'Action Input: {"result":"public answer"}',
            ]
        )

    def generate_chat(self, _messages):
        return next(self.replies)


class FakeService:
    def __init__(self, database_path):
        self.database_path = database_path

    def active_database_path(self, _workbook_id):
        return self.database_path

    def execute_readonly_sql(self, _workbook_id, _sql, max_rows=200):
        return SimpleNamespace(
            columns=["value"], rows=[[1]], row_count=1, truncated=False
        )


class PrivateReasoningTests(unittest.TestCase):
    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = None
        db._SessionLocal = None

    def test_sse_events_and_tool_callback_never_contain_thought(self):
        with TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "book.sqlite")
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "CREATE TABLE _dbgpt_import_manifest (schema_json TEXT)"
                )
                connection.execute(
                    "INSERT INTO _dbgpt_import_manifest VALUES ('[]')"
                )
            callbacks = []
            events = list(
                run_react(
                    SequencedLLM(),
                    FakeService(database_path),
                    "book",
                    "question",
                    on_tool_step_full=lambda action, observation, detail: callbacks.append(
                        {"action": action, "observation": observation, "detail": detail}
                    ),
                )
            )

        serialized = json.dumps(
            {"events": events, "callbacks": callbacks}, ensure_ascii=False
        )
        self.assertNotIn("SECRET_CHAIN", serialized)
        self.assertNotIn("SECRET_FINISH", serialized)
        self.assertNotIn('"thought"', serialized.lower())
        self.assertEqual("public answer", events[-1]["full_answer"])

    def test_new_and_legacy_tool_rows_do_not_expose_or_retain_thought(self):
        with TemporaryDirectory() as storage:
            db._init_engine(storage)
            db.ensure_tables()
            conversations = WorkbookConversationDao()
            messages = WorkbookMessageDao()
            conversations.create_conversation("conv", "book", owner_id="owner")
            conv = ExcelWorkbookConversation(
                "conv", "book", owner_id="owner",
                conv_dao=conversations, message_dao=messages,
            )
            conv.add_tool_message(
                "sql_query: SELECT 1",
                "one row",
                detail={"action": "sql_query", "sql": "SELECT 1"},
            )
            messages.append_message(
                "conv",
                1,
                1,
                "tool",
                "Thought: LEGACY_SECRET\nAction: sql_query\nObservation: old",
                json.dumps(
                    {
                        "thought": "LEGACY_SECRET",
                        "action": "sql_query",
                        "detail": {"thought": "LEGACY_SECRET", "sql": "SELECT 1"},
                    }
                ),
            )

            db.ensure_tables()
            loaded = ExcelWorkbookConversation.load(
                "conv", "book", owner_id="owner",
                conv_dao=conversations, message_dao=messages,
            )
            serialized = json.dumps(loaded.messages, ensure_ascii=False)
            self.assertNotIn("Thought:", serialized)
            self.assertNotIn("LEGACY_SECRET", serialized)
            with sqlite3.connect(Path(storage) / "workbook_meta.db") as connection:
                raw = "\n".join(
                    str(value)
                    for row in connection.execute(
                        "SELECT content, meta_json FROM workbook_message"
                    )
                    for value in row
                )
            self.assertNotIn("Thought:", raw)
            self.assertNotIn("LEGACY_SECRET", raw)

    def test_thought_only_reply_is_not_returned_as_public_answer(self):
        answer = public_answer("Thought: DO_NOT_RETURN_THIS")
        self.assertNotIn("DO_NOT_RETURN_THIS", answer)


if __name__ == "__main__":
    unittest.main()
