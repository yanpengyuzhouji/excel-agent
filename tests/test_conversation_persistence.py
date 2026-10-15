import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app import db
from app.conversation import ExcelWorkbookConversation
from app.models.conversation import WorkbookConversationDao, WorkbookMessageDao


class ConversationPersistenceTests(unittest.TestCase):
    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = None
        db._SessionLocal = None

    def test_report_view_and_messages_survive_reload(self):
        with TemporaryDirectory() as storage:
            db._init_engine(storage)
            db.ensure_tables()
            conversations = WorkbookConversationDao()
            messages = WorkbookMessageDao()
            conversations.create_conversation(
                "conv", "book", title="报告会话", owner_id="owner"
            )
            conv = ExcelWorkbookConversation(
                "conv", "book", owner_id="owner",
                conv_dao=conversations, message_dao=messages,
            )
            conv.set_model_context(
                model_name="Qwen3.8-27B-ModelScope",
                model_label="Qwen3.8-27B (ModelScope)",
                model_id="Qwen/Qwen3.8-27B",
                analysis_route="large",
                analysis_reason="explicit",
                model_size_b=27,
            )
            conv.start_new_round()
            conv.add_user_message(
                "生成报告",
                meta={
                    "model_name": "Qwen3.8-27B-ModelScope",
                    "analysis_route": "large",
                },
            )
            conv.add_tool_message(
                "html_interpreter: 报告",
                "报告已生成",
                detail={
                    "action": "html_interpreter",
                    "html": {
                        "report_id": "report-1",
                        "title": "分析报告",
                        "url": "/api/v1/workbooks/book/reports/report-1",
                    },
                },
            )
            conv.add_view_message(json.dumps({
                "type": "html",
                "report_id": "report-1",
                "title": "分析报告",
                "url": "/api/v1/workbooks/book/reports/report-1",
            }, ensure_ascii=False))
            conv.add_ai_message("已生成报告")

            restored = ExcelWorkbookConversation.load(
                "conv", "book", owner_id="owner",
                conv_dao=conversations, message_dao=messages,
            )
            self.assertIsNotNone(restored)
            self.assertEqual("Qwen3.8-27B-ModelScope", restored.model_name)
            self.assertEqual("Qwen/Qwen3.8-27B", restored.model_id)
            self.assertEqual("large", restored.analysis_route)
            self.assertEqual(
                ["user", "tool", "view", "ai"],
                [message["role"] for message in restored.messages],
            )
            self.assertEqual(
                "Qwen3.8-27B-ModelScope",
                restored.messages[0]["meta"]["model_name"],
            )
            self.assertEqual(
                "report-1",
                json.loads(restored.messages[2]["content"])["report_id"],
            )
            history = restored.get_history_messages()
            self.assertEqual(
                ["user", "tool", "ai"],
                [message["role"] for message in history],
            )
            restored.delete()
            self.assertIsNone(
                ExcelWorkbookConversation.load(
                    "conv", "book", owner_id="owner",
                    conv_dao=conversations, message_dao=messages,
                )
            )


if __name__ == "__main__":
    unittest.main()
