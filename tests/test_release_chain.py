import unittest
import json
from types import SimpleNamespace as N
from app.agent.multi_workbook import _aggregate_results, stream_final_answer


class ReleaseChainTests(unittest.TestCase):
    def test_inspection_unknown_columns_exposes_real_schema(self):
        from app.agent.multi_workbook import _inspect_source
        service = N(execute_readonly_sql_snapshot=lambda *a, **k: N(columns=['column_h'], rows=[[12]]))
        snapshot = {'workbook_id': 'a', 'import_id': 'i', 'version': 1}
        catalog = {'catalog': {'tables': [{'table_name': 'table_1', 'columns': [
            {'physical_name': 'column_h', 'display_name': '投资估算（万元）', 'sql_type': 'REAL'}]}]}}
        result = _inspect_source(service, snapshot, catalog, table_names=['table_1'], columns=['h_value'])
        self.assertEqual(['column_h'], [c['physical_name'] for c in result['tables'][0]['columns']])
        self.assertIn('h_value', result['tables'][0]['unknown_requested_columns'])
    def test_completed_calculation_without_model_placeholder_can_publish(self):
        from app.agent.multi_workbook import run_multi_workbook_agent
        actions = [
            ('query_workbook', {'sql': 'SELECT price, qty FROM table_1'}),
            ('aggregate_results', {'operation': 'weighted_average', 'result_ids': ['result-1'],
                'value_column': 'price', 'weight_column': 'qty'}),
            ('terminate', {'result': '加权平均值为15。'})]
        replies = iter(json.dumps({'action': a, 'action_input': v}, ensure_ascii=False) for a, v in actions)
        llm = N(publish_draft=True, generate_chat=lambda m: next(replies))
        service = N(active_database_snapshot=lambda *a, **k: {'workbook_id': 'a', 'import_id': 'i', 'version': 1},
            execute_readonly_sql_snapshot=lambda *a, **k: N(sql='SELECT price, qty FROM table_1',
                columns=['price', 'qty'], rows=[[10, 1], [20, 1]], row_count=2, truncated=False))
        events = list(run_multi_workbook_agent(llm, service, owner_id='owner', question='按qty加权计算price的平均值',
            catalogs=[{'workbook_id': 'a'}]))
        self.assertEqual('done', events[-1]['type'])
        self.assertIn('15', events[-1]['full_answer'])
        self.assertTrue(any(e.get('type') == 'numeric.validated' for e in events))
    def test_key_match_preserves_left_duplicates_and_exact_keys(self):
        left = {'result_id': 'result-1', 'columns': ['model'], 'rows': [['a'], ['a'], ['b'], [None], ['A']]}
        right = {'result_id': 'result-2', 'columns': ['model'], 'rows': [['a'], ['a']]}
        result = _aggregate_results([left, right], {'operation': 'match_keys', 'left_result_id': 'result-1',
            'right_result_id': 'result-2', 'left_key': 'model', 'right_key': 'model'})
        self.assertNotIn('error', result)
        self.assertEqual([[5, 2, 3, 2, 1]], result['rows'])
        self.assertEqual(['b', 'A'], result['unmatched_keys'])
        self.assertIn('error', _aggregate_results([dict(left, truncated=True), right], {'operation': 'match_keys',
            'left_result_id': 'result-1', 'right_result_id': 'result-2', 'left_key': 'model', 'right_key': 'model'}))

    def test_tool_contract_corrects_denominator_and_column_unit(self):
        from app.agent.numeric_evidence import publication_answer
        raw = {'result_id': 'result-1', 'columns': ['total', 'total_metric'], 'rows': [[550, 123.45], [450, None]]}
        growth = dict(_aggregate_results([raw], {'operation': 'growth_rate',
            'baseline': {'result_id': 'result-1', 'row': 0, 'column': 'total'},
            'current': {'result_id': 'result-1', 'row': 1, 'column': 'total'}}), result_id='aggregate-1')
        text = publication_answer('增长率-18.18%。说明：分母为对应年度记录数4条。', [raw, growth], '核对增长率')
        self.assertNotIn('记录数4', text)
        self.assertIn('550', text)
        text = publication_answer('123.45（单位：total_metric，即该字段原始单位）。', [raw], '读取total_metric')
        self.assertNotIn('单位：total_metric', text)

    def test_accepted_draft_does_not_request_another_model_call(self):
        llm = N(publish_draft=True, generate_chat_stream=lambda m: self.fail('unnecessary final rewrite'))
        events = list(stream_final_answer(llm, [], '读取金额', '金额为7元', None, set(), None, None,
            numeric_results=[{'result_id': 'result-1', 'columns': ['total'], 'rows': [[7]]}]))
        self.assertEqual('金额为7元', ''.join(e.get('text', '') for e in events))

    def test_two_workbook_match_routes_through_cached_tool(self):
        from app.agent.multi_workbook import run_multi_workbook_agent
        actions = [
            ('query_workbook', {'workbook_id': 'a', 'sql': 'SELECT model FROM data'}),
            ('query_workbook', {'workbook_id': 'b', 'sql': 'SELECT model FROM data'}),
            ('aggregate_results', {'operation': 'match_keys', 'left_result_id': 'result-1',
                'right_result_id': 'result-2', 'left_key': 'model', 'right_key': 'model'}),
            ('terminate', {'result': '匹配{{aggregate-1:0:matched_rows}}行，未匹配{{aggregate-1:0:unmatched_rows}}行'})]
        replies = iter(json.dumps({'action': a, 'action_input': v}, ensure_ascii=False) for a, v in actions)
        llm = N(publish_draft=True, generate_chat=lambda m: next(replies))
        service = N(active_database_snapshot=lambda book, **k: {'workbook_id': book, 'import_id': book, 'version': 1},
            execute_readonly_sql_snapshot=lambda *a, **k: N(sql='SELECT model FROM data', columns=['model'],
                rows=[['a'], ['a']], row_count=2, truncated=False))
        events = list(run_multi_workbook_agent(llm, service, owner_id='owner', question='按型号精确匹配两个附件',
            catalogs=[{'workbook_id': 'a'}, {'workbook_id': 'b'}]))
        self.assertEqual('done', events[-1]['type'])
        self.assertEqual('匹配2行，未匹配0行', events[-1]['full_answer'])
