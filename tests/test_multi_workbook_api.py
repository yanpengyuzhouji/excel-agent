import json
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.endpoints import router, get_service, authenticate_request


class MultiWorkbookApiTests(unittest.TestCase):
    def test_analysis_rejection_is_persisted_for_diagnosis(self):
        rejection = {'type': 'analysis.rejected', 'reason': 'already computed',
                     'bindings': {'n': {'result_id': 'result-1', 'column': 'n'}}, 'attempt': 1}
        llm = SimpleNamespace(model_name='test')
        with patch('app.api.endpoints.ExcelLLM', return_value=llm), patch(
                'app.api.endpoints.AgentConversation.create', return_value=self.conv), patch(
                'app.api.endpoints.run_multi_workbook_agent', return_value=iter([
                    rejection, {'type': 'error', 'error_code': 'analysis_incomplete', 'error': 'incomplete', 'done': True}])):
            response = self.client.post('/api/v1/agent/ask', json={'question': '查询记录数',
                'model': 'qwen-test', 'scope': {'mode': 'selected', 'workbook_ids': ['a']}})
        self.assertEqual(200, response.status_code)
        calls = [c for c in self.conv.add_tool.call_args_list if c.args[0] == 'analysis.rejected']
        self.assertEqual(1, len(calls))
        self.assertEqual(rejection, calls[0].kwargs['detail'])

    def test_contract_packet_is_persisted_with_sse_answer(self):
        self.service.list_workbook_catalog.return_value = [{'workbook_id': 'a', 'display_name': '项目',
            'catalog': {'tables': [{'table_name': 'data', 'display_name': '明细',
                                  'columns': [{'physical_name': 'total', 'display_name': '金额（万元）'}]}]}}]
        actions = [
            ('define_analysis', {'tasks': [{'id': 'money', 'label': '投资总额', 'operation': 'value',
                'source': {'workbook_id': 'a', 'table_name': 'data', 'column': 'total'}}]}),
            ('query_workbook', {'workbook_id': 'a', 'sql': 'SELECT SUM(total) AS total FROM data'}),
            ('terminate', {'bindings': {'money': {'result_id': 'result-1', 'row': 0, 'column': 'total'}}})]
        replies = iter(json.dumps({'action': a, 'action_input': v}, ensure_ascii=False) for a, v in actions)
        self.service.execute_readonly_sql_snapshot.return_value = SimpleNamespace(
            sql='SELECT SUM(total) AS total FROM data', columns=['total'], rows=[[10]], row_count=1, truncated=False)
        llm = SimpleNamespace(model_name='test', analysis_contract=True, structured_control=True,
                              generate_chat=lambda m: next(replies))
        with patch('app.api.endpoints.ExcelLLM', return_value=llm), patch(
                'app.api.endpoints.AgentConversation.create', return_value=self.conv):
            response = self.client.post('/api/v1/agent/ask', json={'question': '查询投资总额',
                'scope': {'mode': 'selected', 'workbook_ids': ['a']}})
        events = [json.loads(l[6:]) for l in response.text.splitlines() if l.startswith('data: ')]
        self.assertEqual('done', events[-1]['type'])
        self.assertIn('万元', events[-1]['full_answer'])
        self.assertEqual(events[-1]['analysis'], self.conv.add_ai.call_args.kwargs['meta']['analysis'])
        self.assertTrue(any(c.args[0] == 'analysis.planned' for c in self.conv.add_tool.call_args_list))

    def test_done_is_not_exposed_before_budget_and_run_slot_are_finalized(self):
        from app.api.endpoints import _sse
        def serialize(event):
            if event.get('type')=='done':
                self.conv.add_budget_usage.assert_called_once()
                self.service.release_agent_run.assert_called_once()
            return _sse(event)
        with patch('app.api.endpoints.ExcelLLM',return_value=SimpleNamespace(model_name='test')), patch('app.api.endpoints.AgentConversation.create',return_value=self.conv), patch(
            'app.api.endpoints.run_multi_workbook_agent',return_value=iter([{'type':'done','full_answer':'ok'}])), patch(
            'app.api.endpoints._sse',side_effect=serialize):
            response=self.client.post('/api/v1/agent/ask',json={'question':'count','scope':{'mode':'selected','workbook_ids':['a']}})
        self.assertNotIn('"type": "error"',response.text)
        self.assertIn('"full_answer": "ok"',response.text)

    def test_request_failure_metadata_reaches_sse(self):
        from app.service.llm import LLMRequestError
        def fail(messages):
            raise LLMRequestError('empty_response','模型未返回正文','diagnostic-test',True)
        llm=SimpleNamespace(model_name='Qwen/test',generate_chat=fail)
        with patch('app.api.endpoints.ExcelLLM',return_value=llm), patch('app.api.endpoints.AgentConversation.create',return_value=self.conv):
            response=self.client.post('/api/v1/agent/ask',json={'question':'count','scope':{'mode':'selected','workbook_ids':['a']}})
        events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        error=events[-1]
        self.assertEqual(error['type'],'error')
        self.assertEqual(error['error_code'],'empty_response')
        self.assertEqual(error['request_id'],'diagnostic-test')
        self.assertTrue(error['retryable'])
        self.assertEqual(llm.conversation_id,self.conv.conv_id)
        self.service.release_agent_run.assert_called_once()

    def test_report_retry_persists_model_html_with_owner_scoped_url(self):
        with TemporaryDirectory() as storage:
            self.service.config.resolved_storage_path = lambda:storage
            replies = iter([
                'Action: query_workbook\nAction Input: {"workbook_id":"a","sql":"SELECT 2 AS total"}',
                'Action: query_workbook\nAction Input: {"workbook_id":"b","sql":"SELECT 3 AS total"}',
                'Action: html_interpreter\nAction Input: {}',
                'Action: html_interpreter\nAction Input: ' + json.dumps({'title':'Cross workbook report',
                    'html':'<style>.kpi{color:red}</style><h1>Model report</h1><p>A 2 + B 3 = 5</p><script>bad()</script>'}),
                'Action: terminate\nAction Input: {"result":"Report generated"}',
            ])
            llm = SimpleNamespace(model_name='Qwen/test', generate_chat=lambda messages:next(replies))
            with patch('app.api.endpoints.ExcelLLM', return_value=llm), patch(
                'app.api.endpoints.AgentConversation.create', return_value=self.conv):
                response = self.client.post('/api/v1/agent/ask', json={
                    'question':'Generate a report', 'scope':{'mode':'selected','workbook_ids':['a','b']}})
            events = [json.loads(l[6:]) for l in response.text.splitlines() if l.startswith('data: ')]
            report = next(e for e in events if e['type']=='html')
            self.assertIn('/agent/conversations/conversation-test/reports/', report['url'])
            self.assertNotIn('script', report['html'])
            self.assertIn('.kpi{color:red}', report['html'])
            self.assertEqual(1, len(list(Path(storage).rglob('*.html'))))
            self.assertEqual(report['url'], events[-1]['report_url'])
            self.assertTrue(any(e['type']=='step.done' and e['status']=='failed' for e in events))
            self.assertTrue(any(c.args[0]=='step.done' for c in self.conv.add_tool.call_args_list))
            with patch('app.api.endpoints.AgentConversation.load', return_value=self.conv):
                html = self.client.get(report['url'])
                self.assertEqual(200, html.status_code)
                self.assertEqual(report['html'], html.text)
                self.assertIn("style-src 'unsafe-inline'", html.headers['content-security-policy'])
            with patch('app.api.endpoints.AgentConversation.load', return_value=None):
                self.assertEqual(404, self.client.get(report['url']).status_code)
                self.assertEqual(404, self.client.get(report['url']+'/pdf').status_code)

    def test_report_request_cannot_finish_without_a_saved_report(self):
        replies = iter([
            'Action: terminate\nAction Input: {"result":"Report generated"}',
            'Action: terminate\nAction Input: {"result":"Report generated"}',
        ])
        self.service.config.agent = {'max_loops':2}
        llm = SimpleNamespace(model_name='Qwen/test', generate_chat=lambda messages:next(replies))
        with patch('app.api.endpoints.ExcelLLM', return_value=llm), patch('app.api.endpoints.AgentConversation.create', return_value=self.conv):
            response = self.client.post('/api/v1/agent/ask', json={'question':'Generate report'})
        events = [json.loads(l[6:]) for l in response.text.splitlines() if l.startswith('data: ')]
        self.assertEqual('error', events[-1]['type'])
        self.assertFalse(any(e['type']=='done' for e in events))

    def setUp(self):
        # This suite mocks conversations; do not depend on another test creating
        # a real metadata database before the first-question title update.
        title_update=patch('app.models.agent.AgentConversationDao.update',return_value=None)
        title_update.start();self.addCleanup(title_update.stop)
        self.service = MagicMock()
        self.service.config = SimpleNamespace(
            llm={'model':'default'}, llms=[{'name':'qwen-test', 'model':'Qwen/test'}],
            agent={'max_loops':8}, multi_workbook_enabled=True,
            multi_workbook_max_selected=5, multi_workbook_max_queried=3,
        )
        self.service.try_acquire_agent_run.return_value = True
        self.service.list_workbook_catalog.return_value = [{'workbook_id':'a'}, {'workbook_id':'b'}]
        self.service.list_dataset_relations.return_value = []
        self.service.active_database_snapshot.side_effect = lambda wid, owner_id=None: {
            'workbook_id':wid, 'display_name':wid, 'import_id':'i-'+wid, 'version':1,
        }
        self.service.execute_readonly_sql_snapshot.side_effect = lambda snapshot, sql, max_rows=500: SimpleNamespace(
            sql=sql, columns=['total'], rows=[[2 if snapshot['workbook_id']=='a' else 3]],
            row_count=1, truncated=False,
        )
        app = FastAPI()
        app.include_router(router, prefix='/api/v1')
        app.dependency_overrides[get_service] = lambda:self.service
        app.dependency_overrides[authenticate_request] = lambda:'owner'
        self.client = TestClient(app)
        self.conv = MagicMock()
        self.conv.conv_id = 'conversation-test'
        self.conv.model_name = 'qwen-test'
        self.conv.get_budget_usage.return_value = {}
        self.conv.history_messages.return_value = []

    def test_selected_model_queries_two_sources_and_persists_aggregation(self):
        replies = iter([
            'Action: query_workbook\nAction Input: {"workbook_id":"a","sql":"SELECT 2 AS total"}',
            'Action: query_workbook\nAction Input: {"workbook_id":"b","sql":"SELECT 3 AS total"}',
            'Action: aggregate_results\nAction Input: {"operation":"sum","value_column":"total"}',
            'Action: terminate\nAction Input: {"result":"Total {{aggregate-1:0:total}}"}',
        ])
        llm = SimpleNamespace(model_name='Qwen/test', generate_chat=lambda messages:next(replies))
        with patch('app.api.endpoints.ExcelLLM', return_value=llm) as factory, patch(
            'app.api.endpoints.AgentConversation.create', return_value=self.conv
        ):
            response = self.client.post('/api/v1/agent/ask', json={
                'question':'Compare totals', 'model':'qwen-test',
                'scope':{'mode':'selected', 'workbook_ids':['a','b']},
            })
        self.assertEqual(200, response.status_code)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        factory.assert_called_once_with(self.service.config.llms[0])
        aggregate = next(e for e in events if e['type']=='aggregate.result')
        self.assertEqual([['5']], aggregate['rows'])
        self.assertEqual('Total 5', events[-1]['full_answer'])
        self.assertEqual({'a','b'}, {s['workbook_id'] for s in events[-1]['sources']})
        self.conv.add_ai.assert_called_once()
        self.assertEqual(self.conv.add_ai.call_args.args,('Total 5',))
        self.assertEqual({s['workbook_id'] for s in self.conv.add_ai.call_args.kwargs['meta']['attachments']},{'a','b'})
        self.assertTrue(any(c.args[0]=='aggregate.result' for c in self.conv.add_tool.call_args_list))
        self.conv.add_budget_usage.assert_called_once()
        self.service.release_agent_run.assert_called_once()

    def test_unknown_model_is_rejected_instead_of_silent_fallback(self):
        response = self.client.post('/api/v1/agent/ask', json={'question':'count','model':'missing'})
        self.assertEqual(422, response.status_code)
        self.service.release_agent_run.assert_called_once()

    def test_followup_restores_saved_model_when_request_omits_model(self):
        llm = SimpleNamespace(model_name='Qwen/test', generate_chat=lambda messages:
            'Action: terminate\nAction Input: {"result":"Please clarify"}')
        with patch('app.api.endpoints.ExcelLLM', return_value=llm) as factory, patch(
            'app.api.endpoints.AgentConversation.load', return_value=self.conv
        ):
            response = self.client.post('/api/v1/agent/ask', json={'question':'follow up','conv_id':'conversation-test'})
        self.assertEqual(200, response.status_code)
        factory.assert_called_once_with(self.service.config.llms[0])

    def test_missing_conversation_preserves_404(self):
        with patch('app.api.endpoints.AgentConversation.load', return_value=None):
            response = self.client.post('/api/v1/agent/ask', json={'question':'count','conv_id':'missing'})
        self.assertEqual(404, response.status_code)

    def test_empty_space_is_rejected_without_querying_other_workbooks(self):
        self.service.get_data_space.return_value = {'workbook_ids':[]}
        with patch('app.api.endpoints.ExcelLLM'), patch('app.api.endpoints.AgentConversation.create', return_value=self.conv):
            response = self.client.post('/api/v1/agent/ask', json={'question':'count', 'scope':{'mode':'space','space_id':'empty'}})
        self.assertEqual(422, response.status_code)
        self.service.active_database_snapshot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
