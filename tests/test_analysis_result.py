import unittest
import json
from types import SimpleNamespace as N
from unittest.mock import patch
from copy import deepcopy

from app.agent.analysis_result import AnalysisResult


CATALOGS = [{'workbook_id': 'w', 'display_name': '项目', 'catalog': {'tables': [
    {'table_name': 'data', 'display_name': '明细', 'columns': [
        {'physical_name': 'amount', 'display_name': '投资（万元）', 'unit_hint': '万元'},
        {'physical_name': 'spec', 'display_name': '规格'},
        {'physical_name': 'n', 'display_name': '条数', 'unit_hint': '条'}]}]}}]
SOURCE = {'workbook_id': 'w', 'import_id': 'i', 'version': 1, 'display_name': '项目'}


def result(columns=('a', 'b'), rows=((85347.4729, 36346.7817),), **extra):
    return dict(result_id='result-1', columns=list(columns), rows=[list(r) for r in rows],
                source=SOURCE, sql='SELECT amount AS a, amount AS b FROM data', **dict({'truncated': False}, **extra))


def cell(column='amount'):
    return {'workbook_id': 'w', 'table_name': 'data', 'column': column}


class AnalysisResultTests(unittest.TestCase):
    def test_sql_aggregate_cannot_be_counted_as_one_business_row(self):
        evidence = result(('n',), ((13,),))
        evidence['sql'] = 'SELECT COUNT(*) AS n FROM data'
        task = {'id': 'n', 'label': '记录数', 'operation': 'count', 'source': cell()}
        binding = {'n': {'result_id': 'result-1', 'row': 0, 'column': 'n'}}
        with self.assertRaisesRegex(ValueError, 'already computed'):
            AnalysisResult({'tasks': [task]}, CATALOGS).resolve(binding, [evidence])
        task.update(operation='value', result_unit='条')
        packet = AnalysisResult({'tasks': [task]}, CATALOGS).resolve(binding, [evidence])
        self.assertEqual('13', packet['tasks'][0]['value'])
        self.assertEqual('条', packet['tasks'][0]['unit'])

    def test_group_average_requires_explicit_calculation(self):
        evidence = result(('a',), ((10,), (100,)))
        evidence['sql'] = 'SELECT AVG(amount) AS a FROM data GROUP BY spec'
        task = {'id': 'avg', 'label': '平均', 'operation': 'average', 'source': cell()}
        with self.assertRaisesRegex(ValueError, 'already computed'):
            AnalysisResult({'tasks': [task]}, CATALOGS).resolve(
                {'avg': {'result_id': 'result-1', 'column': 'a'}}, [evidence])

    def test_expression_unit_is_not_inherited_from_single_column(self):
        evidence = result(('a',), ((100,),))
        evidence['sql'] = 'SELECT SUM(amount*n) AS a FROM data'
        task = {'id': 'total', 'label': '加权总额', 'operation': 'value', 'source': cell()}
        bindings = {'total': {'result_id': 'result-1', 'row': 0, 'column': 'a'}}
        with self.assertRaisesRegex(ValueError, 'result_unit'):
            AnalysisResult({'tasks': [task]}, CATALOGS).resolve(bindings, [evidence])
        task['result_unit'] = '万元·条'
        packet = AnalysisResult({'tasks': [task]}, CATALOGS).resolve(bindings, [evidence])
        self.assertEqual('万元·条', packet['tasks'][0]['unit'])

    def test_declared_compound_units_cancel_without_business_rules(self):
        from app.agent.analysis_result import divide_units
        self.assertEqual('万元', divide_units('万元·台', '台'))
        self.assertEqual('倍', divide_units('万元', '万元'))
        self.assertEqual('', divide_units('', ''))
        self.assertEqual('万元/台', divide_units('万元', '台'))

    def test_generated_table_has_adjacent_markdown_rows(self):
        p = AnalysisResult({'tasks': [{'id': 't', 'label': '表', 'operation': 'table', 'source': cell()}]}, CATALOGS)
        packet = p.resolve({'t': {'result_id': 'result-1'}}, [result()])
        self.assertIn('| a | b |\n| --- | --- |\n|', p.text(packet))

    def test_interpretation_uses_same_values_in_text_and_html(self):
        p = self.comparison()
        p.plan['summary'] = '差额为{{task:delta}}，两者不一致。'
        packet = p.resolve({'city': {'result_id': 'result-1', 'row': 0, 'column': 'a'},
                            'fuqing': {'result_id': 'result-1', 'row': 0, 'column': 'b'}}, [result()])
        self.assertIn('差额为49000.6912 万元', p.text(packet))
        self.assertIn('差额为49000.6912 万元', p.html(packet))

    def comparison(self):
        return AnalysisResult({'tasks': [
            {'id': 'city', 'label': '市区', 'operation': 'value', 'source': cell(), 'scope': '市区'},
            {'id': 'fuqing', 'label': '福清', 'operation': 'value', 'source': cell(), 'scope': '福清'},
            {'id': 'delta', 'label': '市区减福清', 'operation': 'subtract', 'inputs': ['city', 'fuqing'], 'decimal_places': 4},
            {'id': 'ratio', 'label': '市区/福清', 'operation': 'ratio', 'inputs': ['city', 'fuqing'], 'decimal_places': 4}
        ]}, CATALOGS)

    def test_compiles_all_tasks_with_direction_units_and_one_publication(self):
        plan = self.comparison()
        packet = plan.resolve({'city': {'result_id': 'result-1', 'row': 0, 'column': 'a'},
                               'fuqing': {'result_id': 'result-1', 'row': 0, 'column': 'b'}}, [result()])
        self.assertEqual('49000.6912', packet['tasks'][2]['value'])
        self.assertEqual('万元', packet['tasks'][2]['unit'])
        self.assertEqual('2.3481', packet['tasks'][3]['value'])
        self.assertIn('市区 − 福清', plan.text(packet))
        self.assertIn('49000.6912', plan.html(packet))

    def test_missing_second_metric_is_not_complete(self):
        with self.assertRaisesRegex(ValueError, 'fuqing'):
            self.comparison().resolve({'city': {'result_id': 'result-1', 'row': 0, 'column': 'a'}}, [result()])

    def test_multi_metric_sums_are_program_owned(self):
        plan = AnalysisResult({'tasks': [
            {'id': 'count', 'label': '总条数', 'operation': 'sum', 'source': cell('n')},
            {'id': 'amount', 'label': '总投资', 'operation': 'sum', 'source': cell()}
        ]}, CATALOGS)
        packet = plan.resolve({'count': {'result_id': 'result-1', 'column': 'n'},
                               'amount': {'result_id': 'result-1', 'column': 'total'}},
                              [result(('n', 'total'), ((676, 10), (419, 20)))])
        self.assertEqual('1095', packet['tasks'][0]['value'])
        self.assertEqual('30', packet['tasks'][1]['value'])

    def test_original_text_and_html_escaping(self):
        plan = AnalysisResult({'tasks': [{'id': 'spec', 'label': '规格', 'operation': 'table', 'source': cell('spec')}]}, CATALOGS)
        packet = plan.resolve({'spec': {'result_id': 'result-1', 'columns': ['id', 'spec']}},
                              [result(('id', 'spec'), (('00001', '1*35<script>'),))])
        self.assertIn('00001', plan.text(packet))
        self.assertIn('1*35&lt;script&gt;', plan.html(packet))

    def test_truncation_and_literal_bindings_rejected(self):
        for evidence, binding in [(result(truncated=True), {'result_id': 'result-1', 'row': 0, 'column': 'a'}),
                                  (result(), {'value': 999})]:
            with self.assertRaises(ValueError):
                self.comparison().resolve({'city': binding, 'fuqing': binding}, [evidence])

    def test_empty_result_is_not_zero(self):
        plan = AnalysisResult({'tasks': [{'id': 'empty', 'label': '2023年金额', 'operation': 'value', 'source': cell()}]}, CATALOGS)
        packet = plan.resolve({'empty': {'result_id': 'result-1', 'row': 0, 'column': 'a'}}, [result(rows=())])
        self.assertEqual('no_records', packet['tasks'][0]['status'])
        self.assertIn('未查到记录', plan.text(packet))

    def test_zero_baseline_and_negative_subtraction(self):
        plan = AnalysisResult({'tasks': [
            {'id': 'base', 'label': '基期', 'operation': 'value', 'source': cell()},
            {'id': 'now', 'label': '本期', 'operation': 'value', 'source': cell()},
            {'id': 'growth', 'label': '增长率', 'operation': 'growth_rate', 'inputs': ['base', 'now']}
        ]}, CATALOGS)
        packet = plan.resolve({'base': {'result_id': 'result-1', 'row': 0, 'column': 'a'},
                               'now': {'result_id': 'result-1', 'row': 0, 'column': 'b'}}, [result(rows=((0, 50),))])
        self.assertEqual('undefined', packet['tasks'][2]['status'])

    def test_unknown_sources_and_plan_cycles_rejected(self):
        with self.assertRaises(ValueError):
            AnalysisResult({'tasks': [{'id': 'x', 'label': 'x', 'operation': 'value', 'source': dict(cell(), workbook_id='other')}]}, CATALOGS)
        with self.assertRaises(ValueError):
            AnalysisResult({'tasks': [{'id': 'x', 'label': 'x', 'operation': 'sum', 'inputs': ['x']}]}, CATALOGS)

    def test_negative_difference_is_not_magnitude_matched(self):
        packet = self.comparison().resolve({'city': {'result_id': 'result-1', 'row': 0, 'column': 'a'},
                                           'fuqing': {'result_id': 'result-1', 'row': 0, 'column': 'b'}},
                                          [result(rows=((10, 25),))])
        self.assertEqual('-15.0000', packet['tasks'][2]['value'])

    def test_unrounded_operands_are_used_for_half_up(self):
        p = self.comparison()
        p.tasks[2]['decimal_places'] = 2
        packet = p.resolve({'city': {'result_id': 'result-1', 'row': 0, 'column': 'a'},
                            'fuqing': {'result_id': 'result-1', 'row': 0, 'column': 'b'}},
                           [result(rows=((1.005, 1),))])
        self.assertEqual('0.01', packet['tasks'][2]['value'])

    def test_mixed_units_cannot_be_subtracted(self):
        p = self.comparison()
        p.tasks[1]['source'] = cell('n')
        with self.assertRaisesRegex(ValueError, 'units'):
            p.resolve({'city': {'result_id': 'result-1', 'row': 0, 'column': 'a'},
                       'fuqing': {'result_id': 'result-1', 'row': 0, 'column': 'b'}}, [result()])

    def test_cross_workbook_aggregate_requires_all_declared_provenance(self):
        books = CATALOGS+[dict(CATALOGS[0], workbook_id='other', display_name='参数')]
        p = AnalysisResult({'tasks': [{'id': 'matched', 'label': '匹配数', 'operation': 'value', 'result_unit': '条',
            'sources': [dict(cell(), column=None), dict(cell(), workbook_id='other', column=None)]}]}, books)
        left, right = result(), dict(result(), result_id='result-2', source=dict(SOURCE, workbook_id='other'))
        aggregate = {'result_id': 'aggregate-1', 'operation': 'match_keys', 'columns': ['matched_rows'],
                     'rows': [[12]], 'input_result_ids': ['result-1', 'result-2']}
        packet = p.resolve({'matched': {'result_id': 'aggregate-1', 'row': 0, 'column': 'matched_rows'}}, [left, right, aggregate])
        self.assertEqual('12', packet['tasks'][0]['value'])

    def test_parser_header_hint_and_mixed_physical_fields(self):
        books = deepcopy(CATALOGS)
        fields = books[0]['catalog']['tables'][0]['columns']
        fields[0].update(mixed_numeric=True, unit_hint='投资估算（万元）')
        plan = AnalysisResult({'tasks': [{'id': 'money', 'label': '金额', 'operation': 'value',
                                         'source': cell('amount_value')}]}, books)
        packet = plan.resolve({'money': {'result_id': 'result-1', 'row': 0, 'column': 'a'}}, [result()])
        self.assertEqual('万元', packet['tasks'][0]['unit'])
        from app.agent.analysis_result import metadata_unit
        self.assertEqual('kVA', metadata_unit({'unit_hint': '变压器容量(kVA)'}))
        self.assertEqual('', metadata_unit({'unit_hint': '建设类型（新建/改造）'}))

    def test_known_conversion_does_not_accept_arbitrary_factor(self):
        p = AnalysisResult({'tasks': [
            {'id': 'money', 'label': '万元金额', 'operation': 'value', 'source': cell()},
            {'id': 'yuan', 'label': '元金额', 'operation': 'convert', 'inputs': ['money'], 'unit': '元', 'decimal_places': 2}
        ]}, CATALOGS)
        binding = {'money': {'result_id': 'result-1', 'row': 0, 'column': 'a'}}
        self.assertEqual('10000.00', p.resolve(binding, [result(rows=((1, 0),))])['tasks'][1]['value'])
        p.tasks[1]['unit'] = 'kg'
        with self.assertRaises(ValueError):
            p.resolve(binding, [result()])

    def test_narrative_modes_are_explicit_and_evidence_bound(self):
        p = AnalysisResult({'mode': 'explanation', 'message': '已核查的字段中没有实际回款，不能代算。',
                            'evidence_ids': ['result-1']}, CATALOGS)
        with self.assertRaises(ValueError):
            p.resolve({}, [])
        self.assertIn('模型解释', p.text(p.resolve({}, [result()])))
        p = AnalysisResult({'mode': 'clarification', 'message': '需要按计划年份还是实际日期统计？'}, CATALOGS)
        self.assertIn('需要澄清', p.text(p.resolve({}, [])))

    def test_null_and_string_identifiers_not_coerced_to_numbers(self):
        p = AnalysisResult({'tasks': [{'id': 'id', 'label': '编号', 'operation': 'value', 'source': cell('spec')}]}, CATALOGS)
        packet = p.resolve({'id': {'result_id': 'result-1', 'row': 0, 'column': 'a'}}, [result(rows=(('00001', 0),))])
        self.assertEqual('00001', packet['tasks'][0]['value'])
        p.tasks[0]['decimal_places'] = 2
        with self.assertRaises(ValueError):
            p.resolve({'id': {'result_id': 'result-1', 'row': 0, 'column': 'a'}}, [result(rows=(('00001', 0),))])


class AnalysisAgentTests(unittest.TestCase):
    def test_plan_can_be_revised_before_publication(self):
        actions, bindings = self.actions()
        plan = deepcopy(actions[0][1])
        plan['summary'] = '差额为{{task:delta}}。'
        events = self.run_agent(actions+[('define_analysis', plan), ('terminate', {'bindings': bindings})])
        self.assertEqual('done', events[-1]['type'])
        self.assertEqual(2, sum(e['type'] == 'analysis.planned' for e in events))
        self.assertIn('差额为49000.6912', events[-1]['full_answer'])

    def run_agent(self, actions, question='比较两地投资'):
        from app.agent.multi_workbook import run_multi_workbook_agent
        replies = iter(json.dumps({'action': a, 'action_input': v}, ensure_ascii=False) for a, v in actions)
        llm = N(analysis_contract=True, publish_draft=True, structured_control=True,
                generate_chat=lambda m: next(replies),
                generate_chat_stream=lambda m: self.fail('No second free-form rewrite allowed'))
        service = N(active_database_snapshot=lambda *a, **k: SOURCE,
                    execute_readonly_sql_snapshot=lambda *a, **k: N(sql='SELECT amount AS a, amount AS b FROM data',
                        columns=['a', 'b'], rows=[[85347.4729, 36346.7817]], row_count=1, truncated=False))
        return list(run_multi_workbook_agent(llm, service, owner_id='owner', question=question,
                    catalogs=CATALOGS, max_loops=12, conversation_id='test'))

    def actions(self):
        plan = AnalysisResultTests().comparison().plan
        bindings = {'city': {'result_id': 'result-1', 'row': 0, 'column': 'a'},
                    'fuqing': {'result_id': 'result-1', 'row': 0, 'column': 'b'}}
        return [('define_analysis', plan), ('query_workbook', {'sql': 'SELECT amount AS a, amount AS b FROM data'})], bindings

    def test_program_publishes_not_wrong_model_draft(self):
        actions, bindings = self.actions()
        events = self.run_agent(actions+[('terminate', {'bindings': bindings, 'result': '差额-49000.6912，不支持PDF'})])
        self.assertEqual('done', events[-1]['type'])
        self.assertIn('49000.6912', events[-1]['full_answer'])
        self.assertNotIn('-49000.6912', events[-1]['full_answer'])
        self.assertNotIn('不支持PDF', events[-1]['full_answer'])
        self.assertEqual(4, len(events[-1]['analysis']['tasks']))

    def test_missing_binding_never_streams_partial_answer(self):
        actions, bindings = self.actions()
        del bindings['fuqing']
        events = self.run_agent(actions+[('terminate', {'bindings': bindings})]*3)
        self.assertEqual('analysis_incomplete', events[-1]['error_code'])
        self.assertFalse(any(e['type'] == 'answer.delta' for e in events))

    def test_report_and_answer_share_packet_and_real_pdf_link(self):
        actions, bindings = self.actions()
        with patch('app.agent.multi_workbook.tools.html_interpreter', return_value={
            'report_id': 'r', 'url': '/api/v1/agent/conversations/test/reports/r', 'title': '比较报告'}) as save:
            events = self.run_agent(actions+[('terminate', {'bindings': bindings}),
                ('html_interpreter', {'html': '<html>WRONG 999</html>', 'title': '比较报告'}), ('terminate', {})],
                '生成HTML分析报告并提供PDF')
        self.assertEqual('done', events[-1]['type'])
        self.assertEqual(1, save.call_count)
        generated_html = save.call_args.args[2]
        self.assertIn('49000.6912', generated_html)
        self.assertNotIn('WRONG', generated_html)
        self.assertIn('/reports/r/pdf', events[-1]['full_answer'])
        artifact = next(e for e in events if e['type'] == 'html')
        self.assertEqual(artifact['analysis'], events[-1]['analysis'])
        self.assertEqual(events[-1]['full_answer'], ''.join(e.get('text', '') for e in events if e['type'] == 'answer.delta'))

    def test_no_plan_cannot_silently_publish_half_sentence(self):
        events = self.run_agent([('terminate', {'result': '根据工作簿中的'})]*3)
        self.assertEqual('analysis_incomplete', events[-1]['error_code'])
        self.assertFalse(any(e['type'] == 'done' for e in events))

    def test_real_llm_defaults_to_direct_evidence_publication(self):
        from app.service.llm import ExcelLLM
        with patch('openai.OpenAI'):
            llm = ExcelLLM({'api_key': 'test'})
            self.assertFalse(llm.analysis_contract)
            self.assertTrue(llm.evidence_publication)


if __name__ == '__main__':
    unittest.main()
