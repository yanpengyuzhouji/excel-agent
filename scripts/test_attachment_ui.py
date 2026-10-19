"""Browser regression: mock model traffic, no user database mutations."""
import json
import os
from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
        try:
            page = browser.new_page(viewport={'width': 1360, 'height': 900})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            attachment = {'import_id': 'import-a', 'workbook_id': 'book-a', 'filename': 'test.csv', 'status': 'ready', 'revision': 1}
            scope = {'mode': 'selected', 'workbook_ids': ['book-a'], 'attachments': [attachment]}
            conversations = []
            asked = []
            messages = []
            state = {'uploads': 0}
            report = {'report_id': 'report-a', 'title': '测试报告', 'url': '/api/v1/agent/conversations/ui-test/reports/report-a', 'html': '<!doctype html><html><body><h1>测试报告</h1><p>总额 10</p></body></html>'}

            def route_request(route):
                url = route.request.url
                method = route.request.method
                if url.endswith('/capabilities'):
                    route.fulfill(json={'pdf_available': False})
                elif url.endswith('/models'):
                    route.fulfill(json=[{'name': 'test-model', 'label': '测试模型', 'default': True}])
                elif url.endswith('/agent/conversations'):
                    if method == 'POST':
                        conversations.append({'conv_id': 'ui-test', 'title': '新对话'})
                        route.fulfill(json={'conv_id': 'ui-test', 'scope': scope})
                    else:
                        route.fulfill(json=conversations)
                elif url.endswith('/ui-test/attachments'):
                    state['uploads'] += 1
                    index = state['uploads']
                    item = dict(attachment, import_id=f'import-{index}', workbook_id=f'book-{index}', filename=f'file-{index}.csv') if index > 1 else attachment
                    if index == 3:
                        item.update(status='needs_review', severe_warnings=[{'message': '公式缺少缓存值'}], errors=[])
                    route.fulfill(json=item)
                elif url.endswith('/import-3/confirm'):
                    route.fulfill(json={'import_id': 'import-3', 'workbook_id': 'book-3', 'filename': 'file-3.csv', 'status': 'ready', 'revision': 1})
                elif url.endswith('/agent/ask'):
                    asked.append(route.request.post_data_json)
                    events = [
                        {'type': 'step.start', 'id': 's1', 'action': 'query_workbook'},
                        {'type': 'step.done', 'id': 's1', 'action': 'query_workbook', 'status': 'done', 'result': {'columns': ['total'], 'rows': [[10]]}},
                        {'type': 'html', **report},
                        {'type': 'step.done', 'id': 's2', 'action': 'html_interpreter', 'status': 'done', 'result': report},
                        {'type': 'done', 'full_answer': '总额 10。报告链接：'+report['url'], 'sources': []},
                    ]
                    messages.extend([{'role': 'user', 'round_index': 1, 'content': '生成分析报告'},
                                     *[{'role': 'tool', 'round_index': 1, 'meta': {'detail': e}} for e in events if e['type'] == 'step.done'],
                                     {'role': 'ai', 'round_index': 1, 'content': events[-1]['full_answer']}])
                    route.fulfill(content_type='text/event-stream', body=''.join('data: '+json.dumps(e, ensure_ascii=False)+'\n\n' for e in events))
                elif url.endswith('/agent/conversations/ui-test'):
                    if method == 'PATCH':
                        route.fulfill(json={'title': '生成分析报告'})
                    else:
                        route.fulfill(json={'conv_id': 'ui-test', 'title': '测试会话', 'scope': scope, 'messages': messages})
                else:
                    route.continue_()

            page.route('**/api/v1/**', route_request)
            page.goto('http://127.0.0.1:8090/ui/')
            page.wait_for_load_state('networkidle')
            assert page.locator('#sidebar').inner_text().find('工作簿建库') == -1
            assert page.locator('#reviewDialog').is_visible() is False
            page.locator('#fileInput').set_input_files({'name': 'test.csv', 'mimeType': 'text/csv', 'buffer': b'name,amount\na,10\n'})
            page.get_by_text('可提问', exact=True).wait_for()
            page.locator('#globalInput').fill('生成分析报告')
            page.locator('#globalAskBtn').click()
            page.locator('#globalAnswer').get_by_text('查看报告 ↗').wait_for()
            assert asked[0]['scope'] == {'mode': 'selected', 'workbook_ids': ['book-a']}
            assert page.locator('#globalProcess').get_attribute('open') is None
            assert page.locator('#globalReport > details').get_attribute('open') is None
            page.locator('#globalReport summary').click()
            assert page.frame_locator('#globalReport iframe').locator('h1').inner_text() == '测试报告'
            page.locator('#conversationList [data-conversation]').click()
            page.wait_for_function("!globalAgent.loading")
            assert page.locator('#attachmentChips').inner_text().find('test.csv') >= 0
            # Dragging into the input/composer uses the same upload path.
            page.evaluate("""() => {const data=new DataTransfer();data.items.add(new File(['name,amount\\na,10'],'second.csv',{type:'text/csv'}));document.querySelector('#composer').dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:data}));}""")
            page.wait_for_function("document.querySelectorAll('.attachment-chip').length === 2")
            page.screenshot(path='data/attachment-chat-desktop.png', full_page=True)
            page.set_viewport_size({'width': 393, 'height': 852})
            page.evaluate("document.body.classList.add('sidebar-collapsed')")
            assert page.locator('body').evaluate('el => el.scrollWidth <= window.innerWidth')
            page.screenshot(path='data/attachment-chat-mobile.png', full_page=True)
            page.locator('#fileInput').set_input_files({'name': 'warning.csv', 'mimeType': 'text/csv', 'buffer': b'name,amount\na,10\n'})
            page.locator('#reviewDialog').wait_for(state='visible')
            assert '公式缺少缓存值' in page.locator('#reviewIssues').inner_text()
            page.locator('#reviewAccept').click()
            page.locator('#reviewDialog').wait_for(state='hidden')
            assert '需确认' not in page.locator('#attachmentChips').inner_text()
            assert errors == [], errors
            print('PASS: attachment upload/drag, serious warning dialog/confirmation, scoped SSE, folded report, history restoration, mobile layout')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
