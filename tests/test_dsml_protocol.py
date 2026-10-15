import unittest
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
from app.agent.react_loop import parse_react, public_answer
from app.agent.multi_workbook import run_multi_workbook_agent, _verify_record_count
from app.agent.session import AgentConversation


def dsml(sql='SELECT 1'):
    return ('<｜DSML｜ calls><｜DSML｜ invoke name="query_workbook">'
            '<｜DSML｜ parameter name="workbook_id" string="true">a</｜DSML｜ parameter>'
            f'<｜DSML｜ parameter name="sql" string="true">{sql}</｜DSML｜ parameter>'
            '</｜DSML｜ invoke></｜DSML｜ calls>')


class DSMLTests(unittest.TestCase):
    def test_parse_actual_protocol(self):
        self.assertEqual(('query_workbook',{'workbook_id':'a','sql':'SELECT 1'}),parse_react(dsml()))

    def test_protocol_is_not_public_answer(self):
        self.assertEqual('',public_answer(dsml()))

    def test_polluted_assistant_history_is_filtered(self):
        conv=AgentConversation.__new__(AgentConversation)
        conv.messages=[{'role':'ai','round_index':1,'content':dsml(),'meta':{}},
                       {'role':'user','round_index':2,'content':'How many?','meta':{}}]
        self.assertEqual(['user'],[m['role'] for m in conv.history_messages()])

    def test_real_loop_dispatches_query_before_answer(self):
        replies=iter([dsml(), 'Action: terminate\nAction Input: {"result":"one"}'])
        calls=[]
        def query(*args,**kwargs):
            calls.append(args)
            return SimpleNamespace(sql='SELECT 1',columns=['n'],rows=[[1]],row_count=1,truncated=False)
        service=SimpleNamespace(active_database_snapshot=lambda *a,**kw:{'workbook_id':'a','import_id':'i','version':1},execute_readonly_sql_snapshot=query)
        events=list(run_multi_workbook_agent(SimpleNamespace(generate_chat=lambda m:next(replies)),service,
            owner_id='owner',question='test',catalogs=[{'workbook_id':'a'}],max_loops=3))
        self.assertEqual(1,len(calls))
        self.assertEqual('one',events[-1]['full_answer'])

    def test_multiple_or_truncated_calls_fail_closed(self):
        for text in (dsml()[:-10], dsml().replace('</｜DSML｜ calls>',dsml().split('<｜DSML｜ calls>')[1])):
            with self.subTest(text=text):self.assertEqual((None,{}),parse_react(text))

    def test_old_token_variant_and_json_arguments(self):
        text=dsml().replace('｜DSML｜ ','｜DSML｜').replace('calls>','tool_calls>')
        self.assertEqual('query_workbook',parse_react(text)[0])
        text='<｜DSML｜ calls><｜DSML｜ invoke name="aggregate_results"><｜DSML｜ parameter name="result_ids" string="false">["result-1"]</｜DSML｜ parameter></｜DSML｜ invoke></｜DSML｜ calls>'
        self.assertEqual(['result-1'],parse_react(text)[1]['result_ids'])

    def test_raw_sql_comparisons_and_html_are_preserved(self):
        self.assertEqual('SELECT 1 WHERE 1 < 2',parse_react(dsml('SELECT 1 WHERE 1 < 2'))[1]['sql'])
        html='<!DOCTYPE html><html><body><h1>Report & data</h1></body></html>'
        text=f'<｜DSML｜ calls><｜DSML｜ invoke name="html_interpreter"><｜DSML｜ parameter name="html" string="true">{html}</｜DSML｜ parameter></｜DSML｜ invoke></｜DSML｜ calls>'
        self.assertEqual(html,parse_react(text)[1]['html'])

    def test_single_workbook_uses_the_shared_parser(self):
        from app.agent.react_loop import run_react
        replies=iter([dsml().replace('query_workbook','sql_query'), 'Action: terminate\nAction Input: {"result":"one"}'])
        with patch('app.agent.react_loop.build_database_context',return_value='schema'), patch(
            'app.agent.react_loop.tools.sql_query',return_value={'sql':'SELECT 1','columns':['n'],'rows':[[1]],'row_count':1,'truncated':False}) as query:
            events=list(run_react(SimpleNamespace(generate_chat=lambda m:next(replies)),SimpleNamespace(),'a','test',max_loops=3))
        self.assertEqual(1,query.call_count)
        self.assertEqual('one',events[-1]['full_answer'])

    def test_bad_protocol_retries_then_errors_without_answer(self):
        service=SimpleNamespace(active_database_snapshot=lambda *a,**kw:{'workbook_id':'a','import_id':'i','version':1})
        events=list(run_multi_workbook_agent(SimpleNamespace(generate_chat=lambda m:dsml()[:-10]),service,
            owner_id='owner',question='test',catalogs=[{'workbook_id':'a'}],max_loops=8))
        self.assertEqual('error',events[-1]['type'])
        self.assertFalse(any(e['type']=='done' for e in events))
        self.assertEqual(2,sum(e.get('status')=='protocol_retry' for e in events))

    def test_dsml_still_enforces_source_authorization(self):
        replies=iter([dsml().replace('>a<','>secret<'),'Action: terminate\nAction Input: {"result":"cannot access"}'])
        service=SimpleNamespace(active_database_snapshot=lambda *a,**kw:{'workbook_id':'a','import_id':'i','version':1})
        events=list(run_multi_workbook_agent(SimpleNamespace(generate_chat=lambda m:next(replies)),service,
            owner_id='owner',question='test',catalogs=[{'workbook_id':'a'}],max_loops=3))
        step=next(e for e in events if e['type']=='step.done')
        self.assertEqual('failed',step['status'])
        self.assertIn('authorized',step['result']['error'])

    def test_count_scope_detects_repeated_note_row_without_changing_data(self):
        connection=sqlite3.connect(':memory:')
        try:
            connection.execute('CREATE TABLE table_1 (sequence_no TEXT,column_b TEXT,column_c TEXT)')
            connection.executemany('INSERT INTO table_1 VALUES (?,?,?)', [('1','2月','SG1'),('2','3月','SG2'),('备注：仅说明','备注：仅说明','备注：仅说明')])
            def query(snapshot,sql,**kwargs):
                cursor=connection.execute(sql);rows=cursor.fetchall()
                return SimpleNamespace(sql=sql,columns=[x[0] for x in cursor.description],rows=rows,row_count=len(rows),truncated=False)
            service=SimpleNamespace(execute_readonly_sql_snapshot=query)
            snapshot={'workbook_id':'a','import_id':'i','version':1}
            catalog={'catalog':{'tables':[{'table_name':'table_1','columns':[{'physical_name':x} for x in ('sequence_no','column_b','column_c')]}]}}
            result=_verify_record_count(service,snapshot,catalog,'table_1')
            self.assertEqual(3,result['counting_check']['total_rows'])
            self.assertEqual(1,result['counting_check']['suspected_nonbusiness_rows'])
            self.assertEqual(2,result['counting_check']['candidate_business_rows'])
            self.assertEqual(3,connection.execute('SELECT COUNT(*) FROM table_1').fetchone()[0])
            with self.assertRaisesRegex(ValueError,'authorized'):_verify_record_count(service,snapshot,catalog,'secret')
            replies=iter(['Action: query_workbook\nAction Input: {"workbook_id":"a","sql":"SELECT COUNT(*) AS n FROM table_1"}',
                          'Action: terminate\nAction Input: {"result":"3批"}',
                          'Action: verify_record_count\nAction Input: {"workbook_id":"a","table_name":"table_1"}',
                          'Action: terminate\nAction Input: {"result":"3行，2批，1行备注"}'])
            service.active_database_snapshot=lambda *a,**kw:snapshot
            events=list(run_multi_workbook_agent(SimpleNamespace(generate_chat=lambda m:next(replies)),service,
                owner_id='owner',question='有多少批',catalogs=[dict(catalog,workbook_id='a')],max_loops=5))
            self.assertEqual('3行，2批，1行备注',events[-1]['full_answer'])
            self.assertTrue(any(e.get('status')=='counting_review' for e in events))
        finally:connection.close()


if __name__=='__main__':unittest.main()
