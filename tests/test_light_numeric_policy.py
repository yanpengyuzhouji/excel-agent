import unittest
from types import SimpleNamespace

from app.agent.multi_workbook import run_multi_workbook_agent
from app.agent.numeric_evidence import render_numeric_answer


class LightNumericPolicyTests(unittest.TestCase):
    def test_original_numeric_strings_keep_leading_zeros(self):
        evidence = [{'result_id': 'result-1', 'sql': 'SELECT id FROM table_1',
                     'columns': ['id'], 'rows': [['00001']]}]
        answer, refs = render_numeric_answer('编号{{result-1:0:id}}，版本v1.2.3。', evidence)
        self.assertEqual('编号00001，版本v1.2.3。', answer)
        self.assertEqual('00001', refs[0]['rendered'])

    def test_plain_numbers_quotes_and_missing_data_are_not_blocked(self):
        for text in ('备注：合计3类设备，日期2025/03/01。', '不能记为0%增长率。',
                     '前两条样本100.10元、200.20元。', '没有记录，无法计算总额。'):
            with self.subTest(text=text):
                self.assertEqual((text, []), render_numeric_answer(text, [], question='读取原文'))

    def test_raw_and_cte_values_do_not_require_reaggregation(self):
        for sql in ('SELECT avg_metric AS value FROM table_1',
                    'WITH a AS (SELECT SUM(amount) AS value FROM table_1) SELECT value FROM a'):
            evidence = [{'result_id': 'result-1', 'sql': sql, 'columns': ['value'], 'rows': [[7.5]]}]
            answer, _ = render_numeric_answer('原始平均值{{result-1:0:value}}', evidence)
            self.assertEqual('原始平均值7.5', answer)

    def test_only_explicit_invalid_references_block(self):
        evidence = [{'result_id': 'result-1', 'columns': ['id'], 'rows': [['00001']]}]
        for text in ('{{result-99:0:id}}', '{{result-1:9:id}}', '{{result-1:0:missing}}',
                     '{{result-1:0:id', '{{result-1:0:id|0}}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                render_numeric_answer(text, evidence)

    def test_agent_accepts_original_remark_without_calculation(self):
        llm = SimpleNamespace(generate_chat=lambda messages:
            'Action: terminate\nAction Input: {"result":"备注：合计3类设备；规格1*35 mm2；2025/03/01。"}')
        service = SimpleNamespace(active_database_snapshot=lambda *a, **k:
            {'workbook_id': 'a', 'import_id': 'i', 'version': 1})
        events = list(run_multi_workbook_agent(llm, service, owner_id='owner',
            question='原样引用工作表备注，不做汇总计算', catalogs=[{'workbook_id': 'a'}], max_loops=2))
        self.assertEqual('done', events[-1]['type'])
        self.assertIn('合计3类设备', events[-1]['full_answer'])
        self.assertFalse(any(e.get('status') == 'numeric_review' for e in events))
        self.assertTrue(any(e['type'] == 'numeric.observed' for e in events))

    def test_final_rewrite_accepts_ordinary_numbers(self):
        replies = iter(['Action: query_workbook\nAction Input: {"sql":"SELECT remark FROM table_1"}',
                        'Action: terminate\nAction Input: {"result":"原文已找到。"}'])
        service = SimpleNamespace(active_database_snapshot=lambda *a, **k:
            {'workbook_id': 'a', 'import_id': 'i', 'version': 1},
            execute_readonly_sql_snapshot=lambda *a, **k: SimpleNamespace(
                sql='SELECT remark FROM table_1', columns=['remark'], rows=[['合计2025元']], row_count=1, truncated=False))
        llm = SimpleNamespace(generate_chat=lambda m: next(replies),
                              generate_chat_stream=lambda m: iter(['id为00001的备注原文：', '合计2025元，版本v1.2.3。']))
        events = list(run_multi_workbook_agent(llm, service, owner_id='owner',
            question='原样引用备注', catalogs=[{'workbook_id': 'a'}]))
        self.assertEqual('done', events[-1]['type'])
        self.assertIn('00001', events[-1]['full_answer'])
        self.assertIn('合计2025元', events[-1]['full_answer'])

    def test_final_rewrite_invalid_reference_fails_without_placeholder_leak(self):
        llm = SimpleNamespace(
            generate_chat=lambda m: 'Action: terminate\nAction Input: {"result":"已完成。"}',
            generate_chat_stream=lambda m: iter(['结果{{result-99:0:value}}']))
        service = SimpleNamespace(active_database_snapshot=lambda *a, **k:
            {'workbook_id': 'a', 'import_id': 'i', 'version': 1})
        events = list(run_multi_workbook_agent(llm, service, owner_id='owner',
            question='读取数据', catalogs=[{'workbook_id': 'a'}]))
        self.assertEqual('result_reference_invalid', events[-1]['error_code'])
        self.assertFalse(any(e['type'] in {'done', 'answer.delta'} for e in events))


if __name__ == '__main__':
    unittest.main()
