import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app import db
from app.agent.session import AgentConversation
from app.models.agent import DataSpaceDao
from app.models.workbook import WorkbookDatasetDao
from app.service.service import WorkbookImportService


class MultiWorkbookScopePersistenceTests(unittest.TestCase):
    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = None
        db._SessionLocal = None

    def _service(self, storage):
        db._init_engine(storage)
        db.ensure_tables()
        datasets = WorkbookDatasetDao()
        for workbook_id, owner in (("a", "owner-a"), ("b", "owner-a"), ("secret", "owner-b")):
            datasets.create_entity({
                "workbook_id": workbook_id,
                "owner_id": owner,
                "display_name": workbook_id,
                "datasource_name": "workbook_" + workbook_id,
            })
        config = SimpleNamespace(
            max_concurrent_imports=2, max_concurrent_sql=4,
            max_concurrent_agent_runs=2, query_row_limit=500,
            sql_timeout_ms=5000, sql_vm_steps=100000,
            storage_path=storage, resolved_storage_path=lambda: storage,
        )
        return WorkbookImportService(config=config)

    def test_data_space_and_relation_are_owner_scoped(self):
        with TemporaryDirectory() as storage:
            service = self._service(storage)
            space = service.create_data_space("owner-a", "采购", "", ["a", "b"])
            self.assertEqual(["a", "b"], space["workbook_ids"])
            with self.assertRaisesRegex(ValueError, "not found"):
                service.get_data_space(space["space_id"], "owner-b")
            self.assertFalse(DataSpaceDao().delete(space["space_id"], "owner-b"))
            self.assertEqual(["a", "b"], DataSpaceDao().list_workbook_ids(space["space_id"]))
            with self.assertRaisesRegex(ValueError, "Workbook not found"):
                service.create_data_space("owner-a", "非法", "", ["secret"])

            relation = service.create_dataset_relation("owner-a", {
                "left_workbook_id": "a", "left_table": "t", "left_column": "id",
                "right_workbook_id": "b", "right_table": "t", "right_column": "id",
            })
            self.assertEqual("owner-a", relation["owner_id"])
            self.assertEqual([], service.list_dataset_relations("owner-b"))

    def test_global_agent_conversation_cannot_cross_owner_boundary(self):
        with TemporaryDirectory() as storage:
            self._service(storage)
            conversation = AgentConversation.create("owner-a", "auto", {"mode": "auto"})
            conversation.start_new_round()
            conversation.add_user("统计采购")
            self.assertIsNone(AgentConversation.load(conversation.conv_id, "owner-b"))
            self.assertIsNotNone(AgentConversation.load(conversation.conv_id, "owner-a"))
            from app.models.agent import AgentConversationDao
            AgentConversationDao().delete(conversation.conv_id, "owner-b")
            restored = AgentConversation.load(conversation.conv_id, "owner-a")
            self.assertIsNotNone(restored)
            self.assertEqual("统计采购", restored.messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
