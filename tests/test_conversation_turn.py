import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.agent.react_loop import run_react
from app.api.endpoints import prepare_conversation_turn


class FakeConversation:
    def __init__(self):
        self.events = []
        self.history = [
            {"role": "user", "content": "old question"},
            {"role": "ai", "content": "old answer"},
        ]

    def get_history_messages(self):
        self.events.append("history")
        return list(self.history)

    def start_new_round(self):
        self.events.append("start")

    def add_user_message(self, question):
        self.events.append(("user", question))
        self.history.append({"role": "user", "content": question})


class RecordingLLM:
    def __init__(self):
        self.messages = None

    def generate_chat(self, messages):
        self.messages = list(messages)
        return 'Thought: done\nAction: terminate\nAction Input: {"result":"ok"}'


class FakeService:
    def __init__(self, database_path):
        self.database_path = database_path

    def active_database_path(self, _workbook_id):
        return self.database_path


class ConversationTurnTests(unittest.TestCase):
    def test_latest_persisted_budget_is_reused_by_next_turn(self):
        from app.conversation import ExcelWorkbookConversation

        conversation = ExcelWorkbookConversation(
            "conv", "book", conv_dao=object(), message_dao=object()
        )
        conversation.messages = [
            {
                "role": conversation.ROLE_BUDGET,
                "meta": {
                    "llm_calls": 3, "tool_calls": 2,
                    "sql_calls": 1, "estimated_tokens": 50,
                },
            }
        ]
        self.assertEqual(3, conversation.get_budget_usage()["llm_calls"])

    def test_history_is_snapshotted_before_current_question_is_persisted(self):
        conversation = FakeConversation()
        history = prepare_conversation_turn(conversation, "current question")
        self.assertEqual(["history", "start", ("user", "current question")], conversation.events)
        self.assertNotIn("current question", [item["content"] for item in history])

    def test_current_question_appears_exactly_once_in_model_context(self):
        with TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "book.sqlite")
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "CREATE TABLE _dbgpt_import_manifest (schema_json TEXT)"
                )
                connection.execute(
                    "INSERT INTO _dbgpt_import_manifest VALUES ('[]')"
                )
            llm = RecordingLLM()
            events = list(run_react(
                llm,
                FakeService(database_path),
                "book",
                "current question",
                history_messages=[
                    {"role": "user", "content": "old question"},
                    {"role": "ai", "content": "old answer"},
                ],
            ))
        self.assertEqual("done", events[-1]["type"])
        self.assertEqual(
            1,
            sum(
                message["content"] == "current question"
                for message in llm.messages
            ),
        )


if __name__ == "__main__":
    unittest.main()
