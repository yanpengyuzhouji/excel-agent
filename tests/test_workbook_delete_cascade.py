import unittest
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app import db
from app.models.conversation import (
    WorkbookConversationDao,
    WorkbookConversationEntity,
    WorkbookMessageDao,
    WorkbookMessageEntity,
)
from app.models.workbook import (
    WorkbookDatasetDao,
    WorkbookDatasetEntity,
    WorkbookImportDao,
    WorkbookImportEntity,
)
from app.service.service import WorkbookImportService


class WorkbookDeleteCascadeTests(unittest.TestCase):
    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = None
        db._SessionLocal = None

    def test_delete_removes_imports_conversations_messages_and_files(self):
        with TemporaryDirectory() as storage:
            db._init_engine(storage)
            db.ensure_tables()
            datasets = WorkbookDatasetDao()
            imports = WorkbookImportDao()
            datasets.create_entity(
                {
                    "workbook_id": "book",
                    "owner_id": "owner",
                    "display_name": "Book",
                    "datasource_name": "workbook_book",
                }
            )
            workbook_dir = Path(storage) / "book"
            version_dir = workbook_dir / "v1"
            version_dir.mkdir(parents=True)
            draft_path = version_dir / "draft.json"
            draft_path.write_text("{}", encoding="utf-8")
            imports.create_entity(
                {
                    "import_id": "import-1",
                    "workbook_id": "book",
                    "version": 1,
                    "source_channel": "agent",
                    "file_name": "source.csv",
                    "source_hash": "hash",
                    "source_path": str(version_dir / "source.csv"),
                    "draft_path": str(draft_path),
                    "status": "ready",
                    "revision": 1,
                }
            )
            conversations = WorkbookConversationDao()
            messages = WorkbookMessageDao()
            conversations.create_conversation(
                "conv-1", "book", owner_id="owner"
            )
            messages.append_message("conv-1", 0, 1, "user", "question")
            (workbook_dir / "reports").mkdir()
            (workbook_dir / "reports" / "report.html").write_text(
                "report", encoding="utf-8"
            )

            service = WorkbookImportService(
                config=SimpleNamespace(resolved_storage_path=lambda: storage),
                dataset_dao=datasets,
                import_dao=imports,
            )
            service.delete_workbook("book")

            with imports.session() as session:
                self.assertEqual(0, session.query(WorkbookDatasetEntity).count())
                self.assertEqual(0, session.query(WorkbookImportEntity).count())
                self.assertEqual(0, session.query(WorkbookConversationEntity).count())
                self.assertEqual(0, session.query(WorkbookMessageEntity).count())
            self.assertFalse(workbook_dir.exists())

    def test_active_database_path_recovers_metadata_created_on_another_os(self):
        with TemporaryDirectory() as storage:
            db._init_engine(storage)
            db.ensure_tables()
            datasets = WorkbookDatasetDao()
            imports = WorkbookImportDao()
            datasets.create_entity(
                {
                    "workbook_id": "book",
                    "owner_id": "owner",
                    "display_name": "Book",
                    "datasource_name": "workbook_book",
                    "active_version": 1,
                    "active_import_id": "import-1",
                }
            )
            version_dir = Path(storage) / "book" / "v1"
            version_dir.mkdir(parents=True)
            database_path = version_dir / "database.sqlite"
            with sqlite3.connect(database_path) as connection:
                connection.execute("CREATE TABLE records (value INTEGER)")
                connection.execute("INSERT INTO records VALUES (7)")
            imports.create_entity(
                {
                    "import_id": "import-1",
                    "workbook_id": "book",
                    "version": 1,
                    "source_channel": "agent",
                    "file_name": "source.xlsx",
                    "source_hash": "hash",
                    "source_path": r"D:\\legacy\\book\\source.xlsx",
                    "draft_path": r"D:\\legacy\\book\\draft.json",
                    "database_path": r"D:\\legacy\\book\\database.sqlite",
                    "status": "active",
                    "revision": 1,
                }
            )
            service = WorkbookImportService(
                config=SimpleNamespace(
                    resolved_storage_path=lambda: storage,
                    query_row_limit=10,
                    sql_timeout_ms=5000,
                    sql_vm_steps=100000,
                ),
                dataset_dao=datasets,
                import_dao=imports,
            )

            self.assertEqual(str(database_path), service.active_database_path("book"))
            result = service.execute_readonly_sql("book", "SELECT value FROM records")
            self.assertEqual([[7]], result.rows)


if __name__ == "__main__":
    unittest.main()
