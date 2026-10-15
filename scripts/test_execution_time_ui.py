"""Live execution clock and understated divider, without model or DB writes."""
import os
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
    try:
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.route('**/api/v1/**', lambda route: route.fulfill(json=[] if route.request.url.endswith(('/models', '/conversations')) else {}))
        page.goto('http://127.0.0.1:8090/ui/')
        page.wait_for_load_state('networkidle')
        page.evaluate("""() => {globalAgent.busy=true;globalAgent.currentRound=1;
            globalAgent.history=[{role:'user',content:'分析各地区投资分布',round_index:1}];
            globalAgent.status='正在查询数据';startExecutionTimer();renderGlobalExecution();} """)
        page.wait_for_function("document.querySelector('#globalElapsed').textContent === '已用时 1 秒'")
        assert page.locator('#globalProcess').is_visible()
        assert page.locator('#globalProcess > summary').evaluate("e=>getComputedStyle(e).borderBottomWidth") == '1px'
        assert page.locator('#globalProcess').evaluate("e=>getComputedStyle(e).borderTopWidth") == '0px'
        page.evaluate("""() => {handleEvent({type:'done',full_answer:'各地区投资已汇总。',timing:{elapsed_ms:65000,completed_at:'2026-10-15T01:55:00Z'}});
            globalAgent.busy=false;renderGlobalExecution();} """)
        assert page.locator('#globalElapsed').inner_text() == '用时 1 分 5 秒'
        page.wait_for_timeout(1100)
        assert page.locator('#globalElapsed').inner_text() == '用时 1 分 5 秒'
        assert not page.locator('#globalProcess').evaluate('e=>e.open')
        page.locator('#globalElapsed').click()
        page.wait_for_function('globalAgent.processExpanded === true')
        page.evaluate('renderGlobalExecution()')
        assert page.locator('#globalProcess').evaluate('e=>e.open')
        page.locator('#globalElapsed').click()
        page.wait_for_function('globalAgent.processExpanded === false')
        timestamp = page.locator('#globalAnswer .answer-meta time')
        assert timestamp.get_attribute('datetime') == '2026-10-15T01:55:00.000Z'
        assert timestamp.evaluate("e=>getComputedStyle(e,'::after').visibility") == 'hidden'
        timestamp.hover()
        assert timestamp.evaluate("e=>getComputedStyle(e,'::after').visibility") == 'visible'
        assert '2026' in timestamp.get_attribute('data-tooltip')
        page.screenshot(path='data/execution-time-ui.png', full_page=True)
        page.evaluate("() => {globalAgent.busy=true;startExecutionTimer();handleEvent({type:'error',error:'测试失败'});globalAgent.busy=false;}")
        assert page.evaluate('executionTimer===null && executionStarted===null')
        print('Live timer, completion freeze, failure stop and divider: passed')
    finally:
        browser.close()
