import unittest
from types import SimpleNamespace
from app.agent.budget import AgentBudget
from app.agent.general_chat import run_general_chat


class GeneralChatTests(unittest.TestCase):
    def test_tool_history_excluded_and_question_preserved(self):
        captured = []
        def stream(messages):
            captured.extend(messages)
            yield '回答'
        history = [{'role': 'tool', 'content': 'SECRET SQL evidence'}, {'role': 'user', 'content': '以前的问题'}]
        events = list(run_general_chat(SimpleNamespace(generate_chat_stream=stream), '当前问题', history, AgentBudget()))
        self.assertFalse(any('SECRET' in m['content'] for m in captured))
        self.assertEqual(captured[-1]['content'], '当前问题')
        self.assertEqual(events[-1]['sources'], [])
        self.assertFalse(any(e['type'].startswith('step.') for e in events))

    def test_compaction_and_budget_without_tool_calls(self):
        budget = AgentBudget()
        events = list(run_general_chat(SimpleNamespace(generate_chat_stream=lambda m: iter(['回答'])),
            '当前问题', [{'role': 'user', 'content': '历史'*20000}], budget, total_tokens=4096, output_reserve=1024))
        self.assertTrue(any(e['type'] == 'context.compacted' for e in events))
        self.assertEqual(budget.tool_calls, 0)
        self.assertEqual(budget.sql_calls, 0)
