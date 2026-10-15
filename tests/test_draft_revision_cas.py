import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app import db
from app.api.schemas import DraftUpdateRequest
from app.core import RevisionConflictError
from app.models.conversation import WorkbookConversationEntity  # noqa: F401
from app.models.workbook import WorkbookDatasetDao, WorkbookImportDao
from app.service.parser import dump_draft, load_draft, parse_workbook
from app.service.service import WorkbookImportService


class DraftRevisionCasTests(unittest.TestCase):
    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = None
        db._SessionLocal = None

    def _fixture(self, storage: str):
        source = Path(storage) / "source.csv"
        source.write_text("name,amount\nAlpha,10\n", encoding="utf-8")
        draft_path = Path(storage) / "draft.json"
        draft = parse_workbook(str(source))
        dump_draft(draft, str(draft_path))
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
        imports.create_entity(
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
        sheet = draft["sheets"][0]
        return service, imports, draft_path, sheet["sheet_id"], sheet["columns"][0]["column_id"]

    def test_same_revision_has_exactly_one_winner(self):
        with TemporaryDirectory() as storage:
            service, imports, draft_path, sheet_id, column_id = self._fixture(storage)

            def update(value):
                try:
                    response = service.update_draft(
                        "import-1",
                        DraftUpdateRequest(
                            revision=1,
                            cell_updates=[
                                {
                                    "sheet_id": sheet_id,
                                    "source_row": 2,
                                    "column_id": column_id,
                                    "value": value,
                                }
                            ],
                        ),
                    )
                    return ("ok", response.revision)
                except RevisionConflictError:
                    return ("conflict", None)

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(update, ["Winner A", "Winner B"]))

            self.assertEqual(1, sum(item[0] == "ok" for item in outcomes))
            self.assertEqual(1, sum(item[0] == "conflict" for item in outcomes))
            entity = imports.get_by_import_id("import-1")
            self.assertEqual(2, entity.revision)
            self.assertNotEqual("updating", entity.status)
            value = load_draft(str(draft_path))["sheets"][0]["rows"][0]["values"][0]
            self.assertIn(value, {"Winner A", "Winner B"})

    def test_failed_update_releases_claim_and_preserves_file(self):
        with TemporaryDirectory() as storage:
            service, imports, draft_path, sheet_id, _ = self._fixture(storage)
            original = draft_path.read_bytes()

            with self.assertRaisesRegex(ValueError, "Unknown column"):
                service.update_draft(
                    "import-1",
                    DraftUpdateRequest(
                        revision=1,
                        cell_updates=[
                            {
                                "sheet_id": sheet_id,
                                "source_row": 2,
                                "column_id": "missing",
                                "value": "bad",
                            }
                        ],
                    ),
                )

            entity = imports.get_by_import_id("import-1")
            self.assertEqual(1, entity.revision)
            self.assertEqual("ready", entity.status)
            self.assertEqual(original, draft_path.read_bytes())
            self.assertEqual([], list(Path(storage).glob(".*.pending")))
            self.assertEqual([], list(Path(storage).glob(".*.backup")))


if __name__ == "__main__":
    unittest.main()
