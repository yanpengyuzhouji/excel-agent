import unittest
import json
from types import SimpleNamespace

from app.agent.multi_workbook import run_multi_workbook_agent, _aggregate_results
from app.agent.numeric_evidence import render_numeric_answer, scope_labels


class NumericEvidenceTests(unittest.TestCase):
    def evidence(self):
        return {'result_id': 'result-1', 'source': {'workbook_id': 'a', 'import_id': 'i', 'version': 1},
                'columns': ['year', 'amount'], 'rows': [[2021, '2624.8556'], [2021, '909.5102'],
                [2021, '90.8100'], [2021, '294.3455'], [2022, '130794.6680']], 'truncated': False}

    def test_exact_sum_difference_growth_and_rendered_references(self):
        raw = self.evidence()
        summed = _aggregate_results([raw], {'operation': 'sum', 'group_by': ['year'], 'value_column': 'amount'})
        summed['result_id'] = 'aggregate-1'
        self.assertEqual([[2021, '3919.5213'], [2022, '130794.6680']], summed['rows'])
        operands = [{'result_id': 'aggregate-1', 'row': i, 'column': 'amount'} for i in (0, 1)]
        difference = _aggregate_results([summed], {'operation': 'difference', 'operands': operands})
        difference['result_id'] = 'aggregate-2'
        self.assertEqual('126875.1467', difference['rows'][0][0])
        growth = _aggregate_results([summed], {'operation': 'growth_rate', 'operands': operands})
        growth['result_id'] = 'aggregate-3'
        answer, refs = render_numeric_answer('2021年总额{{aggregate-1:0:amount}}万元；2022年总额{{aggregate-1:1:amount}}万元；差额{{aggregate-2:0:difference}}万元；增长率{{aggregate-3:0:growth_rate_pct|2}}%。', [raw, summed, difference, growth])
        self.assertIn('3919.5213', answer)
        self.assertIn('3237.01%', answer)
        self.assertEqual(4, len(refs))

    def test_rejects_forged_unknown_raw_and_truncated_references(self):
        raw = self.evidence()
        for answer in ['差额{{result-99:0:amount}}',
                       '总额{{result-1:99:amount}}', '总额{{result-1:0:missing}}']:
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                render_numeric_answer(answer, [raw])
        raw['truncated'] = True
        self.assertIn('error', _aggregate_results([raw], {'operation': 'sum', 'value_column': 'amount'}))

    def test_zero_baseline_null_nonfinite_and_literal_operands(self):
        result = {'result_id': 'result-1', 'columns': ['total'], 'rows': [['0'], ['12']], 'truncated': False}
        operands = [{'result_id': 'result-1', 'row': i, 'column': 'total'} for i in (0, 1)]
        growth = _aggregate_results([result], {'operation': 'growth_rate', 'operands': operands})
        self.assertIsNone(growth['rows'][0][0])
        growth['result_id'] = 'aggregate-1'
        answer, _ = render_numeric_answer('增长率{{aggregate-1:0:growth_rate_pct}}', [growth])
        self.assertIn('未定义', answer)
        self.assertIn('error', _aggregate_results([result], {'operation': 'growth_rate', 'operands': [0, 12]}))
        for bad in (None, 'NaN', 'Infinity', True):
            result['rows'][0][0] = bad
            self.assertIn('error', _aggregate_results([result], {'operation': 'sum', 'value_column': 'total'}))

    def test_missing_requested_operations_and_constant_sql_are_allowed_in_observation_mode(self):
        raw = self.evidence()
        summed = _aggregate_results([raw], {'operation': 'sum', 'value_column': 'amount'})
        summed['result_id'] = 'aggregate-1'
        self.assertTrue(render_numeric_answer('总额{{aggregate-1:0:amount}}', [summed], question='计算总额、差额和增长率')[0])
        forged = {'result_id': 'result-1', 'sql': 'SELECT 3919.0213 AS total', 'columns': ['total'], 'rows': [['3919.0213']]}
        self.assertTrue(render_numeric_answer('总额{{result-1:0:total}}', [forged])[0])

    def test_authorized_source_labels_and_text_specifications_are_not_mental_math(self):
        raw = {'result_id': 'result-1', 'columns': ['kva', 'mm2'], 'rows': [[160, '1*35<script>']], 'truncated': False}
        total = {'result_id': 'result-2', 'columns': ['total'], 'rows': [[160]], 'sql': 'SELECT SUM(kva) AS total FROM table_1'}
        source = '3f12f75588acef3ba9da8fe5f5ce7ef17eb492a4891b88903760c133083be1be'
        html, refs = render_numeric_answer('<h1>容量合计{{result-2:0:total}}</h1><p>来源：'+source+' / Sheet1</p><table><tr><th>容量</th><th>截面 mm2</th></tr><tr><td>{{result-1:0:kva}}</td><td>{{result-1:0:mm2}}</td></tr></table>',
            [raw, total], is_html=True, labels=[source, 'Sheet1'])
        self.assertIn('1*35&lt;script&gt;', html)
        self.assertEqual(3, len(refs))
        self.assertTrue(render_numeric_answer('容量合计{{result-2:0:total}}万元，另有三百万元', [total])[0])

    def test_scope_count_labels_are_bound_to_queried_authorized_tables(self):
        catalogs = [{'workbook_id': 'a', 'catalog': {'tables': [{'table_name': 'table_'+str(i)} for i in range(1, 14)]}}]
        result = {'source': {'workbook_id': 'a'}, 'sql': ' UNION ALL '.join('SELECT amount FROM table_'+str(i) for i in range(1, 14))}
        labels = scope_labels(catalogs, [result], {'a': {'version': 1}})
        self.assertIn('13个区域', labels)
        self.assertNotIn('13', labels)
        self.assertNotIn('13条记录', labels)
        self.assertIn('v1', labels)
        result['sql'] = 'SELECT amount FROM table_1'
        self.assertNotIn('13个区域', scope_labels(catalogs, [result], {'a': {'version': 1}}))

    def test_swapped_difference_and_growth_references_are_allowed_in_observation_mode(self):
        difference = {'result_id': 'aggregate-1', 'operation': 'difference', 'columns': ['difference'], 'rows': [['100']]}
        growth = {'result_id': 'aggregate-2', 'operation': 'growth_rate', 'columns': ['growth_rate_pct'], 'rows': [['20']]}
        self.assertTrue(render_numeric_answer('差额{{aggregate-2:0:growth_rate_pct}}，增长率{{aggregate-1:0:difference}}%', [difference, growth], question='差额及增长率')[0])

    def test_null_note_rounding_and_source_spec_pattern_are_not_computed_values(self):
        result = {'result_id': 'result-1', 'columns': ['total', 'spec'], 'rows': [[160, '1*35']],
                  'sql': 'SELECT SUM(kva) AS total, spec FROM table_1'}
        answer, _ = render_numeric_answer('总额{{result-1:0:total|2}}；保留2位小数。缺失为NULL，非0；原文以1*截面形式记录。', [result])
        self.assertIn('160.00', answer)
        self.assertTrue(render_numeric_answer('总额{{result-1:0:total|2}}；原文以2*截面形式记录。', [result])[0])
        self.assertTrue(render_numeric_answer('总额{{result-1:0:total|2}}，另有0.5万元', [result])[0])
        self.assertTrue(render_numeric_answer('总额{{result-1:0:total|2}}，另有3919万元', [result], labels=['3919'])[0])

    def test_final_rewrite_numbers_do_not_trigger_a_gate(self):
        raw = self.evidence()
        replies = iter(['Action: query_workbook\nAction Input: {"sql":"SELECT year, amount FROM table_1"}',
                        'Action: terminate\nAction Input: {"result":"已查到相关数据。"}'])
        service = SimpleNamespace(active_database_snapshot=lambda *a, **k: raw['source'],
            execute_readonly_sql_snapshot=lambda *a, **k: SimpleNamespace(sql='SELECT year, amount FROM table_1', columns=raw['columns'], rows=raw['rows'], row_count=5, truncated=False))
        def rewrite(messages):
            yield '总额为3919.0213万元。' * 20
        events = list(run_multi_workbook_agent(SimpleNamespace(generate_chat=lambda m: next(replies), generate_chat_stream=rewrite),
            service, owner_id='owner', question='分析数据', catalogs=[{'workbook_id': 'a'}]))
        self.assertEqual('done', events[-1]['type'])
        self.assertTrue(any(e['type'] == 'done' for e in events))

    def test_html_visible_numbers_checked_but_css_not_treated_as_metrics(self):
        result = {'result_id': 'result-1', 'columns': ['total'], 'rows': [['3919.5213']],
                  'sql': 'SELECT SUM(amount) AS total FROM table_1', 'truncated': False}
        html, _ = render_numeric_answer('<style>h1{font-size:20px}</style><h1>2021年总额{{result-1:0:total|2}}万元</h1>', [result], is_html=True)
        self.assertIn('3919.52', html)
        self.assertTrue(render_numeric_answer('<p>总额3919.0213万元</p>', [result], is_html=True)[0])

    def test_agent_calculates_and_publishes_without_unchecked_final_rewrite(self):
        raw = self.evidence()
        def tool(action, **inputs):
            return 'Action: '+action+'\nAction Input: '+json.dumps(inputs, ensure_ascii=False)
        operands = [{'result_id': 'aggregate-1', 'row': i, 'column': 'amount'} for i in (0, 1)]
        replies = iter([tool('query_workbook', sql='SELECT year, amount FROM table_1'),
                        tool('aggregate_results', operation='sum', group_by=['year'], value_column='amount', result_ids=['result-1']),
                        tool('aggregate_results', operation='difference', operands=operands),
                        tool('aggregate_results', operation='growth_rate', operands=operands),
                        tool('terminate', result='2021年总额{{aggregate-1:0:amount}}万元；差额{{aggregate-2:0:difference}}万元；增长率{{aggregate-3:0:growth_rate_pct|2}}%。')])
        def unsafe_stream(messages):
            raise AssertionError('A validated answer must not be rewritten')
        service = SimpleNamespace(active_database_snapshot=lambda *a, **k: raw['source'],
            execute_readonly_sql_snapshot=lambda *a, **k: SimpleNamespace(sql='SELECT year, amount FROM table_1', columns=raw['columns'], rows=raw['rows'], row_count=5, truncated=False))
        events = list(run_multi_workbook_agent(SimpleNamespace(generate_chat=lambda m: next(replies), generate_chat_stream=unsafe_stream),
            service, owner_id='owner', question='汇总投资总额，计算差额和增长率', catalogs=[{'workbook_id': 'a'}], max_loops=6))
        self.assertEqual('done', events[-1]['type'])
        self.assertIn('126875.1467', events[-1]['full_answer'])
        self.assertIn('3237.01%', events[-1]['full_answer'])
        self.assertEqual(events[-1]['full_answer'], ''.join(e['text'] for e in events if e['type'] == 'answer.delta'))

    def test_c22_observation_does_not_guarantee_correct_arithmetic(self):
        replies = iter([
            'Action: query_workbook\nAction Input: {"sql":"SELECT year, amount FROM table_1"}',
            'Action: terminate\nAction Input: {"result":"2021年总投资3919.0213万元，增长率3237.43%。"}',
        ])
        def generate(messages):
            return next(replies, 'Action: terminate\nAction Input: {"result":"2021年总投资3919.0213万元，增长率3237.43%。"}')
        rows = [[2021, '2624.8556'], [2021, '909.5102'], [2021, '90.8100'], [2021, '294.3455'], [2022, '130794.6680']]
        service = SimpleNamespace(
            active_database_snapshot=lambda *a, **k: {'workbook_id': 'a', 'import_id': 'i', 'version': 1},
            execute_readonly_sql_snapshot=lambda *a, **k: SimpleNamespace(sql='SELECT year, amount FROM table_1', columns=['year', 'amount'], rows=rows, row_count=5, truncated=False))
        events = list(run_multi_workbook_agent(SimpleNamespace(generate_chat=generate), service,
            owner_id='owner', question='汇总2021年和2022年的投资总额，计算差额和增长率',
            catalogs=[{'workbook_id': 'a'}], max_loops=5))
        self.assertTrue(any(e['type'] == 'done' for e in events))
        self.assertTrue(any(e['type'] == 'numeric.observed' for e in events))


if __name__ == '__main__':
    unittest.main()
