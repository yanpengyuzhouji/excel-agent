import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.agent.multi_workbook import run_multi_workbook_agent
from app.agent.react_loop import run_react
from app.agent.loop_guard import ToolLoopGuard


class ToolLoopTests(unittest.TestCase):
    def test_alternating_duplicates_and_new_arguments(self):
        guard = ToolLoopGuard()
        self.assertIsNone(guard.check('query', {'sql':'SELECT 1;'}))
        self.assertEqual('skip',guard.check('query', {'sql':' SELECT 1 '}))
        self.assertIsNone(guard.check('query', {'sql':'SELECT 2'}))
        self.assertEqual('skip',guard.check('query', {'sql':'SELECT 1'}))
        self.assertEqual('skip',guard.check('query', {'sql':'SELECT 2'}))
        self.assertEqual('stop',guard.check('query', {'sql':'SELECT 1'}))
        self.assertIsNone(ToolLoopGuard().check('query', {'sql':'SELECT 1'}))

    def test_single_workbook_repeated_query_can_recover_to_answer(self):
        replies = iter([
            'Action: sql_query\nAction Input: {"sql":"SELECT 1"}',
            'Action: sql_query\nAction Input: {"sql":"SELECT 1"}',
            'Action: terminate\nAction Input: {"result":"one"}',
        ])
        llm = SimpleNamespace(generate_chat=lambda messages: next(replies))
        with patch('app.agent.react_loop.build_database_context', return_value='schema'), patch(
            'app.agent.react_loop.tools.sql_query', return_value={'sql':'SELECT 1','columns':['n'],'rows':[[1]],'row_count':1,'truncated':False}
        ) as query:
            events = list(run_react(llm, SimpleNamespace(), 'a','count',max_loops=5))
        self.assertEqual(1,query.call_count)
        self.assertTrue(any(e.get('status') == 'loop_guard' for e in events))
        self.assertEqual('done',events[-1]['type'])

    def test_repeated_query_executes_only_once(self):
        calls = []
        messages_seen = []
        def generate(messages):
            messages_seen.append(list(messages))
            return 'Action: query_workbook\nAction Input: {"workbook_id":"a","sql":"SELECT 1"}'
        def query(*args, **kwargs):
            calls.append(args)
            return SimpleNamespace(sql='SELECT 1', columns=['n'], rows=[[1]], row_count=1, truncated=False)
        service = SimpleNamespace(
            active_database_snapshot=lambda *args, **kwargs: {'workbook_id':'a','import_id':'i','version':1},
            execute_readonly_sql_snapshot=query,
        )
        events = list(run_multi_workbook_agent(SimpleNamespace(generate_chat=generate), service,
            owner_id='owner', question='count', catalogs=[{'workbook_id':'a'}], max_loops=10))
        self.assertEqual(1, len(calls))
        self.assertLessEqual(len(messages_seen), 4)
        self.assertEqual('error', events[-1]['type'])
        self.assertIn('loop', events[-1]['error'].lower())
        self.assertTrue(any('multi-workbook-observation' in m['content'] for m in messages_seen[1]))


if __name__ == '__main__':
    unittest.main()
