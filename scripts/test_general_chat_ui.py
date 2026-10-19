"""Mocked tool-free chat UI, role separation and history restoration."""
import json
import os
from playwright.sync_api import sync_playwright


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
    try:
        page = browser.new_page(viewport={'width': 1360, 'height': 900})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        asks = []
        messages = []
        state = {'title': '通用问答测试', 'pinned': False}
        def handler(route):
            path = route.request.url
            if path.endswith('/models'):
                route.fulfill(json=[{'name': 'stub', 'label': '测试模型'}])
            elif path.endswith('/capabilities'):
                route.fulfill(json={'pdf_available': False})
            elif path.endswith('/agent/conversations'):
                route.fulfill(json={'conv_id': 'general-test'} if route.request.method == 'POST' else [] if state.get('deleted') else [{'conv_id': 'general-test', **state}])
            elif path.endswith('/agent/conversations/general-test'):
                if route.request.method == 'PATCH':
                    state.update(route.request.post_data_json)
                    route.fulfill(json=state)
                elif route.request.method == 'DELETE':
                    state['deleted'] = True
                    route.fulfill(json={'deleted': True})
                else:
                    route.fulfill(json={'title': state['title'], 'scope': {'mode': 'selected', 'workbook_ids': [], 'attachments': []}, 'messages': messages})
            elif path.endswith('/agent/ask'):
                request = route.request.post_data_json
                asks.append(request)
                round_index = len(asks)
                messages.extend([{'role': 'user', 'content': request['question'], 'round_index': round_index},
                                 {'role': 'ai', 'content': '你好，可以直接问我一般问题。', 'round_index': round_index}])
                events = [{'type': 'answer.delta', 'text': '你好，'}, {'type': 'answer.delta', 'text': '可以直接问我一般问题。'},
                          {'type': 'done', 'full_answer': '你好，可以直接问我一般问题。', 'sources': [], 'analysis_route': 'general'}]
                route.fulfill(content_type='text/event-stream', body=''.join('data: '+json.dumps(e, ensure_ascii=False)+'\n\n' for e in events))
            else:
                route.continue_()
        page.route('**/api/v1/**', handler)
        page.goto('http://127.0.0.1:8090/ui/')
        page.wait_for_load_state('networkidle')
        assert page.locator('#scopeLabel').inner_text() == '通用问答'
        page.locator('#globalInput').fill('你好，介绍一下自己')
        page.locator('#globalAskBtn').click()
        page.locator('#globalAnswer').get_by_text('你好，可以直接问我一般问题。').wait_for()
        assert asks[0]['scope'] == {'mode': 'selected', 'workbook_ids': []}
        assert page.locator('#globalProcess').is_visible()
        assert page.locator('#globalProcess').get_attribute('open') is None
        assert page.locator('#globalQuestion').evaluate("e=>getComputedStyle(e).backgroundColor") != page.locator('#globalAnswer').evaluate("e=>getComputedStyle(e).backgroundColor")
        assert page.locator('#globalQuestion').bounding_box()['x'] > page.locator('#globalAnswer').bounding_box()['x']
        page.locator('#globalInput').fill('你可以回答什么问题？')
        page.locator('#globalAskBtn').click()
        page.wait_for_function("document.querySelectorAll('#globalPast .history-message').length === 2")
        page.locator('#globalAskBtn').wait_for(state='visible')
        page.wait_for_function("!document.querySelector('#globalAskBtn').disabled")
        page.locator('#conversationList [data-conversation]').click()
        page.locator('#globalPast .message-user').wait_for()
        page.wait_for_function("!globalAgent.loading")
        assert page.locator('#globalPast .message-assistant').count() == 1
        assert page.locator('#conversationList .conversation-row button').count() == 1
        title = page.locator('#conversationList [data-conversation]')
        # Simulate an older cached stylesheet lacking the newly added menu rule.
        page.evaluate("""() => {for(const sheet of document.styleSheets){for(let i=sheet.cssRules.length-1;i>=0;i--){if(sheet.cssRules[i].selectorText==='#conversationMenu')sheet.deleteRule(i);}}}""")
        title.click(button='right')
        assert page.locator('#conversationMenu').evaluate("e=>getComputedStyle(e).position") == 'fixed', 'Menu must float at the cursor, not become a flex item in the chat layout'
        menu_box = page.locator('#conversationMenu').bounding_box()
        title_box = title.bounding_box()
        assert abs(menu_box['x']-(title_box['x']+title_box['width']/2)) < 10
        assert abs(menu_box['y']-(title_box['y']+title_box['height']/2)) < 10
        page.screenshot(path='data/history-context-menu.png', full_page=True)
        assert page.locator('#conversationMenu button').all_text_contents() == ['重命名', '置顶', '删除']
        page.locator('#conversationMenu [data-action="pin"]').click()
        page.wait_for_function("document.querySelector('#conversationList button').title.includes('已置顶')")
        title.click(button='right')
        assert page.locator('#conversationMenu [data-action="pin"]').inner_text() == '取消置顶'
        page.keyboard.press('Escape')
        assert page.locator('#conversationMenu').count() == 0
        title.click(button='right')
        page.once('dialog', lambda dialog: dialog.accept('新的标题'))
        page.locator('#conversationMenu [data-action="rename"]').click()
        page.get_by_role('button', name='新的标题', exact=True).wait_for()
        page.screenshot(path='data/general-chat-desktop.png', full_page=True)
        page.set_viewport_size({'width': 393, 'height': 852})
        page.evaluate("document.body.classList.add('sidebar-collapsed')")
        assert page.locator('body').evaluate('el=>el.scrollWidth <= window.innerWidth')
        page.screenshot(path='data/general-chat-mobile.png', full_page=True)
        page.set_viewport_size({'width': 1360, 'height': 900})
        page.evaluate("document.body.classList.remove('sidebar-collapsed')")
        page.locator('#conversationList [data-conversation]').click(button='right')
        page.locator('#conversationMenu [data-action="delete"]').click()
        dialog = page.locator('#deleteConversationDialog')
        dialog.wait_for(state='visible')
        bounds = dialog.bounding_box()
        assert abs(bounds['x']+bounds['width']/2-680) < 2
        assert abs(bounds['y']+bounds['height']/2-450) < 2
        assert page.locator('#deleteConversationName').inner_text() == '新的标题'
        page.screenshot(path='data/history-delete-dialog.png', full_page=True)
        dialog.get_by_role('button', name='取消', exact=True).click()
        dialog.wait_for(state='hidden')
        assert page.locator('#conversationList [data-conversation]').count() == 1
        page.locator('#conversationList [data-conversation]').click(button='right')
        page.locator('#conversationMenu [data-action="delete"]').click()
        page.locator('#confirmDeleteConversation').click()
        page.wait_for_function("document.querySelectorAll('#conversationList [data-conversation]').length === 0")
        assert not errors, errors
        print('PASS: general chat, clean history titles, context menu rename/pin, streamed answer, history, mobile')
    finally:
        browser.close()
