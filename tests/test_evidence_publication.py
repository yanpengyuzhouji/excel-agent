import json
import unittest
from types import SimpleNamespace as N
from unittest.mock import patch
from app.agent.multi_workbook import run_multi_workbook_agent


class EvidencePublicationTests(unittest.TestCase):
    def test_observation_exposes_calculation_dependencies_not_only_success(self):
        operands = [{'result_id': 'result-1', 'row': 0, 'column': 'n'}]*2
        events = self.run_agent([('query_workbook', {'sql': 'SELECT COUNT(*) AS n FROM data'}),
            ('aggregate_results', {'operation': 'difference', 'baseline': operands[0], 'current': operands[1]}),
            ('observe_execution', {}),
            ('terminate', {'result': '差额{{aggregate-1:0:difference}}'})])
        observed = next(e['result'] for e in events if e.get('type') == 'step.done' and e.get('action') == 'observe_execution')
        self.assertEqual(operands, observed['calculations'][0]['derivation']['operands'])
        self.assertEqual(['result-1'], observed['calculations'][0]['input_result_ids'])
        self.assertTrue(observed['calculations'][0]['derivation_complete'])

    def test_shares_percent_is_typed_publishable_evidence(self):
        events = self.run_agent([('query_workbook', {'sql': 'SELECT COUNT(*) AS n FROM data'}),
            ('aggregate_results', {'operation': 'shares', 'result_ids': ['result-1'], 'value_column': 'n'}),
            ('terminate', {'result': '占比{{aggregate-1:0:share_pct|2}}%'})])
        self.assertEqual('done', events[-1]['type'])
        self.assertEqual('占比100.00%', events[-1]['full_answer'])

    def test_large_catalog_supplies_typed_fields_before_executing_guessed_sql(self):
        import sqlite3
        columns = [{'physical_name': 'money', 'display_name': '投资估算（万元）', 'sql_type': 'REAL'},
                   {'physical_name': 'length', 'display_name': '光缆长度（千米）', 'sql_type': 'REAL'}]
        columns += [{'physical_name': 'c'+str(i), 'display_name': '其他业务字段说明'*12, 'sql_type': 'TEXT'} for i in range(40)]
        catalog = {'workbook_id': 'w', 'catalog': {'tables': [
            {'table_name': 'data' if i == 0 else 'other'+str(i), 'columns': columns} for i in range(14)]}}
        actions = [('query_workbook', {'sql': 'SELECT length AS amount FROM data'}),
                   ('query_workbook', {'sql': 'SELECT money AS amount FROM data'}),
                   ('query_workbook', {'sql': 'SELECT money AS amount FROM data'}),
                   ('terminate', {'result': '投资{{result-1:0:amount}}万元'})]
        replies = iter(json.dumps({'action': a, 'action_input': v}) for a, v in actions)
        llm = N(evidence_publication=True, analysis_contract=False, structured_control=True,
                generate_chat=lambda m: next(replies))
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE data(money REAL, length REAL)')
            db.execute('INSERT INTO data VALUES(100,7)')
            executed = []
            def execute(snapshot, sql, **kwargs):
                executed.append(sql)
                cursor = db.execute(sql)
                return N(sql=sql, columns=[c[0] for c in cursor.description], rows=cursor.fetchall(), row_count=1, truncated=False)
            service = N(active_database_snapshot=lambda *a, **k: {'workbook_id': 'w', 'import_id': 'i', 'version': 1},
                        execute_readonly_sql_snapshot=execute)
            events = list(run_multi_workbook_agent(llm, service, owner_id='o', question='投资是多少',
                catalogs=[catalog], max_loops=8))
        self.assertEqual('投资100.0万元', events[-1]['full_answer'])
        self.assertNotIn('SELECT length AS amount FROM data', executed)
        self.assertEqual(2, sum(e.get('type') == 'step.start' and e.get('action') == 'inspect_workbook' for e in events))
        self.assertEqual(1, sum(e.get('type') == 'source.query.result' for e in events))

    def test_publication_checkpoint_allows_correcting_aggregate_granularity(self):
        import sqlite3
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE data(n INTEGER)')
            db.executemany('INSERT INTO data VALUES(?)', [(2,), (3,)])
            actions = [('query_workbook', {'sql': 'SELECT COUNT(*) AS n FROM data'}),
                       ('terminate', {'result': '记录合计{{result-1:0:n}}'}),
                       ('query_workbook', {'sql': 'SELECT SUM(n) AS n FROM data'}),
                       ('terminate', {'result': '记录合计{{result-2:0:n}}'})]
            replies = iter(json.dumps({'action': a, 'action_input': v}) for a, v in actions)
            llm = N(evidence_publication=True, publication_observation=True, analysis_contract=False,
                    structured_control=True, generate_chat=lambda m: next(replies))
            def execute(snapshot, sql, **kwargs):
                cursor = db.execute(sql)
                return N(sql=sql, columns=[c[0] for c in cursor.description], rows=cursor.fetchall(), row_count=1, truncated=False)
            service = N(active_database_snapshot=lambda *a, **k: {'workbook_id': 'w', 'import_id': 'i', 'version': 1},
                        execute_readonly_sql_snapshot=execute)
            events = list(run_multi_workbook_agent(llm, service, owner_id='o',
                question='data每行n已是分组记录数，请合计原始业务记录数',
                catalogs=[{'workbook_id': 'w', 'catalog': {'tables': [{'table_name': 'data',
                    'columns': [{'physical_name': 'n', 'sql_type': 'INTEGER'}]}]}}], max_loops=8))
        self.assertEqual('记录合计5', events[-1]['full_answer'])
        self.assertEqual(1, sum(e.get('status') == 'publication_observation' for e in events))
        self.assertEqual(2, sum(e.get('type') == 'source.query.result' for e in events))

    def test_aggregate_guard_tracks_dependency_availability_not_unrelated_outputs(self):
        from app.agent.multi_workbook import _aggregation_context
        action = {'operation': 'sum', 'result_ids': ['result-1']}
        before = _aggregation_context(action, [])
        available = _aggregation_context(action, [{'result_id': 'result-1'}])
        self.assertNotEqual(before, available)
        self.assertEqual(available, _aggregation_context(action, [
            {'result_id': 'result-1'}, {'result_id': 'aggregate-1'}]))
        rows = [{'result_id': 'result-1', 'source': {'workbook_id': 'w'}}]
        implicit = _aggregation_context({'operation': 'sum'}, rows)
        self.assertNotEqual(_aggregation_context({'operation': 'sum'}, []), implicit)
        self.assertEqual(implicit, _aggregation_context({'operation': 'sum'}, rows+[{'result_id': 'aggregate-1'}]))
        self.assertNotEqual(implicit, _aggregation_context({'operation': 'sum'}, rows+[
            {'result_id': 'result-2', 'source': {'workbook_id': 'w'}}]))

    def test_identical_calculation_cannot_evade_loop_guard_by_creating_outputs(self):
        history = [{'role': 'tool', 'content': '', 'evidence': {'result_id': 'result-8',
            'source': {'workbook_id': 'w', 'import_id': 'i', 'version': 1}, 'sql': 'SELECT a,b FROM data',
            'columns': ['a', 'b'], 'rows': [[78, 106]], 'truncated': False}}]
        action = {'operation': 'difference',
                  'baseline': {'result_id': 'result-8', 'row': 0, 'column': 'a'},
                  'current': {'result_id': 'result-8', 'row': 0, 'column': 'b'}}
        events = self.run_agent([('aggregate_results', action)] * 4 +
                               [('terminate', {'result': '差额{{aggregate-1:0:difference}}'})], history=history)
        completed = [e for e in events if e.get('type') == 'step.done' and e.get('action') == 'aggregate_results']
        self.assertEqual(1, len(completed))
        self.assertEqual('error', events[-1]['type'])
        self.assertIn('Tool loop stopped', events[-1]['error'])

    def test_profile_is_cached_and_authorized(self):
        events = self.run_agent([('profile_workbook', {'table_name': 'data', 'columns': ['n']}),
            ('terminate', {'result': '记录数{{result-1:0:n}}'})])
        self.assertEqual('done', events[-1]['type'])
        self.assertTrue(any(e['type'] == 'source.query.result' for e in events))
        events = self.run_agent([('profile_workbook', {'table_name': 'outside', 'columns': ['n']}),
            ('terminate', {'result': '无法读取'})])
        self.assertTrue(any(e['type'] == 'tool.error' for e in events))

    def test_compare_totals_publishes_cached_values(self):
        history = [{'role': 'tool', 'content': '', 'evidence': {'result_id': 'result-8',
            'source': {'workbook_id': 'w', 'import_id': 'i', 'version': 1}, 'sql': 'SELECT a,b FROM data',
            'columns': ['a', 'b'], 'rows': [[10, 20], [5, 10]], 'truncated': False}}]
        events = self.run_agent([('aggregate_results', {'operation': 'compare_totals', 'result_ids': ['result-8'],
            'baseline_column': 'a', 'current_column': 'b'}),
            ('terminate', {'result': '本期{{aggregate-1:0:current_total}}，增长率{{aggregate-1:0:growth_rate_pct|2}}%'})], history=history)
        self.assertEqual('done', events[-1]['type'])
        self.assertIn('本期30，增长率100.00%', events[-1]['full_answer'])
    def test_count_classifier_is_not_a_physical_unit(self):
        from app.agent.evidence_publication import publish
        self.assertEqual('共有29种', publish('共有{{result-1:0:n}}种',
            [{'result_id': 'result-1', 'columns': ['n'], 'rows': [[29]],
              'column_units': {'n': '条'}}])['text'])

    def test_sum_selected_cells_does_not_include_total_or_ignore_scope(self):
        from app.agent.multi_workbook import _aggregate_results
        from app.agent.evidence_publication import describe_result
        rows = [{'result_id': 'result-1', 'columns': ['label', 'n'],
                 'rows': [['total', 30], ['a', 20], ['b', 10]], 'column_units': {'n': '元'}}]
        refs = [{'result_id': 'result-1', 'row': i, 'column': 'n'} for i in [1, 2]]
        action = {'operation': 'sum', 'operands': refs}
        result = _aggregate_results(rows, action)
        self.assertEqual([['30']], result['rows'])
        self.assertEqual(refs, result['derivation']['operands'])
        describe_result(result, [], rows)
        self.assertEqual('元', result['column_units']['sum'])
        for extra in [{'value_column': 'n'}, {'group_by': []}]:
            self.assertIn('error', _aggregate_results(rows, dict(action, **extra)))
        self.assertIn('error', _aggregate_results(rows, dict(action, operands=[999])))
        rows[0]['rows'][2][1] = None
        self.assertEqual([[None]], _aggregate_results(rows, action)['rows'])
        rows[0]['truncated'] = True
        self.assertIn('error', _aggregate_results(rows, action))

    def test_source_only_citation_is_human_readable_not_a_scalar_bypass(self):
        from app.agent.evidence_publication import publish
        inspection = {'result_id': 'inspection-1', 'inspection_table': 'data',
            'source': {'display_name': '工作簿'}, 'columns': ['sheet', 'row', 'text'],
            'rows': [['备注页', 10, '合计3类']]}
        text = publish('来源{{inspection-1}}。原文：{{inspection-1:0:text}}', [inspection])['text']
        self.assertIn('工作簿 · 备注页', text)
        self.assertNotIn('{{inspection-1}}', text)
        with self.assertRaises(ValueError):
            publish('总额999，来源{{result-1}}', [{'result_id': 'result-1', 'columns': ['n'], 'rows': [[13]]}])

    def test_markdown_escaped_precision_separator_is_not_part_of_column_name(self):
        from app.agent.evidence_publication import publish
        evidence = [{'result_id': 'aggregate-1', 'operation': 'difference',
            'columns': ['difference'], 'rows': [['0.005']], 'truncated': False}]
        self.assertEqual('差额0.01', publish(r'差额{{aggregate-1:0:difference\|2}}', evidence)['text'])

    def test_html_template_and_inline_table_become_readable_blocks(self):
        from app.agent.evidence_publication import publish, render_html
        packet = publish('<h2>报告</h2><p>明细见{{table:result-1}}。</p>',
            [{'result_id': 'result-1', 'columns': ['n'], 'rows': [[13]]}])
        self.assertNotIn('<h2>', packet['text'])
        self.assertIn('\n\n| n |\n| --- |\n| 13 |\n\n', packet['text'])
        self.assertIn('<table>', render_html(packet['text'], '报告'))

    def test_known_tool_unit_is_added_when_model_omits_it(self):
        from app.agent.evidence_publication import publish
        packet = publish('合计{{result-1:0:n|2}}', [{'result_id': 'result-1',
            'columns': ['n'], 'rows': [[100]], 'column_units': {'n': '万元'}}])
        self.assertEqual('合计100.00 万元', packet['text'])

    def test_ratio_unit_follows_operand_dimensions(self):
        from app.agent.evidence_publication import describe_result
        result = {'operation': 'ratio', 'columns': ['ratio', 'ratio_pct'],
            'derivation': {'operands': [{'result_id': 'result-1', 'column': 'weighted'},
                                       {'result_id': 'result-1', 'column': 'weights'}]}}
        describe_result(result, [], [{'result_id': 'result-1', 'column_units': {'weighted': '万元·台', 'weights': '台'}}])
        self.assertEqual('万元', result['column_units']['ratio'])

    def test_calculation_missing_input_is_a_typed_result_not_a_tool_error(self):
        from app.agent.numeric_evidence import calculate_comparison
        evidence = [{'result_id': 'result-1', 'columns': ['base', 'now'], 'rows': [[None, 50]], 'truncated': False}]
        for operation in ['difference', 'growth_rate']:
            result = calculate_comparison(evidence, {'operation': operation,
                'baseline': {'result_id': 'result-1', 'row': 0, 'column': 'base'},
                'current': {'result_id': 'result-1', 'row': 0, 'column': 'now'}})
            self.assertIsNone(result['rows'][0][0])
            self.assertEqual('missing_input', result['value_status'])

    def test_reference_unit_check_uses_tool_expression_unit(self):
        from app.agent.evidence_publication import publish
        evidence = [{'result_id': 'aggregate-1', 'operation': 'weighted_average',
            'columns': ['weighted_average'], 'rows': [['176.7987']],
            'column_units': {'weighted_average': '万元'}}]
        corrected = publish('{{aggregate-1:0:weighted_average|4}}（万元/台）', evidence)
        self.assertEqual('176.7987（万元）', corrected['text'])
        self.assertEqual('万元/台', corrected['unit_normalizations'][0]['model_unit'])
        self.assertEqual('176.7987万元', publish('{{aggregate-1:0:weighted_average|4}}万元', evidence)['text'])
    def test_review_is_bounded_and_does_not_rewrite_publication(self):
        from app.agent.evidence_publication import review_publication
        from app.agent.budget import AgentBudget
        llm = N(max_tokens=8192, request_phase='agent_control', last_finish_reason='stop',
                generate_chat=lambda m: '{"decision":"revise","feedback":"Group row is not the requested total"}')
        publication = {'text': '总计13', 'references': []}
        budget = AgentBudget()
        review = review_publication(llm, '总计多少', publication, [], budget)
        self.assertEqual('revise', review['decision'])
        self.assertEqual('总计13', publication['text'])
        self.assertEqual(8192, llm.max_tokens)
        self.assertEqual('agent_control', llm.request_phase)
        self.assertEqual(1, budget.llm_calls)

    def test_text_inspection_does_not_require_numeric_references(self):
        from app.agent.evidence_publication import publish
        packet = publish('备注原文：合计3类设备。', [{'result_id': 'inspection-1',
            'inspection_table': 'data', 'columns': ['text'], 'rows': [['合计3类设备。']]}])
        self.assertEqual('备注原文：合计3类设备。', packet['text'])

    def test_followup_keeps_cached_calculation_ids_and_lineage(self):
        history = [{'role': 'tool', 'content': '', 'evidence': {
            'result_id': 'result-8', 'source': {'workbook_id': 'w', 'import_id': 'i', 'version': 1},
            'sql': 'SELECT n FROM data', 'columns': ['n'], 'rows': [[13]], 'truncated': False}},
            {'role': 'tool', 'content': '', 'evidence': {'result_id': 'aggregate-7',
             'operation': 'sum', 'columns': ['n'], 'rows': [['13']],
             'input_result_ids': ['result-8'], 'truncated': False}}]
        events = self.run_agent([('terminate', {'result': '沿用上轮结果：{{aggregate-7:0:n}}条。'})], history=history)
        self.assertEqual('done', events[-1]['type'])
        self.assertIn('13条', events[-1]['full_answer'])

    def test_tool_describes_count_and_weighted_expression_without_plan(self):
        from app.agent.evidence_publication import describe_result
        catalogs = [{'workbook_id': 'w', 'catalog': {'tables': [{'table_name': 'data', 'columns': [
            {'physical_name': 'h', 'unit_hint': '万元'}, {'physical_name': 'n', 'unit_hint': '台'}]}]}}]
        result = {'result_id': 'result-1', 'source': {'workbook_id': 'w'},
                  'sql': 'SELECT COUNT(*) AS cnt, SUM(h*n) AS weighted, SUM(n) AS weights FROM data',
                  'columns': ['cnt', 'weighted', 'weights'], 'rows': [[13, 100, 2]]}
        describe_result(result, catalogs)
        self.assertEqual('computed_metrics', result['result_kind'])
        self.assertEqual({'cnt': '条', 'weighted': '万元·台', 'weights': '台'}, result['column_units'])

    def test_invalid_and_truncated_scalar_references_fail_without_streaming(self):
        from app.agent.evidence_publication import publish
        result = {'result_id': 'result-1', 'columns': ['n'], 'rows': [[13]], 'truncated': True}
        for template in ['{{result-1:0:n}}', '{{result-99:0:n}}', '{{result-1:0:bad}}']:
            with self.assertRaises(ValueError):
                publish(template, [result])

    def test_cached_table_data_cannot_inject_a_reference(self):
        from app.agent.evidence_publication import publish
        packet = publish('{{table:result-1}}', [{'result_id': 'result-1', 'columns': ['note'],
            'rows': [['{{result-999:0:secret}}']], 'truncated': False}])
        self.assertIn('{{result-999:0:secret}}', packet['text'])
        self.assertEqual([], packet['references'])

    def test_complete_controller_index_compacts_without_pinning_large_rows(self):
        from app.agent.context_window import ContextWindow
        q = {'role': 'user', 'content': '比较金额'}
        messages = [{'role': 'system', 'content': 'system'},
                    {'role': 'user', 'content': 'old observation '*5000}, q,
                    {'role': 'user', 'content': 'Execution controller state: {"result_id":"result-1"}'}]
        window = ContextWindow(4096, 1024)
        window.prepare(messages, q)
        self.assertLessEqual(window.state(messages)['used_tokens'], window.input_limit)

    def run_agent(self, actions, question='查询记录数', history=None):
        replies = iter(json.dumps({'action': a, 'action_input': v}, ensure_ascii=False) for a, v in actions)
        llm = N(evidence_publication=True, analysis_contract=False, structured_control=True,
                generate_chat=lambda m: next(replies))
        service = N(active_database_snapshot=lambda *a, **k: {'workbook_id': 'w', 'import_id': 'i', 'version': 1},
                    execute_readonly_sql_snapshot=lambda *a, **k: N(sql='SELECT COUNT(*) AS n FROM data',
                        columns=['n'], rows=[[13]], row_count=1, truncated=False))
        return list(run_multi_workbook_agent(llm, service, owner_id='o', question=question,
            catalogs=[{'workbook_id': 'w', 'catalog': {'tables': [{'table_name': 'data', 'columns': [{'physical_name': 'n'}]}]}}],
            max_loops=8, conversation_id='test', history_messages=history))

    def test_query_first_without_plan_and_without_recounting(self):
        events = self.run_agent([('query_workbook', {'sql': 'SELECT COUNT(*) AS n FROM data'}),
            ('terminate', {'result': '记录数为{{result-1:0:n}}条。'})])
        self.assertEqual('done', events[-1]['type'])
        self.assertEqual('记录数为13条。', events[-1]['full_answer'])
        self.assertFalse(any(e.get('status') == 'counting_review' for e in events))

    def test_report_uses_identical_template_no_second_rewrite(self):
        with patch('app.agent.multi_workbook.tools.html_interpreter', return_value={
                'report_id': 'r', 'url': '/reports/r'}) as save:
            events = self.run_agent([('query_workbook', {'sql': 'SELECT COUNT(*) AS n FROM data'}),
                ('html_interpreter', {'title': '报告', 'result': '记录数为{{result-1:0:n}}条。\n\n{{table:result-1}}'}),
                ('terminate', {})], '生成HTML报告并提供PDF')
        self.assertEqual('done', events[-1]['type'])
        self.assertIn('记录数为13条', events[-1]['full_answer'])
        self.assertIn('记录数为13条', save.call_args.args[2])
        self.assertIn('<table>', save.call_args.args[2])

    def test_numeric_query_cannot_publish_unreferenced_explanation(self):
        events = self.run_agent([('query_workbook', {'sql': 'SELECT COUNT(*) AS n FROM data'})]+
            [('terminate', {'result': '模型解释：共有999条'})]*3)
        self.assertEqual('result_reference_invalid', events[-1]['error_code'])
        self.assertFalse(any(e['type'] == 'answer.delta' for e in events))

    def test_missing_fields_explanation_does_not_require_numeric_plan(self):
        events = self.run_agent([('terminate', {'result': '需要先明确统计年份。'})], '需要什么口径？')
        self.assertEqual('done', events[-1]['type'])


if __name__ == '__main__':
    unittest.main()
