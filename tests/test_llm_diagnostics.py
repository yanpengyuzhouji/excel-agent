import json
import unittest
import time
from types import SimpleNamespace as N
from unittest.mock import patch
import httpx
import openai
from app.service.llm import ExcelLLM, LLMRequestError, llm_error_event


def chunk(content=None, finish='stop', **extra):
    return N(choices=[N(delta=N(content=content, **extra), finish_reason=finish)])


class DiagnosticsTests(unittest.TestCase):
    @patch('openai.OpenAI')
    def test_structured_mode_is_only_for_deepseek_control_steps(self,factory):
        factory.return_value.chat.completions.create.return_value=[chunk('ok')]
        llm=ExcelLLM({'api_key':'test','provider':'deepseek'})
        llm.request_phase='agent_control';llm.generate_chat([])
        self.assertEqual(factory.return_value.chat.completions.create.call_args.kwargs['response_format'],{'type':'json_object'})
        self.assertEqual(factory.return_value.chat.completions.create.call_args.kwargs['extra_body']['thinking'],{'type':'disabled'})
        llm.request_phase='final_answer';llm.generate_chat([])
        self.assertNotIn('response_format',factory.return_value.chat.completions.create.call_args.kwargs)
        self.assertNotIn('extra_body',factory.return_value.chat.completions.create.call_args.kwargs)

    def client(self, chunks=None, error=None):
        with patch('openai.OpenAI') as factory:
            llm=ExcelLLM({'model':'test-model','api_key':'secret-key','empty_response_retries':0})
            factory.return_value.chat.completions.create.return_value=chunks or []
            factory.return_value.chat.completions.create.side_effect=error
        llm.conversation_id='test-conversation'
        return llm

    def failure(self, llm, code):
        with self.assertLogs('app.service.llm',level='WARNING') as logs:
            with self.assertRaises(LLMRequestError) as caught:
                llm.generate_chat([{'role':'user','content':'private-business-question'}])
        error=caught.exception
        self.assertEqual(error.code,code)
        event=llm_error_event(error)
        self.assertEqual(event['request_id'],error.request_id)
        self.assertEqual(event['error_code'],code)
        joined='\n'.join(logs.output)
        for secret in ('secret-key','private-business-question','provider-secret','secret-reasoning'):
            self.assertNotIn(secret,joined+str(error))
        stats=json.loads(logs.records[-1].getMessage().split('llm_request ',1)[1])
        self.assertEqual(stats['request_id'],event['request_id'])
        self.assertEqual(stats['conv_id'],'test-conversation')
        return error,stats

    def test_empty_normal_stop_is_not_mislabelled_route_failure(self):
        error,stats=self.failure(self.client([chunk()]),'empty_response')
        self.assertEqual(stats['finish_reason'],'stop')
        self.assertEqual(stats['chunks'],1)
        self.assertNotIn('healthy inference route',str(error))

    def test_reasoning_only_is_counted_not_logged_or_exposed(self):
        _,stats=self.failure(self.client([chunk(reasoning_content='secret-reasoning')]),'reasoning_only')
        self.assertEqual(stats['reasoning_chars'],16)

    def test_tool_call_protocol_is_distinguished(self):
        _,stats=self.failure(self.client([chunk(tool_calls=[N(arguments='provider-secret')])]),'unexpected_tool_calls')
        self.assertEqual(stats['tool_call_chunks'],1)

    def test_whitespace_is_not_a_visible_answer(self):
        self.failure(self.client([chunk(' \n\t')]),'empty_response')

    def test_output_limit_and_safety_filter(self):
        for reason,code in [('length','output_limit'),('content_filter','content_filtered')]:
            with self.subTest(reason=reason):self.failure(self.client([chunk(finish=reason)]),code)

    def test_http_failures_are_sanitized_and_classified(self):
        for status,code in [(401,'authentication_failed'),(403,'authentication_failed'),(402,'quota_exhausted'),
                            (429,'rate_limited'),(503,'upstream_unavailable'),(400,'request_rejected')]:
            with self.subTest(status=status):
                response=httpx.Response(status,request=httpx.Request('POST','https://example.invalid'))
                error=openai.APIStatusError('provider-secret',response=response,body={'error':'provider-secret'})
                _,stats=self.failure(self.client(error=error),code)
                self.assertEqual(stats['http_status'],status)

    def test_connection_failure(self):
        error=openai.APIConnectionError(request=httpx.Request('POST','https://example.invalid'))
        self.failure(self.client(error=error),'connection_failed')

    def test_bailian_arrearage_400_is_billing_not_parameter_failure(self):
        response=httpx.Response(400,request=httpx.Request('POST','https://example.invalid'))
        error=openai.APIStatusError('provider-secret',response=response,
            body={'error': {'code': 'Arrearage', 'message': 'provider-secret'}})
        public, stats=self.failure(self.client(error=error),'quota_exhausted')
        self.assertEqual('Arrearage',stats['provider_error_code'])
        self.assertFalse(public.retryable)

    def test_bailian_free_tier_403_is_quota_not_invalid_key(self):
        response=httpx.Response(403,request=httpx.Request('POST','https://example.invalid'))
        error=openai.APIStatusError('provider-secret',response=response,
            body={'error':{'code':'AllocationQuota.FreeTierOnly','message':'provider-secret'}})
        _,stats=self.failure(self.client(error=error),'quota_exhausted')
        self.assertEqual('AllocationQuota.FreeTierOnly',stats['provider_error_code'])

    def test_partial_text_is_not_replayed(self):
        def stream():
            yield chunk('first',finish=None)
            raise openai.APIConnectionError(request=httpx.Request('POST','https://example.invalid'))
        llm=self.client(stream())
        output=llm.generate_chat_stream([])
        self.assertEqual(next(output),'first')
        with self.assertLogs('app.service.llm',level='WARNING'):
            with self.assertRaises(LLMRequestError) as error:next(output)
        self.assertTrue(error.exception.partial_output)
        self.assertTrue(llm_error_event(error.exception)['partial_output'])

    def test_success_and_usage_only_chunk(self):
        llm=self.client([chunk('hello'),N(choices=[],usage=N(prompt_tokens=10,completion_tokens=2,total_tokens=12))])
        with self.assertLogs('app.service.llm',level='INFO') as logs:
            self.assertEqual(llm.generate_chat([]),'hello')
        stats=json.loads(logs.records[-1].getMessage().split('llm_request ',1)[1])
        self.assertEqual(stats['outcome'],'success')
        self.assertEqual(stats['usage']['total_tokens'],12)
        self.assertEqual(stats['content_chars'],5)

    def test_stream_http_status_and_provider_id_are_correlated(self):
        class Stream:
            response=N(status_code=200,headers={'x-request-id':'upstream-123','authorization':'secret-key'})
            def __iter__(self):return iter([chunk('hello')])
            def close(self):pass
        llm=self.client(Stream())
        with self.assertLogs('app.service.llm',level='INFO') as logs:
            self.assertEqual(llm.generate_chat([]),'hello')
        text=logs.records[-1].getMessage()
        stats=json.loads(text.split('llm_request ',1)[1])
        self.assertEqual(stats['http_status'],200)
        self.assertEqual(stats['provider_request_id'],'upstream-123')
        self.assertNotIn('secret-key',text)

    def test_timeout_preserves_already_received_http_metadata(self):
        class Stream:
            response=N(status_code=200,headers={'x-request-id':'upstream-slow'})
            def __iter__(self):
                yield N(choices=[])
                time.sleep(.1)
            def close(self):pass
        llm=self.client(Stream())
        llm.first_token_timeout=.02
        _,stats=self.failure(llm,'first_content_timeout')
        self.assertEqual(stats['http_status'],200)
        self.assertEqual(stats['provider_request_id'],'upstream-slow')


if __name__=='__main__':unittest.main()
