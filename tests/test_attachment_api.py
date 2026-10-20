import io
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from fastapi.testclient import TestClient
from openpyxl import Workbook
from app import db
from app.config import AppConfig
from app.main import app


class AttachmentApiTests(unittest.TestCase):
    def cached_csv(self, conv=None, content=b'item,amount\nA,10\nB,20\n', name='sample.csv'):
        response = self.client.post(f'/api/v1/agent/conversations/{conv or self.conv}/attachments',
                                   files={'file': (name, content)})
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def other_conversation(self):
        return self.client.post('/api/v1/agent/conversations', json={'title':'reuse test'}).json()['conv_id']

    def test_identical_upload_reuses_and_last_reference_cleans(self):
        from pathlib import Path
        first = self.cached_csv()
        other = self.other_conversation()
        with patch('app.service.service.parse_workbook', side_effect=AssertionError('must not reparse')):
            second = self.cached_csv(other, name='renamed.csv')
        self.assertFalse(first['reused'])
        self.assertTrue(second['reused'])
        self.assertEqual(first['workbook_id'], second['workbook_id'])
        self.assertEqual('renamed.csv', self.client.get(f'/api/v1/agent/conversations/{other}').json()['scope']['attachments'][0]['filename'])
        path = Path(self.temp.name)/first['workbook_id']
        self.client.delete(f'/api/v1/agent/conversations/{self.conv}')
        self.assertTrue(path.exists())
        self.client.delete(f'/api/v1/agent/conversations/{other}')
        self.assertFalse(path.exists())
        from app.models.workbook import AttachmentCacheEntity
        with app.state.excel_service._import_dao.session() as session:
            self.assertEqual(0,session.query(AttachmentCacheEntity).count())

    def test_same_conversation_reupload_is_idempotent(self):
        first = self.cached_csv()
        app.state.excel_service.config.multi_workbook_max_selected = 1
        second = self.cached_csv()
        self.assertEqual(first['workbook_id'],second['workbook_id'])
        scope = self.client.get(f'/api/v1/agent/conversations/{self.conv}').json()['scope']
        self.assertEqual(1,len(scope['attachments']))

    def test_manual_warning_acceptance_never_bypasses_another_upload(self):
        content = '项目,金额\n甲,10\n乙,20\n合计,30\n'.encode('utf-8')
        first = self.cached_csv(content=content)
        self.assertEqual('needs_review',first['status'])
        response=self.client.post(f"/api/v1/agent/conversations/{self.conv}/attachments/{first['import_id']}/confirm",
            json={'revision':first['revision'],'accept_severe_warnings':True})
        self.assertEqual(200,response.status_code,response.text)
        second=self.cached_csv(self.other_conversation(),content)
        self.assertEqual('needs_review',second['status'])
        self.assertFalse(second['reused'])
        self.assertNotEqual(first['workbook_id'],second['workbook_id'])

    def test_changed_content_or_parser_config_is_not_reused(self):
        first = self.cached_csv()
        changed = self.cached_csv(self.other_conversation(), b'item,amount\nA,11\nB,20\n')
        self.assertNotEqual(first['workbook_id'],changed['workbook_id'])
        app.state.excel_service.config.max_import_rows -= 1
        configured = self.cached_csv(self.other_conversation())
        self.assertFalse(configured['reused'])
        self.assertNotEqual(first['workbook_id'],configured['workbook_id'])

    def test_concurrent_equal_uploads_publish_one_workbook(self):
        from concurrent.futures import ThreadPoolExecutor
        conversations = [self.other_conversation(),self.other_conversation()]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.cached_csv,conversations))
        self.assertEqual(1,len({r['workbook_id'] for r in results}))
        self.assertEqual([False,True],sorted(r['reused'] for r in results))

    def test_corrupt_cached_file_is_not_reused_and_old_book_stays_immutable(self):
        from pathlib import Path
        first = self.cached_csv()
        service = app.state.excel_service
        entity = service._load_entity(first['import_id'])
        Path(entity.database_path).write_bytes(b'broken cache')
        second = self.cached_csv(self.other_conversation())
        self.assertFalse(second['reused'])
        self.assertNotEqual(first['workbook_id'],second['workbook_id'])
        with self.assertRaisesRegex(ValueError,'immutable'):
            service.create_import('sample.csv',b'item,amount\nA,12\n','agent',first['workbook_id'],'anonymous')

    def test_cache_identity_includes_owner_and_parser_dependencies(self):
        from app.service.attachment_cache import cache_key
        service=app.state.excel_service
        first=cache_key(service,'owner-a','hash','file.csv')
        self.assertNotEqual(first,cache_key(service,'owner-b','hash','file.csv'))
        with patch('app.service.attachment_cache.version',return_value='changed-reader'):
            self.assertNotEqual(first,cache_key(service,'owner-a','hash','file.csv'))

    def test_retained_note_and_repeated_header_are_automatically_ready(self):
        item = self.upload_rows([['项目','金额'],['甲',10],['乙',20],
                                 ['项目','金额'],['备注：合计3类设备；1*35；2025/03/01']])
        self.assertEqual('ready', item['status'], item)
        self.assertEqual([], item['severe_warnings'])
        warning = next(w for w in item['warnings'] if w['code'] == 'NONBUSINESS_ROWS')
        self.assertIs(True, warning['auto_confirm_safe'])
        self.assertEqual(2, warning['count'])
        catalog = next(c for c in self.client.get('/api/v1/data-catalog').json()
                       if c['workbook_id'] == item['workbook_id'])
        self.assertEqual(2, catalog['catalog']['tables'][0]['row_count'])
        self.assertEqual(2, catalog['catalog']['tables'][0]['excluded_row_count'])

    def test_partitioned_summary_is_automatically_ready_and_queryable(self):
        item=self.upload_rows([['=== 概览 ==='],['总文件数','1,234'],[],
                               ['=== 后缀分布 ==='],['后缀','数量'],['.pdf',1200],['.xlsx',34]])
        self.assertEqual('ready',item['status'],item)
        self.assertEqual([],item.get('severe_warnings',[]))
        catalog=next(c for c in self.client.get('/api/v1/data-catalog').json() if c['workbook_id']==item['workbook_id'])
        tables=catalog['catalog']['tables']
        self.assertEqual([1,2],[t['row_count'] for t in tables])
        result=self.client.post(f"/api/v1/workbooks/{item['workbook_id']}/query",json={
            'sql':f'SELECT metric,numeric_value,_source_row FROM "{tables[0]["table_name"]}"'})
        self.assertEqual(200,result.status_code,result.text)
        self.assertEqual([['总文件数',1234.0,2]],result.json()['rows'])

    def upload_rows(self, rows):
        book = Workbook()
        for row in rows:
            book.active.append(row)
        out = io.BytesIO()
        book.save(out)
        book.close()
        response = self.client.post(f'/api/v1/agent/conversations/{self.conv}/attachments',
            files={'file':('integrity.xlsx',out.getvalue())})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_p0_identifiers_survive_automatic_import_and_query(self):
        item = self.upload_rows([['项目编号','金额'],['00001',10],['9007199254740993',20]])
        self.assertEqual(item['status'],'ready',item)
        catalog = next(c for c in self.client.get('/api/v1/data-catalog').json() if c['workbook_id']==item['workbook_id'])
        table = catalog['catalog']['tables'][0]
        name = table['columns'][0]['physical_name']
        result = self.client.post(f"/api/v1/workbooks/{item['workbook_id']}/query",json={'sql':f'SELECT "{name}" FROM "{table["table_name"]}" ORDER BY _source_row'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['rows'],[['00001'],['9007199254740993']])

    def test_p0_repeated_header_requires_review(self):
        item = self.upload_rows([['项目','金额'],['甲',10],['乙',20],['项目','金额'],['合计',30]])
        self.assertEqual(item['status'],'needs_review',item)
        self.assertIn('NONBUSINESS_ROWS',[w['code'] for w in item['severe_warnings']])
        self.assertEqual(self.client.get(f'/api/v1/agent/conversations/{self.conv}').json()['scope']['workbook_ids'],[])
        confirmed = self.client.post(f"/api/v1/agent/conversations/{self.conv}/attachments/{item['import_id']}/confirm",
            json={'revision':item['revision'],'accept_severe_warnings':True})
        self.assertEqual(confirmed.status_code,200,confirmed.text)
        self.assertEqual(confirmed.json()['status'],'ready')
        catalog = next(c for c in self.client.get('/api/v1/data-catalog').json() if c['workbook_id']==item['workbook_id'])
        self.assertEqual(catalog['catalog']['tables'][0]['row_count'],2)
        self.assertEqual(catalog['catalog']['tables'][0]['excluded_row_count'],2)

    def test_p0_parallel_tables_cannot_be_confirmed(self):
        item = self.upload_rows([['项目','金额','项目','金额']]+[[f'P{i}',i,f'Q{i}',i] for i in range(6)])
        self.assertEqual(item['status'],'blocked',item)
        result = self.client.post(f"/api/v1/agent/conversations/{self.conv}/attachments/{item['import_id']}/confirm",
            json={'revision':item['revision'],'accept_severe_warnings':True})
        self.assertEqual(result.json()['status'],'blocked')

    def test_p0_source_change_prevents_confirmation(self):
        from pathlib import Path
        item = self.upload(formula=True)
        entity = app.state.excel_service._load_entity(item['import_id'])
        Path(entity.source_path).write_bytes(b'changed test fixture')
        result = self.client.post(f"/api/v1/agent/conversations/{self.conv}/attachments/{item['import_id']}/confirm",
            json={'revision':item['revision'],'accept_severe_warnings':True})
        self.assertEqual(result.status_code,400,result.text)
        self.assertIn('源文件',result.json()['detail'])

    def test_agent_streamed_answer_is_persisted_once(self):
        import json
        attachment = self.upload()
        catalog = next(c for c in self.client.get('/api/v1/data-catalog').json() if c['workbook_id'] == attachment['workbook_id'])
        table = catalog['catalog']['tables'][0]
        amount = table['columns'][1]['physical_name']
        sql = f'SELECT SUM("{amount}") AS total FROM "{table["table_name"]}"'
        replies = iter(['Action: query_workbook\nAction Input: '+json.dumps({'sql':sql}),
                        'Action: terminate\nAction Input: {"result":"查询总额为{{result-1:0:total|0}}。"}'])
        llm = SimpleNamespace(model_name='stub', max_tokens=4096,
            generate_chat=lambda messages: next(replies),
            generate_chat_stream=lambda messages: iter(['查询总额为', '7。']))
        with patch('app.api.endpoints.ExcelLLM', return_value=llm):
            response = self.client.post('/api/v1/agent/ask', json={'conv_id':self.conv,
                'question':'分析金额', 'scope':{'mode':'selected','workbook_ids':[attachment['workbook_id']]}})
        events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
        self.assertEqual('查询总额为210。', ''.join(e['text'] for e in events if e['type']=='answer.delta'))
        self.assertEqual('done', events[-1]['type'])
        detail = self.client.get(f'/api/v1/agent/conversations/{self.conv}').json()
        answers = [m for m in detail['messages'] if m['role']=='ai']
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]['content'], '查询总额为210。')
        self.assertEqual(answers[0]['meta']['timing'], events[-1]['timing'])
        self.assertEqual(answers[0]['meta']['attachments'], events[-1]['attachments'])
        self.assertEqual(events[-1]['attachments'][0]['workbook_id'], attachment['workbook_id'])
        self.assertGreaterEqual(events[-1]['timing']['elapsed_ms'], 0)
        from datetime import datetime
        self.assertIsNotNone(datetime.fromisoformat(events[-1]['timing']['completed_at']).tzinfo)

    def test_running_same_conversation_cannot_be_deleted_or_started_twice(self):
        service = app.state.excel_service
        self.assertTrue(service.try_acquire_agent_run(self.conv))
        try:
            self.assertFalse(service.try_acquire_agent_run(self.conv))
            self.assertEqual(self.client.delete(f'/api/v1/agent/conversations/{self.conv}').status_code, 429)
            self.assertEqual(self.client.get(f'/api/v1/agent/conversations/{self.conv}').status_code, 200)
        finally:
            service.release_agent_run(self.conv)

    def test_running_other_conversation_does_not_block_delete(self):
        service = app.state.excel_service
        self.assertTrue(service.try_acquire_agent_run('other-conversation'))
        try:
            response = self.client.delete(f'/api/v1/agent/conversations/{self.conv}')
            self.assertEqual(response.status_code, 200, response.text)
        finally:
            service.release_agent_run('other-conversation')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = db._engine, db._SessionLocal
        db._engine = db._SessionLocal = None
        db._init_engine(self.temp.name)
        cfg = AppConfig(storage_path=self.temp.name, llm={'name': 'stub', 'model': 'stub', 'max_tokens': 4096}, raw={'security': {'require_auth': False}})
        self.patch = patch('app.main.build_app_config', return_value=cfg)
        self.patch.start()
        self.client = TestClient(app)
        self.client.__enter__()
        self.conv = self.client.post('/api/v1/agent/conversations', json={'title': 'test'}).json()['conv_id']

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.patch.stop()
        db._engine.dispose()
        db._engine, db._SessionLocal = self.previous
        self.temp.cleanup()

    def upload(self, formula=False):
        workbook = Workbook()
        workbook.active.append(['项目', '金额'])
        workbook.active.append(['甲', '=1+1' if formula else 10])
        workbook.active.append(['乙', 20])
        workbook.active.append(['丙', 30])
        workbook.active.append(['丁', 40])
        workbook.active.append(['戊', 50])
        workbook.active.append(['己', 60])
        out = io.BytesIO()
        workbook.save(out)
        response = self.client.post(f'/api/v1/agent/conversations/{self.conv}/attachments',
                                    files={'file': ('test.xlsx', out.getvalue())})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_auto_import_persistence_and_detach(self):
        attachment = self.upload()
        self.assertEqual(attachment['status'], 'ready', attachment)
        detail = self.client.get(f'/api/v1/agent/conversations/{self.conv}').json()
        self.assertEqual(detail['scope']['workbook_ids'], [attachment['workbook_id']])
        self.assertEqual(detail['scope']['attachments'][0]['import_id'], attachment['import_id'])
        self.assertEqual(self.client.delete(f"/api/v1/agent/conversations/{self.conv}/attachments/{attachment['import_id']}").status_code, 200)
        self.assertEqual(self.client.get(f'/api/v1/agent/conversations/{self.conv}').json()['scope']['attachments'], [])
        self.assertFalse((__import__('pathlib').Path(self.temp.name)/attachment['workbook_id']).exists())
        self.assertFalse(any(c['workbook_id'] == attachment['workbook_id'] for c in self.client.get('/api/v1/data-catalog').json()))

    def test_delete_session_cleans_book_and_reports(self):
        from pathlib import Path
        attachment = self.upload()
        report_dir = Path(self.temp.name)/'agent_conversations'/self.conv/'reports'
        report_dir.mkdir(parents=True)
        # Create an artifact through the real report tool, not a hand-written file.
        from app.agent.tools import html_interpreter
        html_interpreter(app.state.excel_service, '', '<html><body><p>test</p></body></html>', conversation_id=self.conv)
        response = self.client.delete(f'/api/v1/agent/conversations/{self.conv}')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(attachment['workbook_id'], response.json()['deleted_workbooks'])
        self.assertFalse((Path(self.temp.name)/attachment['workbook_id']).exists())
        self.assertFalse(report_dir.parent.exists())

    def test_shared_attachment_preserved_until_last_session_deleted(self):
        from pathlib import Path
        from app.agent.session import AgentConversation
        attachment = self.upload()
        other = self.client.post('/api/v1/agent/conversations', json={'title': 'shared'}).json()['conv_id']
        conv = AgentConversation.load(other, 'anonymous')
        conv.set_scope({'mode': 'selected', 'workbook_ids': [attachment['workbook_id']], 'attachments': [attachment]})
        response = self.client.delete(f'/api/v1/agent/conversations/{self.conv}')
        self.assertIn(attachment['workbook_id'], response.json()['retained_shared_workbooks'])
        self.assertTrue((Path(self.temp.name)/attachment['workbook_id']).exists())
        self.client.delete(f'/api/v1/agent/conversations/{other}')
        self.assertFalse((Path(self.temp.name)/attachment['workbook_id']).exists())

    def test_active_analysis_rejects_cleanup(self):
        self.upload()
        service = app.state.excel_service
        self.assertTrue(service.try_acquire_agent_run())
        try:
            self.assertEqual(self.client.delete(f'/api/v1/agent/conversations/{self.conv}').status_code, 429)
            self.assertEqual(self.client.get(f'/api/v1/agent/conversations/{self.conv}').status_code, 200)
        finally:
            service.release_agent_run()

    def test_failed_parse_does_not_leave_attachment_data(self):
        from pathlib import Path
        response = self.client.post(f'/api/v1/agent/conversations/{self.conv}/attachments', files={'file': ('broken.xlsx', b'invalid zip')})
        self.assertGreaterEqual(response.status_code, 400)
        self.assertFalse([p for p in Path(self.temp.name).iterdir() if p.is_dir() and len(p.name) == 32])

    def test_staging_failure_rolls_back_session_and_files(self):
        from pathlib import Path
        attachment = self.upload()
        with patch('app.service.lifecycle.Path.rename', side_effect=PermissionError('locked file')):
            response = self.client.delete(f'/api/v1/agent/conversations/{self.conv}')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get(f'/api/v1/agent/conversations/{self.conv}').status_code, 200)
        self.assertTrue((Path(self.temp.name)/attachment['workbook_id']).exists())

    def test_pending_cleanup_retry_after_file_removal_failure(self):
        from pathlib import Path
        from app.service.lifecycle import recover_pending
        self.upload()
        with patch('app.service.lifecycle.shutil.rmtree', side_effect=OSError('locked file')):
            response = self.client.delete(f'/api/v1/agent/conversations/{self.conv}')
        self.assertTrue(response.json()['cleanup_warnings'])
        recover_pending(app.state.excel_service)
        pending = Path(self.temp.name)/'.cleanup_pending'
        self.assertEqual(list(pending.iterdir()), [])

    def test_pending_precommit_data_restored_after_restart(self):
        from pathlib import Path
        from app.service.lifecycle import recover_pending
        attachment = self.upload()
        root = Path(self.temp.name)
        pending = root/'.cleanup_pending'/('a'*32)
        pending.mkdir(parents=True)
        original = root/attachment['workbook_id']
        original.rename(pending/('workbook--'+attachment['workbook_id']))
        recover_pending(app.state.excel_service)
        self.assertTrue(original.exists())
        self.assertFalse(pending.exists())

    def test_path_escape_rejected_without_deleting_conversation(self):
        from app.agent.session import AgentConversation
        conv = AgentConversation.load(self.conv, 'anonymous')
        conv.set_scope({'mode': 'selected', 'workbook_ids': ['../outside'], 'attachments': []})
        self.assertEqual(self.client.delete(f'/api/v1/agent/conversations/{self.conv}').status_code, 400)
        self.assertEqual(self.client.get(f'/api/v1/agent/conversations/{self.conv}').status_code, 200)

    def test_reports_restored_when_workbook_staging_fails(self):
        from pathlib import Path
        from app.agent.tools import html_interpreter
        attachment = self.upload()
        html_interpreter(app.state.excel_service, '', '<html><body><p>test</p></body></html>', conversation_id=self.conv)
        original_rename = Path.rename
        def rename(path, destination):
            if path.name == attachment['workbook_id']:
                raise PermissionError('locked workbook')
            return original_rename(path, destination)
        with patch('app.service.lifecycle.Path.rename', new=rename):
            self.assertEqual(self.client.delete(f'/api/v1/agent/conversations/{self.conv}').status_code, 403)
        self.assertTrue((Path(self.temp.name)/'agent_conversations'/self.conv/'reports').exists())
        self.assertEqual(self.client.get(f'/api/v1/agent/conversations/{self.conv}').status_code, 200)

    def test_open_mode_shares_history_with_another_client_without_credentials(self):
        self.upload()
        other = TestClient(app)
        try:
            self.assertEqual(other.get(f'/api/v1/agent/conversations/{self.conv}').status_code, 200)
            history = other.get('/api/v1/agent/conversations').json()
            self.assertTrue(any(c['conv_id'] == self.conv for c in history))
        finally:
            other.close()

    def test_pin_persists_without_renaming_and_orders_history(self):
        other = self.client.post('/api/v1/agent/conversations', json={'title': 'later'}).json()['conv_id']
        response = self.client.patch(f'/api/v1/agent/conversations/{self.conv}', json={'pinned': True})
        self.assertEqual(response.status_code, 200)
        rows = self.client.get('/api/v1/agent/conversations').json()
        self.assertEqual(rows[0]['conv_id'], self.conv)
        self.assertTrue(rows[0]['pinned'])
        self.assertEqual(rows[0]['title'], 'test')
        self.client.patch(f'/api/v1/agent/conversations/{self.conv}', json={'pinned': False, 'title': 'renamed'})
        self.assertEqual(self.client.get(f'/api/v1/agent/conversations/{self.conv}').json()['title'], 'renamed')

    def test_formula_requires_explicit_confirmation_and_revision(self):
        attachment = self.upload(formula=True)
        self.assertEqual(attachment['status'], 'needs_review', attachment)
        url = f"/api/v1/agent/conversations/{self.conv}/attachments/{attachment['import_id']}/confirm"
        self.assertEqual(self.client.post(url, json={'revision': 999, 'accept_severe_warnings': True}).status_code, 409)
        unaccepted = self.client.post(url, json={'revision': attachment['revision']}).json()
        self.assertEqual(unaccepted['status'], 'needs_review')
        accepted = self.client.post(url, json={'revision': attachment['revision'], 'accept_severe_warnings': True}).json()
        self.assertEqual(accepted['status'], 'ready')

    def test_cross_conversation_and_missing_scope_rejected(self):
        attachment = self.upload()
        other = self.client.post('/api/v1/agent/conversations', json={'title': 'other'}).json()['conv_id']
        self.assertEqual(self.client.post(f"/api/v1/agent/conversations/{other}/attachments/{attachment['import_id']}/confirm",
                                         json={'revision': 1, 'accept_severe_warnings': True}).status_code, 404)
        self.assertEqual(self.client.post('/api/v1/agent/ask', json={'question': '数量', 'conv_id': self.conv,
                               'scope': {'mode': 'auto'}}).status_code, 422)

    def test_owner_cannot_access_another_users_attachment_session(self):
        from app.security import ApiKeyAuthenticator
        app.state.authenticator = ApiKeyAuthenticator(True, (('alice', 'a'*24), ('bob', 'b'*24)))
        self.client.headers['Authorization'] = 'Bearer '+'a'*24
        self.conv = self.client.post('/api/v1/agent/conversations', json={'title': 'alice'}).json()['conv_id']
        self.upload()
        self.client.headers['Authorization'] = 'Bearer '+'b'*24
        self.assertEqual(self.client.get(f'/api/v1/agent/conversations/{self.conv}').status_code, 404)
        self.assertEqual(self.client.post(f'/api/v1/agent/conversations/{self.conv}/attachments',
                                         files={'file': ('test.csv', b'col,value\na,1')}).status_code, 404)

    def test_no_attachments_uses_general_chat_without_catalog_or_tools(self):
        captured = []
        def reply(messages):
            captured.extend(messages)
            yield '你好，'
            yield '可以直接聊天。'
        fake = SimpleNamespace(model_name='stub', last_finish_reason='stop', generate_chat_stream=reply)
        with patch('app.api.endpoints.ExcelLLM', return_value=fake), \
             patch('app.api.endpoints.run_multi_workbook_agent') as agent, \
             patch('app.service.service.WorkbookImportService.list_workbook_catalog') as catalog:
            response = self.client.post('/api/v1/agent/ask', json={'conv_id': self.conv, 'question': '你好',
                                        'scope': {'mode': 'selected', 'workbook_ids': []}})
        self.assertEqual(response.status_code, 200)
        self.assertIn('answer.delta', response.text)
        self.assertIn('你好，可以直接聊天。', response.text)
        self.assertIn('"analysis_route": "general"', response.text)
        agent.assert_not_called()
        catalog.assert_not_called()
        detail = self.client.get(f'/api/v1/agent/conversations/{self.conv}').json()
        self.assertEqual([m for m in detail['messages'] if m['role'] == 'ai'][-1]['content'], '你好，可以直接聊天。')
        self.assertFalse(any('authorized-workbook-catalog' in m['content'] for m in captured))
        self.assertEqual(detail['title'], '你好')

    def test_general_then_attachment_then_general_keeps_session_and_scope(self):
        fake = SimpleNamespace(model_name='stub', last_finish_reason='stop', generate_chat_stream=lambda m: iter(['通用回答']))
        with patch('app.api.endpoints.ExcelLLM', return_value=fake):
            self.client.post('/api/v1/agent/ask', json={'conv_id': self.conv, 'question': '你好', 'scope': {'mode': 'selected'}})
        attachment = self.upload()
        events = iter([{'type': 'done', 'full_answer': '附件结果', 'sources': []}])
        with patch('app.api.endpoints.ExcelLLM', return_value=fake), patch('app.api.endpoints.run_multi_workbook_agent', return_value=events) as agent:
            response = self.client.post('/api/v1/agent/ask', json={'conv_id': self.conv, 'question': '统计数据',
                'scope': {'mode': 'selected', 'workbook_ids': [attachment['workbook_id']]}})
            self.assertIn('附件结果', response.text)
            agent.assert_called_once()
        with patch('app.api.endpoints.ExcelLLM', return_value=fake), patch('app.api.endpoints.run_multi_workbook_agent') as agent:
            response = self.client.post('/api/v1/agent/ask', json={'conv_id': self.conv, 'question': '解释统计学', 'scope': {'mode': 'selected'}})
            self.assertIn('通用回答', response.text)
            agent.assert_not_called()
        detail = self.client.get(f'/api/v1/agent/conversations/{self.conv}').json()
        self.assertEqual(len(detail['scope']['attachments']), 1)
