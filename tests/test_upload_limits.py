import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

from app.core import ResourceBusyError, http_error
from app.service.parser import parse_workbook
from app.service.service import WorkbookImportService
from app.service.upload_stream import spool_upload


class FakeUpload:
    def __init__(self, parts):
        self.parts = list(parts)

    async def read(self, _size):
        return self.parts.pop(0) if self.parts else b""


class UploadStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_is_spooled_and_hashed_incrementally(self):
        with TemporaryDirectory() as directory:
            path, size, digest = await spool_upload(
                FakeUpload([b"abc", b"def"]), directory, max_bytes=6, chunk_size=3
            )
            self.assertEqual(6, size)
            self.assertEqual(hashlib.sha256(b"abcdef").hexdigest(), digest)
            self.assertEqual(b"abcdef", Path(path).read_bytes())

    async def test_oversized_upload_removes_partial_file(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "import limit"):
                await spool_upload(
                    FakeUpload([b"abcd", b"efgh"]), directory,
                    max_bytes=7, chunk_size=4,
                )
            self.assertEqual([], list(Path(directory).iterdir()))


class ParserGuardrailTests(unittest.TestCase):
    def test_csv_row_column_and_cell_limits_are_enforced_while_reading(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "data.csv"
            source.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "column limit"):
                parse_workbook(str(source), max_columns=2)
            with self.assertRaisesRegex(ValueError, "row limit"):
                parse_workbook(str(source), max_rows=1)
            with self.assertRaisesRegex(ValueError, "total cell limit"):
                parse_workbook(str(source), max_cells=5)
            source.write_text("a,b\n" + "x" * 20 + ",1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cell exceeds"):
                parse_workbook(str(source), max_cell_chars=10)

    def test_zip_expanded_size_and_compression_ratio_are_checked_first(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "bomb.xlsx"
            with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
                archive.writestr("xl/workbook.xml", b"A" * (2 * 1024 * 1024))
            with self.assertRaisesRegex(ValueError, "uncompressed size"):
                parse_workbook(str(source), max_uncompressed_bytes=1024)
            with self.assertRaisesRegex(ValueError, "compression ratio"):
                parse_workbook(
                    str(source),
                    max_uncompressed_bytes=4 * 1024 * 1024,
                    max_xml_entry_bytes=4 * 1024 * 1024,
                    max_zip_ratio=10,
                )

    def test_large_merged_range_is_rejected_before_expansion(self):
        from openpyxl import Workbook

        with TemporaryDirectory() as directory:
            source = Path(directory) / "merged.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet["A1"] = "value"
            sheet.merge_cells("A1:Z100")
            workbook.save(source)
            workbook.close()
            with self.assertRaisesRegex(ValueError, "merged-cell expansion"):
                parse_workbook(
                    str(source), max_columns=30, max_rows=200,
                    max_cells=5000, max_merged_cells=100,
                )


class ImportConcurrencyTests(unittest.TestCase):
    def test_excess_import_is_rejected_with_retryable_http_status(self):
        config = SimpleNamespace(max_concurrent_imports=1, max_import_bytes=100)
        service = WorkbookImportService(
            config=config, dataset_dao=SimpleNamespace(), import_dao=SimpleNamespace()
        )
        self.assertTrue(service._import_slots.acquire(blocking=False))
        try:
            with self.assertRaises(ResourceBusyError) as raised:
                service.create_import("data.csv", b"a,b\n", "agent")
        finally:
            service._import_slots.release()
        self.assertEqual(429, http_error(raised.exception).status_code)

    def test_service_copies_staged_source_without_materializing_bytes(self):
        with TemporaryDirectory() as directory:
            staged = Path(directory) / "staged.csv"
            staged.write_bytes(b"a,b\n1,2\n")
            config = SimpleNamespace(
                max_concurrent_imports=1, max_import_bytes=100,
            )
            service = WorkbookImportService(
                config=config,
                dataset_dao=SimpleNamespace(),
                import_dao=SimpleNamespace(),
            )

            def locked(**values):
                destination = Path(directory) / "copied.csv"
                values["write_source"](destination)
                return destination.read_bytes()

            service._create_import_locked = locked
            result = service.create_import_from_path(
                "data.csv", str(staged), "agent",
                file_size=staged.stat().st_size,
            )
            self.assertEqual(staged.read_bytes(), result)


if __name__ == "__main__":
    unittest.main()
