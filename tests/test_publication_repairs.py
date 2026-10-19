import unittest
from types import SimpleNamespace as N
from unittest.mock import patch

from app.agent.multi_workbook import stream_final_answer, _aggregate_results, run_multi_workbook_agent
from app.agent.numeric_evidence import render_numeric_answer


class PublicationRepairTests(unittest.TestCase):
    def test_workbook_absence_claim_requires_full_table_scope(self):
        from app.agent.numeric_evidence import validate_catalog_claim
        catalogs = [{'workbook_id': 'a', 'catalog': {'tables': [{'table_name': 'table_1'}, {'table_name': 'table_2'}]}}]
        results = [{'source': {'workbook_id': 'a'}, 'sql': 'SELECT * FROM table_1'}]
        with self.assertRaises(ValueError):
            validate_catalog_claim('本工作簿中缺少实际回款金额。', catalogs, results=results)
        validate_catalog_claim('已核查的汇总表缺少实际回款金额；其他表未核查。', catalogs, results=results)

    def test_report_multiplier_requires_ratio_evidence(self):
        from app.agent.numeric_evidence import validate_report_comparisons
        with self.assertRaises(ValueError):
            validate_report_comparisons('<p>市区约为福清的1.60倍。</p>', [])
        validate_report_comparisons('<p>差额16134.9005万元。</p>', [])
        validate_report_comparisons('<p>约1.60倍。</p>', [{'operation': 'ratio', 'columns': ['ratio'], 'rows': [['1.60']]}])
    def test_unit_contract_applies_to_fast_and_buffered_paths(self):
        for verified in (True, False):
            llm = N(weighted_unit_contract={'result_unit': '万元', 'weight_unit': '台'})
            events = list(stream_final_answer(llm, [], '', '加权平均176.7987万元/台', None, set(), None, None, verified))
            self.assertIn('176.7987万元', ''.join(e.get('text', '') for e in events))
            self.assertNotIn('万元/台', ''.join(e.get('text', '') for e in events))

    def test_reference_binds_an_explicit_scenario(self):
        result = {'result_id': 'result-1', 'columns': ['scenario', 'baseline', 'current'],
                  'rows': [['正常', 100, 120], ['负基期', -100, -50]]}
        with self.assertRaises(ValueError):
            render_numeric_answer('负基期{{result-1:0:baseline}}', [result], question='比较负基期差额')
        self.assertEqual('负基期-100', render_numeric_answer('负基期{{result-1:1:baseline}}', [result], question='比较负基期差额')[0])
        # Copying raw text or comparing multiple scenarios is not a single-row contract.
        self.assertEqual('100', render_numeric_answer('{{result-1:0:baseline}}', [result], question='原样引用备注中的负基期')[0])

    def test_ratio_is_not_growth_rate(self):
        result = {'result_id': 'result-1', 'columns': ['total'], 'rows': [[200], [100]]}
        value = _aggregate_results([result], {'operation': 'ratio',
            'numerator': {'result_id': 'result-1', 'row': 0, 'column': 'total'},
            'denominator': {'result_id': 'result-1', 'row': 1, 'column': 'total'}})
        self.assertNotIn('error', value)
        self.assertEqual([['2', '200']], value['rows'])

    def test_ratio_zero_and_partial_evidence(self):
        result = {'result_id': 'result-1', 'columns': ['total'], 'rows': [[200], [0]]}
        action = {'operation': 'ratio',
            'numerator': {'result_id': 'result-1', 'row': 0, 'column': 'total'},
            'denominator': {'result_id': 'result-1', 'row': 1, 'column': 'total'}}
        self.assertEqual([[None, None]], _aggregate_results([result], action)['rows'])
        self.assertIn('error', _aggregate_results([dict(result, truncated=True)], action))

    def test_derived_reference_preserves_scenario_contract(self):
        result = {'result_id': 'result-1', 'columns': ['scenario', 'baseline', 'current'],
                  'rows': [['正常', 100, 120], ['负基期', -100, -50]]}
        aggregate = _aggregate_results([result], {'operation': 'difference',
            'baseline': {'result_id': 'result-1', 'row': 0, 'column': 'baseline'},
            'current': {'result_id': 'result-1', 'row': 0, 'column': 'current'}})
        aggregate['result_id'] = 'aggregate-1'
        with self.assertRaises(ValueError):
            render_numeric_answer('{{aggregate-1:0:difference}}', [result, aggregate], question='负基期差额')

    def test_catalog_claim_guard_is_narrow(self):
        from app.agent.numeric_evidence import validate_catalog_claim
        catalogs = [{'catalog': {'tables': [{}, {}]}}]
        with self.assertRaises(ValueError):
            validate_catalog_claim('这是唯一工作表。', catalogs)
        validate_catalog_claim('这不是唯一工作表。', catalogs)
        validate_catalog_claim('唯一工作表', catalogs, '原样引用备注')

    def test_final_reference_can_be_corrected_once(self):
        replies = iter(['Action: query_workbook\nAction Input: {"sql":"SELECT COUNT(*) AS total FROM table_1"}',
                        'Action: terminate\nAction Input: {"result":"查询完成。"}'])
        streams = iter(['总数{{result-1:0:missing}}', '总数{{result-1:0:total}}'])
        llm = N(generate_chat=lambda m: next(replies), generate_chat_stream=lambda m: iter([next(streams)]))
        service = N(active_database_snapshot=lambda *a, **k: {'workbook_id': 'a', 'import_id': 'i', 'version': 1},
            execute_readonly_sql_snapshot=lambda *a, **k: N(sql='SELECT COUNT(*) AS total FROM table_1', columns=['total'], rows=[[25]], row_count=1, truncated=False))
        events = list(run_multi_workbook_agent(llm, service, owner_id='owner', question='读取查询结果', catalogs=[{'workbook_id': 'a'}]))
        self.assertEqual('done', events[-1]['type'])
        self.assertEqual('总数25', events[-1]['full_answer'])
        self.assertNotIn('missing', ''.join(e.get('text', '') for e in events))
        self.assertTrue(any(e.get('status') == 'final_reference_review' for e in events))

    def test_structured_whitespace_recovery_adds_json_instruction(self):
        from app.service.llm import ExcelLLM
        def chunk(text):
            return N(choices=[N(delta=N(content=text), finish_reason='stop')])
        with patch('openai.OpenAI') as factory:
            factory.return_value.chat.completions.create.side_effect = [[chunk(' \n\t')], [chunk('{"action":"terminate","action_input":{"result":"ok"}}')]]
            llm = ExcelLLM({'provider': 'deepseek', 'api_key': 'test'})
            llm.request_phase = 'agent_control'
            messages = [{'role': 'user', 'content': 'hello'}]
            self.assertIn('terminate', llm.generate_chat(messages))
            retry = factory.return_value.chat.completions.create.call_args_list[-1].kwargs
            self.assertIn('JSON', retry['messages'][0]['content'])
            self.assertEqual([{'role': 'user', 'content': 'hello'}], messages)
