import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app import db
from app.api.schemas import DraftUpdateRequest
from app.models.conversation import WorkbookConversationEntity  # register metadata tables
from app.models.workbook import WorkbookDatasetDao, WorkbookImportDao
from app.service.materializer import build_database
from app.service.parser import dump_draft, load_draft, parse_workbook
from app.service.service import WorkbookImportService


class HeaderReparseTests(unittest.TestCase):
    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = None
        db._SessionLocal = None

    def test_header_adjustment_rebuilds_and_materializes_new_rows(self):
        with TemporaryDirectory() as storage:
            source = Path(storage) / "source.csv"
            source.write_text(
                "wrong,title\nname,amount\nAlpha,10\nBeta,20\n",
                encoding="utf-8",
            )
            draft_path = Path(storage) / "draft.json"
            initial = parse_workbook(
                str(source),
                sheet_overrides={
                    "sheet_1": {"header_row": 1, "data_start_row": 2}
                },
            )
            dump_draft(initial, str(draft_path))

            db._init_engine(storage)
            db.ensure_tables()
            datasets = WorkbookDatasetDao()
            imports = WorkbookImportDao()
            datasets.create_entity(
                {
                    "workbook_id": "book",
                    "owner_id": "alice",
                    "display_name": "Book",
                    "datasource_name": "workbook_book",
                }
            )
            entity = imports.create_entity(
                {
                    "import_id": "import-1",
                    "workbook_id": "book",
                    "version": 1,
                    "source_channel": "agent",
                    "file_name": "source.csv",
                    "source_hash": "hash",
                    "source_path": str(source),
                    "draft_path": str(draft_path),
                    "status": "ready",
                    "revision": 1,
                }
            )
            service = WorkbookImportService(
                config=SimpleNamespace(
                    max_import_rows=100,
                    resolved_storage_path=lambda: storage,
                ),
                dataset_dao=datasets,
                import_dao=imports,
            )

            service.update_draft(
                "import-1",
                DraftUpdateRequest(
                    revision=1,
                    sheet_updates=[
                        {
                            "sheet_id": "sheet_1",
                            "header_row": 2,
                            "data_start_row": 3,
                        }
                    ],
                ),
            )

            updated = load_draft(str(draft_path))
            sheet = updated["sheets"][0]
            self.assertEqual(["name", "amount"], [c["display_name"] for c in sheet["columns"]])
            self.assertEqual(["TEXT", "INTEGER"], [c["sql_type"] for c in sheet["columns"]])
            self.assertEqual([3, 4], [row["source_row"] for row in sheet["rows"]])
            self.assertEqual([["Alpha", "10"], ["Beta", "20"]], [row["values"] for row in sheet["rows"]])

            database_path = Path(storage) / "materialized.sqlite"
            build_database(
                updated,
                str(database_path),
                import_id=entity.import_id,
                workbook_id=entity.workbook_id,
                version=entity.version,
                file_name=entity.file_name,
                source_hash=entity.source_hash,
            )
            with sqlite3.connect(database_path) as connection:
                rows = connection.execute(
                    'SELECT name, amount, _source_row FROM "data" ORDER BY _source_row'
                ).fetchall()
            self.assertEqual([("Alpha", 10, 3), ("Beta", 20, 4)], rows)


if __name__ == "__main__":
    unittest.main()
