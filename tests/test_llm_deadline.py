import threading
import time
import unittest
from types import SimpleNamespace
from app.service.llm import ExcelLLM


class DeadlineTests(unittest.TestCase):
    def test_first_content_timeout_ignores_heartbeats(self):
        llm = ExcelLLM({'model': 'stub', 'timeout': 1, 'first_token_timeout': .05})
        llm._client.close()
        stopped = threading.Event()
        def chunks():
            while not stopped.wait(.005):
                yield SimpleNamespace(choices=[])
        llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: chunks())), close=stopped.set)
        try:
            with self.assertRaisesRegex(TimeoutError, '未返回正文'):
                llm.generate_chat([{'role': 'user', 'content': 'hello'}])
        finally:
            stopped.set()

    def test_first_content_does_not_limit_rest_of_answer(self):
        llm = ExcelLLM({'model': 'stub', 'timeout': 1, 'first_token_timeout': .05})
        llm._client.close()
        def chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='hello'), finish_reason=None)])
            time.sleep(.1)
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=' world'), finish_reason='stop')])
        llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: chunks())), close=lambda: None)
        self.assertEqual(llm.generate_chat([]), 'hello world')

    def test_stream_heartbeats_cannot_extend_deadline(self):
        llm = ExcelLLM({'model': 'stub', 'timeout': .05, 'stream': True})
        llm._client.close()
        stopped = threading.Event()
        def chunks():
            while not stopped.wait(.005):
                yield SimpleNamespace(choices=[])
        llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: chunks())), close=stopped.set)
        try:
            with self.assertRaises(TimeoutError):
                list(llm.generate_chat_stream([{'role': 'user', 'content': 'hello'}]))
        finally:
            stopped.set()

    def test_blocked_provider_has_absolute_deadline(self):
        llm = ExcelLLM({'model': 'stub', 'timeout': .05})
        llm._client.close()
        released = threading.Event()
        def create(**kwargs):
            released.wait(1)
            return SimpleNamespace(choices=[])
        llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=released.set)
        start = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                llm.generate_chat([{'role': 'user', 'content': 'hello'}])
            self.assertLess(time.monotonic()-start, .5)
        finally:
            released.set()
