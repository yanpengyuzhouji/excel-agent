import unittest
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app.agent.html_sanitizer import MAX_REPORT_CHARS, sanitize_report_html
from app.agent.tools import html_interpreter


class HtmlSanitizerTests(unittest.TestCase):
    def test_report_url_allows_sanitized_inline_styles_but_no_scripts(self):
        from app.api.endpoints import get_report
        with TemporaryDirectory() as storage:
            service = SimpleNamespace(config=SimpleNamespace(resolved_storage_path=lambda: storage))
            report = html_interpreter(service, 'book', '<style>h1{color:red}</style><h1>Report</h1>')
            response = asyncio.run(get_report('book', report['report_id'], service, 'owner'))
            policy = response.headers['content-security-policy']
            self.assertIn("style-src 'unsafe-inline'", policy)
            self.assertIn("default-src 'none'", policy)
            self.assertNotIn('allow-scripts', policy)

    def test_preserves_safe_style_attributes_but_blocks_obfuscated_urls(self):
        result = sanitize_report_html('<div style="display:grid;gap:12px">Report</div>')
        self.assertIn('style="display:grid;gap:12px"', result)
        unsafe = sanitize_report_html(r'<div style="background:u\72l(https://evil.invalid)">Report</div>')
        self.assertNotIn('style=', unsafe)
        commented = sanitize_report_html('<style>/* layout */ .kpi > span {color:red}</style><div>Report</div>')
        self.assertIn('.kpi > span {color:red}', commented)
        hidden_url = sanitize_report_html('<div style="background:u/**/rl(https://evil.invalid)">Report</div>')
        self.assertNotIn('style=', hidden_url)

    def test_removes_active_and_remote_content(self):
        source = """<!DOCTYPE html><html><head>
        <style>@import url(https://evil.invalid/x.css)</style>
        <script>top.location='https://evil.invalid/'</script></head>
        <body onload="alert(1)"><iframe src="https://evil.invalid"></iframe>
        <img src=x onerror=alert(1)><a href="https://evil.invalid">external</a>
        <a href="#safe">anchor</a><table><tr><td colspan="2">42</td></tr></table>
        </body></html>"""

        result = sanitize_report_html(source)

        lowered = result.lower()
        for forbidden in (
            "script", "iframe", "img", "onload", "onerror", "https://",
            "@import",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertIn('<a href="#safe">anchor</a>', result)
        self.assertIn('<td colspan="2">42</td>', result)

    def test_preserves_safe_inline_css(self):
        source = "<style>.report { color: #123456; } .report td { padding: 8px; }</style><p class=report>Report</p>"

        result = sanitize_report_html(source)

        self.assertIn("<style>.report { color: #123456; } .report td { padding: 8px; }</style>", result)
        self.assertIn('<p class="report">Report</p>', result)

    def test_drops_style_block_with_remote_or_active_css(self):
        source = "<style>.report { background: url(https://evil.invalid/a.png); }</style><p>Report</p>"

        result = sanitize_report_html(source)

        self.assertNotIn("style", result.lower())
        self.assertEqual("<p>Report</p>", result)

    def test_escapes_text_that_looks_like_markup(self):
        self.assertEqual(
            "<p>&lt;unsafe text&gt; &amp; safe</p>",
            sanitize_report_html("<p>&lt;unsafe text&gt; &amp; safe</p>"),
        )

    def test_rejects_oversized_report(self):
        with self.assertRaisesRegex(ValueError, "safety limit"):
            sanitize_report_html("x" * (MAX_REPORT_CHARS + 1))

    def test_html_tool_persists_only_sanitized_content(self):
        with TemporaryDirectory() as storage:
            config = SimpleNamespace(resolved_storage_path=lambda: storage)
            service = SimpleNamespace(config=config)

            result = html_interpreter(
                service,
                "workbook-1",
                '<style>.report { color: red; }</style><h1 class="report" onclick="steal()">Report</h1><script>steal()</script>',
            )

            self.assertNotIn("error", result)
            persisted = Path(result["path"]).read_text(encoding="utf-8")
            self.assertEqual(result["html"], persisted)
            self.assertEqual(
                '<style>.report { color: red; }</style><h1 class="report">Report</h1>',
                persisted,
            )


if __name__ == "__main__":
    unittest.main()
