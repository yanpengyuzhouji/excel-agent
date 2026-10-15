import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from app import db
from app.models import conversation as _conversation_models  # noqa: F401
from app.models.workbook import WorkbookDatasetDao


class AtomicVersionAllocationTests(unittest.TestCase):
    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = None
        db._SessionLocal = None

    def _initialize(self, storage: str) -> None:
        self.tearDown()
        db._init_engine(storage)
        db.ensure_tables()

    def test_concurrent_allocations_are_unique_and_monotonic(self):
        with TemporaryDirectory() as storage:
            self._initialize(storage)
            WorkbookDatasetDao().create_entity(
                {
                    "workbook_id": "book",
                    "owner_id": "owner",
                    "display_name": "Book",
                    "datasource_name": "workbook_book",
                }
            )

            with ThreadPoolExecutor(max_workers=12) as executor:
                versions = list(
                    executor.map(
                        lambda _: WorkbookDatasetDao().allocate_next_version("book"),
                        range(24),
                    )
                )

            self.assertEqual(list(range(1, 25)), sorted(versions))
            dataset = WorkbookDatasetDao().get_by_workbook_id("book")
            self.assertEqual(25, dataset.next_version)

    def test_legacy_counter_is_backfilled_from_existing_imports(self):
        with TemporaryDirectory() as storage:
            self._initialize(storage)
            datasets = WorkbookDatasetDao()
            datasets.create_entity(
                {
                    "workbook_id": "legacy",
                    "owner_id": "owner",
                    "display_name": "Legacy",
                    "datasource_name": "workbook_legacy",
                }
            )
            meta_db = Path(storage) / "workbook_meta.db"
            with sqlite3.connect(meta_db) as connection:
                connection.execute(
                    "INSERT INTO workbook_import "
                    "(import_id, workbook_id, version, source_channel, file_name, "
                    "source_hash, source_path, draft_path, status, revision) "
                    "VALUES ('old', 'legacy', 7, 'agent', 'old.csv', 'hash', "
                    "'/tmp/old.csv', '/tmp/old.json', 'active', 1)"
                )
                connection.execute(
                    "UPDATE workbook_dataset SET next_version = 1 "
                    "WHERE workbook_id = 'legacy'"
                )

            db.ensure_tables()
            self.assertEqual(8, datasets.allocate_next_version("legacy"))

    def test_unknown_workbook_cannot_allocate_a_version(self):
        with TemporaryDirectory() as storage:
            self._initialize(storage)
            with self.assertRaisesRegex(ValueError, "Workbook not found"):
                WorkbookDatasetDao().allocate_next_version("missing")


if __name__ == "__main__":
    unittest.main()
