import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.api.endpoints import (
    _report_pdf_emoji_font_path,
    _report_pdf_font_path,
    get_report_pdf,
)


class ReportPdfTests(unittest.TestCase):
    def test_stored_html_report_is_downloadable_as_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_dir = root / "book" / "reports"
            report_dir.mkdir(parents=True)
            (report_dir / "report_测试.html").write_text(
                "<!doctype html><html><body><h1>测试报告</h1>"
                "<table><tr><th>指标</th><th>值</th></tr>"
                "<tr><td>有效批次</td><td>98</td></tr></table>"
                "</body></html>",
                encoding="utf-8",
            )
            service = SimpleNamespace(
                config=SimpleNamespace(resolved_storage_path=lambda: directory)
            )

            response = asyncio.run(
                get_report_pdf("book", "report_测试", service, "anonymous")
            )

        self.assertEqual("application/pdf", response.media_type)
        self.assertTrue(response.body.startswith(b"%PDF"))
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertIn("report_%E6%B5%8B%E8%AF%95.pdf", response.headers["content-disposition"])

    def test_cjk_report_uses_embedded_font_when_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_dir = root / "book" / "reports"
            report_dir.mkdir(parents=True)
            report_path = report_dir / "report_中文.html"
            report_path.write_text(
                "<!doctype html><html><body><h1>📊 中文报告</h1>"
                "<p>⚠️ 完整数据与结果</p></body></html>",
                encoding="utf-8",
            )
            service = SimpleNamespace(
                config=SimpleNamespace(resolved_storage_path=lambda: directory)
            )
            if _report_pdf_font_path(service) is None:
                self.skipTest("No local CJK font is installed in this environment")
            if _report_pdf_emoji_font_path() is None:
                self.skipTest("No local emoji font is installed in this environment")

            class NotdefCounter(logging.Handler):
                def __init__(self):
                    super().__init__(logging.WARNING)
                    self.count = 0

                def emit(self, record):
                    if ".notdef glyph rendered" in record.getMessage():
                        self.count += 1

            logger = logging.getLogger("weasyprint")
            old_handlers = list(logger.handlers)
            old_level = logger.level
            old_propagate = logger.propagate
            counter = NotdefCounter()
            logger.handlers = [counter]
            logger.setLevel(logging.WARNING)
            logger.propagate = False
            try:
                response = asyncio.run(
                    get_report_pdf("book", "report_中文", service, "anonymous")
                )
            finally:
                logger.handlers = old_handlers
                logger.setLevel(old_level)
                logger.propagate = old_propagate

        self.assertTrue(response.body.startswith(b"%PDF"))
        self.assertEqual(0, counter.count)


if __name__ == "__main__":
    unittest.main()
