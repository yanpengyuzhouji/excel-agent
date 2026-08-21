import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app import db
from app.conversation import ExcelWorkbookConversation
from app.models.conversation import WorkbookConversationDao
from app.models.workbook import WorkbookDatasetDao, WorkbookImportDao
from app.service.service import WorkbookImportService


class OwnerIsolationTests(unittest.TestCase):
    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = None
        db._SessionLocal = None

    def _initialize(self, storage):
        self.tearDown()
        db._init_engine(storage)
        db.ensure_tables()

    def test_legacy_tables_receive_default_owner_migration(self):
        with TemporaryDirectory() as storage:
            meta_db = Path(storage) / "workbook_meta.db"
            with sqlite3.connect(meta_db) as connection:
                connection.execute(
                    "CREATE TABLE workbook_dataset (id INTEGER PRIMARY KEY)"
                )
                connection.execute(
                    "CREATE TABLE workbook_conversation (id INTEGER PRIMARY KEY)"
                )

            self._initialize(storage)

            with sqlite3.connect(meta_db) as connection:
                for table in ("workbook_dataset", "workbook_conversation"):
                    columns = {
                        row[1]: row for row in connection.execute(
                            f"PRAGMA table_info({table})"
                        )
                    }
                    self.assertIn("owner_id", columns)
                    self.assertEqual("'default'", columns["owner_id"][4])

    def test_workbooks_imports_and_conversations_are_owner_scoped(self):
        with TemporaryDirectory() as storage:
            self._initialize(storage)
            datasets = WorkbookDatasetDao()
            imports = WorkbookImportDao()
            datasets.create_entity(
                {
                    "workbook_id": "alice-book",
                    "owner_id": "alice",
                    "display_name": "Alice",
                    "datasource_name": "workbook_alice",
                }
            )
            datasets.create_entity(
                {
                    "workbook_id": "bob-book",
                    "owner_id": "bob",
                    "display_name": "Bob",
                    "datasource_name": "workbook_bob",
                }
            )
            for owner, workbook in (("alice", "alice-book"), ("bob", "bob-book")):
                imports.create_entity(
                    {
                        "import_id": f"{owner}-import",
                        "workbook_id": workbook,
                        "version": 1,
                        "source_channel": "agent",
                        "file_name": f"{owner}.csv",
                        "source_hash": owner,
                        "source_path": str(Path(storage) / f"{owner}.csv"),
                        "draft_path": str(Path(storage) / f"{owner}.json"),
                        "status": "ready",
                        "revision": 1,
                    }
                )

            service = WorkbookImportService(
                config=SimpleNamespace(resolved_storage_path=lambda: storage),
                dataset_dao=datasets,
                import_dao=imports,
            )
            service.assert_workbook_owner("alice-book", "alice")
            service.assert_import_owner("alice-import", "alice")
            with self.assertRaisesRegex(ValueError, "not found"):
                service.assert_workbook_owner("alice-book", "bob")
            with self.assertRaisesRegex(ValueError, "not found"):
                service.assert_import_owner("alice-import", "bob")
            self.assertEqual(
                ["alice-import"],
                [row.import_id for row in imports.list_imports(owner_id="alice")],
            )

            conversations = WorkbookConversationDao()
            conversations.create_conversation(
                "alice-conv", "alice-book", owner_id="alice"
            )
            self.assertIsNotNone(
                conversations.get_by_conv_id(
                    "alice-conv", workbook_id="alice-book", owner_id="alice"
                )
            )
            self.assertIsNone(
                conversations.get_by_conv_id(
                    "alice-conv", workbook_id="bob-book", owner_id="alice"
                )
            )
            self.assertIsNone(
                conversations.get_by_conv_id(
                    "alice-conv", workbook_id="alice-book", owner_id="bob"
                )
            )
            self.assertIsNone(
                ExcelWorkbookConversation.load(
                    "alice-conv", "bob-book", owner_id="alice"
                )
            )
            self.assertIsNone(
                ExcelWorkbookConversation.load(
                    "alice-conv", "alice-book", owner_id="bob"
                )
            )


if __name__ == "__main__":
    unittest.main()
