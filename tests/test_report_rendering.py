import unittest

from app.agent.evidence_publication import render_html
from app.agent.html_sanitizer import sanitize_report_html


class ReportRenderingTests(unittest.TestCase):
    def test_title_inline_lists_and_values(self):
        text = '# 投资报告\n\n## 结论\n- **总额**：12678.81142万元\n- `1*35`\n\n> 保留原始精度\n\n3. 第三项\n5. 第五项'
        result = sanitize_report_html(render_html(text, '投资报告'))
        self.assertEqual(result.count('>投资报告</h'), 1)
        for value in ('<strong>总额</strong>', '12678.81142万元', '<code>1*35</code>', '<blockquote>', '<li value="5">', '@page report', 'class="report-document"'):
            self.assertIn(value, result)

    def test_table_escapes_and_numeric_precision(self):
        result = render_html('| 规格 | 金额 |\n| --- | --- |\n| 1\\*35 | 0.1000 |\n| A\\|B | 12678.81142 |', '表')
        self.assertIn('<td>1*35</td>', result)
        self.assertIn('<td>A|B</td>', result)
        self.assertIn('<td class="number">0.1000</td>', result)
        self.assertIn('<td class="number">12678.81142</td>', result)

    def test_source_html_is_passive_even_in_code_and_bold(self):
        result = sanitize_report_html(render_html('**<script>alert(1)</script>**\n`<img src=x onerror=bad>`\n\n```\n<b>raw</b>\n```', '<unsafe>'))
        self.assertNotIn('<script>', result)
        self.assertNotIn('<img', result)
        self.assertIn('&lt;script&gt;', result)
        self.assertIn('&lt;b&gt;raw&lt;/b&gt;', result)

    def test_empty_list_and_pipe_prose_remain_text(self):
        result = render_html('- \n\n值 A|B\n下一行', '说明')
        self.assertIn('值 A|B', result)
        self.assertNotIn('<table>', result)


if __name__ == '__main__':
    unittest.main()
