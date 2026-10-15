import unittest
from types import SimpleNamespace
from unittest.mock import patch
from app.agent.multi_workbook import run_multi_workbook_agent, report_requested


class EvidenceTests(unittest.TestCase):
    def test_report_intent_is_explicit_and_respects_negation(self):
        for question in ('总结文件','国网批次有多少条','解释这份报告','不要生成报告，只给结论','无需报告','do not generate a report'):
            with self.subTest(question=question):self.assertFalse(report_requested(question))
        for question in ('生成国网批次分析报告','国网批次分析报告','给我一份报告','make report','生成 HTML','report'):
            with self.subTest(question=question):self.assertTrue(report_requested(question))

    def test_unrequested_html_does_not_lock_text_answer(self):
        replies=iter(['Action: html_interpreter\nAction Input: {"html":"<html>hello</html>"}',
                      'Action: terminate\nAction Input: {"result":"text summary"}'])
        service=SimpleNamespace(active_database_snapshot=lambda *a,**kw:{'workbook_id':'a','import_id':'i','version':1})
        with patch('app.agent.multi_workbook.tools.html_interpreter') as tool:
            events=list(run_multi_workbook_agent(SimpleNamespace(generate_chat=lambda m:next(replies)),service,
                owner_id='owner',question='总结文件',catalogs=[{'workbook_id':'a'}],conversation_id='c',max_loops=2))
        self.assertEqual(0,tool.call_count)
        self.assertEqual('text summary',events[-1]['full_answer'])
        self.assertFalse(any(e.get('status')=='report_retry' for e in events))
    def run_agent(self, history):
        result = {'result_id':'result-1','source':{'workbook_id':'a','import_id':'i','version':1},
                  'sql':'SELECT 1','columns':['n'],'rows':[[1]],'row_count':1,'truncated':False}
        replies = iter(['Action: html_interpreter\nAction Input: {"html":"<html><body>one</body></html>"}',
                        'Action: terminate\nAction Input: {"result":"done"}'])
        service = SimpleNamespace(active_database_snapshot=lambda *a, **kw: result['source'])
        with patch('app.agent.multi_workbook.tools.html_interpreter', return_value={'report_id':'r','url':'/report','title':'report'}) as tool:
            events = list(run_multi_workbook_agent(SimpleNamespace(generate_chat=lambda m:next(replies)),service,
                owner_id='owner',question='make report',catalogs=[{'workbook_id':'a'}],
                history_messages=[{'role':'tool','content':'previous query','evidence':dict(result,**history)}],
                conversation_id='c',max_loops=2))
        return events, tool.call_count

    def test_previous_successful_query_allows_report(self):
        events,calls=self.run_agent({})
        self.assertEqual(1,calls)
        self.assertTrue(any(e['type']=='html' for e in events))

    def test_changed_version_does_not_allow_report(self):
        events,calls=self.run_agent({'source':{'workbook_id':'a','import_id':'old','version':0}})
        self.assertEqual(0,calls)

    def test_error_and_unauthorized_evidence_cannot_unlock_report(self):
        for change in ({'error':'failed'}, {'source':{'workbook_id':'secret','import_id':'i','version':1}}):
            with self.subTest(change=change):
                self.assertEqual(0,self.run_agent(change)[1])

    def test_empty_successful_query_can_report_no_matching_rows(self):
        self.assertEqual(1,self.run_agent({'rows':[],'row_count':0})[1])

    def test_report_retry_after_query_is_not_blocked_as_duplicate(self):
        html='Action: html_interpreter\nAction Input: {"html":"<html><body>one</body></html>"}'
        replies=iter([html,'Action: observe_execution\nAction Input: {}',
                      'Action: query_workbook\nAction Input: {"workbook_id":"a","sql":"SELECT 1"}',
                      html,'Action: terminate\nAction Input: {"result":"done"}'])
        service=SimpleNamespace(
            active_database_snapshot=lambda *a,**kw:{'workbook_id':'a','import_id':'i','version':1},
            execute_readonly_sql_snapshot=lambda *a,**kw:SimpleNamespace(sql='SELECT 1',columns=['n'],rows=[[1]],row_count=1,truncated=False))
        with patch('app.agent.multi_workbook.tools.html_interpreter',return_value={'report_id':'r','url':'/r'}) as tool:
            events=list(run_multi_workbook_agent(SimpleNamespace(generate_chat=lambda m:next(replies)),service,
                owner_id='owner',question='make report',catalogs=[{'workbook_id':'a'}],conversation_id='c',max_loops=5))
        self.assertEqual(1,tool.call_count)
        self.assertEqual('done',events[-1]['type'])
        state=next(e['result'] for e in events if e.get('type')=='step.done' and e.get('action')=='observe_execution')
        self.assertFalse(state['report_ready'])

    def test_session_retains_structured_success_record(self):
        from app.agent.session import AgentConversation
        conv=AgentConversation.__new__(AgentConversation)
        evidence={'source':{'workbook_id':'a','version':1,'import_id':'i'},'columns':['n'],'rows':[[1]]}
        conv.messages=[{'role':'tool','round_index':1,'content':'query',
            'meta':{'detail':{'type':'step.done','action':'query_workbook','status':'done','result':evidence}}}]
        self.assertEqual(evidence,conv.history_messages()[0]['evidence'])


if __name__=='__main__':unittest.main()
