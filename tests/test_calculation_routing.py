import json
import unittest
from types import SimpleNamespace

from app.agent.multi_workbook import _aggregate_results, run_multi_workbook_agent
from app.agent.numeric_evidence import calculate_comparison, render_numeric_answer


class CalculationRoutingTests(unittest.TestCase):
    def test_named_operands_and_half_up(self):
        result = {'result_id': 'result-1', 'columns': ['baseline', 'current'], 'rows': [[1.0, 1.005]]}
        calculated = calculate_comparison([result], {'operation': 'difference',
            'baseline': {'result_id': 'result-1', 'row': 0, 'column': 'baseline'},
            'current': {'result_id': 'result-1', 'row': 0, 'column': 'current'}})
        calculated['result_id'] = 'aggregate-1'
        self.assertEqual('0.01', render_numeric_answer('{{aggregate-1:0:difference|2}}', [calculated])[0])

    def test_raw_reading_does_not_require_a_calculation(self):
        from app.agent.numeric_evidence import calculation_requirement
        self.assertIsNone(calculation_requirement('只读取avg_metric原始平均值，不要重新求平均'))
        self.assertEqual('difference', calculation_requirement('精确计算差额，ROUND_HALF_UP保留2位'))

    def test_scenario_grader_rejects_number_in_wrong_row_and_wrong_sign(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        from numeric_case_checks import scenario_check
        self.assertFalse(scenario_check('C06', '|场景|舍入2位|\n|大额精度|0.01|\n|四舍五入|0.00|')['passed'])
        self.assertFalse(scenario_check('A08', '基期0元，本期50元。差额为-50元。')['passed'])
        self.assertTrue(scenario_check('C06', '|场景|舍入2位|\n|四舍五入|0.01|')['passed'])

    def test_weighted_average_is_decimal_and_preserves_components(self):
        result = {'result_id': 'result-1', 'columns': ['price', 'qty'],
                  'rows': [['0.1', 1], ['0.2', 1]], 'truncated': False}
        calculated = _aggregate_results([result], {'operation': 'weighted_average',
            'value_column': 'price', 'weight_column': 'qty'})
        self.assertNotIn('error', calculated)
        self.assertEqual([['0.3', '2', '0.15']], calculated['rows'])
        result['truncated'] = True
        self.assertIn('error', _aggregate_results([result], {'operation': 'weighted_average',
            'value_column': 'price', 'weight_column': 'qty'}))

    def test_precise_difference_cannot_finish_after_real_sql_only(self):
        replies = iter([
            'Action: query_workbook\nAction Input: {"sql":"SELECT baseline,current,current-baseline AS diff FROM table_1"}',
            'Action: terminate\nAction Input: {"result":"差额0.00"}',
            'Action: aggregate_results\nAction Input: '+json.dumps({'operation': 'difference',
                'baseline': {'result_id': 'result-1', 'row': 0, 'column': 'baseline'},
                'current': {'result_id': 'result-1', 'row': 0, 'column': 'current'}}),
            'Action: terminate\nAction Input: {"result":"差额{{aggregate-1:0:difference|2}}"}',
        ])
        service = SimpleNamespace(active_database_snapshot=lambda *a, **k: {'workbook_id': 'a', 'import_id': 'i', 'version': 1},
            execute_readonly_sql_snapshot=lambda *a, **k: SimpleNamespace(
                sql='SELECT baseline,current,current-baseline AS diff FROM table_1',
                columns=['baseline', 'current', 'diff'], rows=[[1.0, 1.005, 0.004999999999999893]], row_count=1, truncated=False))
        events = list(run_multi_workbook_agent(SimpleNamespace(generate_chat=lambda m: next(replies)), service,
            owner_id='owner', question='精确计算本期减基期差额，ROUND_HALF_UP保留2位小数', catalogs=[{'workbook_id': 'a'}]))
        self.assertEqual('done', events[-1]['type'])
        self.assertEqual('差额0.01', events[-1]['full_answer'])
        self.assertTrue(any(e.get('status') == 'calculation_review' for e in events))

    def test_report_placeholder_resolved_without_another_model_call(self):
        from app.agent.multi_workbook import stream_final_answer
        events = stream_final_answer(SimpleNamespace(), [], '报告', '已生成：{{report_url}}',
            {'url': '/api/v1/agent/conversations/c/reports/r'}, set(), None, None, verified_numeric=True)
        text = ''.join(e.get('text', '') for e in events)
        self.assertEqual('已生成：/api/v1/agent/conversations/c/reports/r', text)
