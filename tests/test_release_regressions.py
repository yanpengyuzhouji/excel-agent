import json
import unittest
from types import SimpleNamespace as N
from unittest.mock import patch
from app.agent.context_window import ContextWindow, estimate_tokens
from app.agent.multi_workbook import _observation
from app.agent.multi_workbook import weighted_unit_contract, normalize_weighted_unit, _internal_metadata_query


class ReleaseRegressions(unittest.TestCase):
    def test_whitespace_is_not_partial_answer_and_does_not_block_recovery(self):
        from app.service.llm import ExcelLLM
        with patch('openai.OpenAI') as factory:
            make=factory.return_value.chat.completions.create
            make.side_effect=[[N(choices=[N(delta=N(content=' \n',reasoning_content='private'),finish_reason='stop')])],
                              [N(choices=[N(delta=N(content='ok'),finish_reason='stop')])]]
            llm=ExcelLLM({'api_key':'test'})
            self.assertEqual(list(llm.generate_chat_stream([])),['ok'])
            self.assertEqual(make.call_count,2)

    def test_single_json_action_envelope_is_not_mistaken_for_answer(self):
        from app.agent.react_loop import parse_react,has_tool_protocol
        value=json.dumps({'action':'query_workbook','action_input':{'workbook_id':'a','sql':'SELECT 1'}})
        self.assertEqual(parse_react(value),('query_workbook',{'workbook_id':'a','sql':'SELECT 1'}))
        self.assertTrue(has_tool_protocol(value))

    def test_flat_tool_arguments_are_not_silently_discarded(self):
        from app.agent.react_loop import parse_react
        value=json.dumps({'action':'query_workbook','workbook_id':'a','sql':'SELECT 1'})
        self.assertEqual(parse_react(value),('query_workbook',{'workbook_id':'a','sql':'SELECT 1'}))
        mixed=json.dumps({'action':'query_workbook','action_input':{'workbook_id':'a'},'sql':'SELECT 1'})
        self.assertEqual(parse_react(mixed),('query_workbook',{'workbook_id':'a','sql':'SELECT 1'}))

    def test_conflicting_nested_and_flat_arguments_are_rejected(self):
        from app.agent.react_loop import parse_react
        value=json.dumps({'action':'query_workbook','action_input':{'sql':'SELECT 1'},'sql':'SELECT 2'})
        self.assertEqual(parse_react(value),(None,{}))

    def test_structured_controller_cannot_publish_arbitrary_json_as_answer(self):
        from app.agent.multi_workbook import run_multi_workbook_agent
        llm=N(structured_control=True,generate_chat=lambda m:'{"answer":42}')
        service=N(active_database_snapshot=lambda *a,**kw:{'workbook_id':'a','import_id':'i','version':1})
        events=list(run_multi_workbook_agent(llm,service,owner_id='owner',question='count',catalogs=[{'workbook_id':'a'}],max_loops=3))
        self.assertEqual(events[-1]['type'],'error')
        self.assertFalse(any(e['type']=='done' for e in events))

    def test_recovery_cannot_bypass_agent_call_budget(self):
        from app.service.llm import ExcelLLM
        from app.agent.budget import AgentBudget, BudgetExceeded
        with patch('openai.OpenAI') as factory:
            make=factory.return_value.chat.completions.create
            make.return_value=[N(choices=[N(delta=N(content=None),finish_reason='stop')])]
            llm=ExcelLLM({'api_key':'test'})
            llm.retry_budget=AgentBudget(max_llm_calls=1)
            llm.retry_budget.before_llm([])
            with self.assertRaises(BudgetExceeded):llm.generate_chat([])
            self.assertEqual(make.call_count,1)

    def test_weighted_unit_comes_from_source_not_model_guess(self):
        catalogs=[{'catalog':{'tables':[{'columns':[{'source_column':'H','unit_hint':'投资（万元）'},{'source_column':'N','unit_hint':'数量（台）'}]}]}}]
        contract=weighted_unit_contract('SUM(H*N)/SUM(N)',catalogs)
        self.assertEqual(contract['result_unit'],'万元')
        self.assertEqual(normalize_weighted_unit('结果176.7987万元/台',contract),'结果176.7987万元')
        self.assertEqual(normalize_weighted_unit('单位不是万元/台',contract),'单位不是万元/台')
        self.assertIsNone(weighted_unit_contract('SUM(H)/SUM(N)',catalogs))

    def test_internal_table_block_does_not_block_business_literal(self):
        self.assertTrue(_internal_metadata_query('SELECT * FROM _dbgpt_import_manifest'))
        self.assertFalse(_internal_metadata_query("SELECT * FROM table_1 WHERE name='sqlite_master'"))
    def test_empty_stream_recovers_once_without_replaying_text(self):
        from app.service.llm import ExcelLLM
        with patch('openai.OpenAI') as factory:
            make=factory.return_value.chat.completions.create
            make.side_effect=[[N(choices=[N(delta=N(content=None),finish_reason='stop')])],
                              [N(choices=[N(delta=N(content='recovered'),finish_reason='stop')])]]
            llm=ExcelLLM({'api_key':'test'})
            self.assertEqual(llm.generate_chat([]),'recovered')
            self.assertEqual(make.call_count,2)

    def test_first_body_timeout_recovers_on_fresh_sdk_client(self):
        from app.service.llm import ExcelLLM, _RequestTimeout
        with patch('openai.OpenAI') as factory:
            first=factory.return_value
            second=N(chat=N(completions=N(create=lambda **kw:[N(choices=[N(delta=N(content='ok'),finish_reason='stop')])])),close=lambda:None)
            factory.side_effect=[first,second]
            first.chat.completions.create.side_effect=_RequestTimeout('first_content_timeout')
            llm=ExcelLLM({'api_key':'test'})
            self.assertEqual(llm.generate_chat([]),'ok')
            self.assertEqual(factory.call_count,2)

    def test_observation_is_valid_json_and_keeps_table_mapping(self):
        tables=[{'table_name':f'table_{i}','display_name':f'region-{i}','columns':[{'physical_name':f'column_{c}','display_name':'x'*300} for c in range(40)]} for i in range(14)]
        text=_observation({'tables':tables})
        data=json.loads(text)
        self.assertEqual([t['display_name'] for t in tables],[t['display_name'] for t in data['tables']])
        self.assertLessEqual(len(text),16000)

    def test_bounded_schema_does_not_hide_later_columns(self):
        tables=[{'table_name':f'table_{i}','display_name':f'region-{i}','columns':[{'physical_name':f'column_{c}','display_name':'x'*300} for c in range(40)]} for i in range(14)]
        data=json.loads(_observation({'tables':tables}))
        self.assertIn('column_39',json.dumps(data['tables'][0]['columns']))

    def test_explicit_nonempty_column_scope_does_not_force_business_count_gate(self):
        from app.agent.multi_workbook import requires_business_count_verification
        self.assertFalse(requires_business_count_verification('列出E列非空记录总数及H列投资总额'))
        self.assertTrue(requires_business_count_verification('一共有多少采购批次记录'))
        self.assertFalse(requires_business_count_verification('严重程度=疑似的数量合计是多少？解释来源不一致，不自行改写原值。'))

    def test_counting_gate_stops_repeated_termination_before_budget_exhaustion(self):
        from app.agent.multi_workbook import run_multi_workbook_agent
        calls=[]
        def reply(messages):
            calls.append(1)
            return 'Action: terminate\nAction Input: {"result":"unverified"}'
        llm=N(generate_chat=reply)
        service=N(active_database_snapshot=lambda *a,**kw:{'workbook_id':'a','import_id':'i','version':1})
        events=list(run_multi_workbook_agent(llm,service,owner_id='owner',question='有多少批次记录',catalogs=[{'workbook_id':'a'}],max_loops=100))
        self.assertEqual(len(calls),3)
        self.assertEqual(events[-1]['type'],'error')
        self.assertIn('counting scope',events[-1]['error'])

    def test_mandatory_context_that_fits_is_not_rejected_by_soft_target(self):
        question={'role':'user','content':'q'*6000}
        messages=[{'role':'system','content':'s'*3000},{'role':'user','content':'old'*10000},question]
        window=ContextWindow(4096,512)
        window.prepare(messages,question)
        self.assertLessEqual(estimate_tokens(messages),window.input_limit)


if __name__=='__main__':unittest.main()
