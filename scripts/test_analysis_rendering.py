"""Read-only UI smoke: structured-answer escaping, plan display and PDF links."""
import json
import os
from pathlib import Path
import sys
from playwright.sync_api import sync_playwright
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agent.analysis_result import AnalysisResult
from app.agent.evidence_publication import publish

# Exercise the actual producer, not just a hand-authored valid Markdown table.
catalogs = [{'workbook_id': 'w', 'catalog': {'tables': [
    {'table_name': 'data', 'columns': [{'physical_name': 'spec', 'display_name': '规格'}]}]}}]
analysis = AnalysisResult({'tasks': [{'id': 't', 'label': '原文', 'operation': 'table',
    'source': {'workbook_id': 'w', 'table_name': 'data', 'column': 'spec'}}]}, catalogs)
packet = analysis.resolve({'t': {'result_id': 'result-1'}}, [{'result_id': 'result-1',
    'source': {'workbook_id': 'w'}, 'sql': 'SELECT spec FROM data',
    'columns': ['spec'], 'rows': [['1*35|A<script>']]}])
generated_answer = analysis.text(packet)
native_answer = publish('{{table:result-1}}', [{'result_id': 'result-1',
    'columns': ['spec'], 'rows': [['1*35|A<script>']]}])['text']

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE') or
        'C:/Users/18779/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe')
    try:
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto('http://127.0.0.1:8092/ui/')
        page.wait_for_load_state('networkidle')
        rendered = page.evaluate(r'''() => {
            const el=document.createElement('div');
            el.innerHTML=globalAnswerHtml(['### 原文','', '| 编号 | 规格 |','| --- | --- |',
                String.raw`| 00001 | 1\*35\|A\<script\> |`,'',
                '[下载 PDF](/api/v1/agent/conversations/test/reports/r/pdf)'].join('\n'));
            return {text:el.textContent, columns:el.querySelectorAll('tbody td').length,
                pdf:el.querySelector('a')?.getAttribute('href'), scripts:el.querySelectorAll('script').length,
                label:globalActionLabel('define_analysis')};
        }''')
        assert '1*35|A<script>' in rendered['text'], rendered
        assert rendered['columns'] == 2 and rendered['scripts'] == 0, rendered
        assert rendered['pdf'].endswith('/reports/r/pdf'), rendered
        assert '分析任务' in rendered['label'], rendered
        assert not errors, errors
        generated = page.evaluate('''text => {
            const el=document.createElement('div');
            el.innerHTML=globalAnswerHtml(text);
            return {tables:el.querySelectorAll('table').length,
                cells:el.querySelectorAll('tbody td').length,text:el.textContent};
        }''', generated_answer)
        assert generated['tables'] == 1 and generated['cells'] == 1, generated
        assert '1*35|A<script>' in generated['text'], generated
        native = page.evaluate('''text => {
            const el=document.createElement('div');el.innerHTML=globalAnswerHtml(text);
            return {tables:el.querySelectorAll('table').length,
                text:el.textContent,scripts:el.querySelectorAll('script').length};
        }''', native_answer)
        assert native['tables'] == 1 and native['scripts'] == 0, native
        assert '1*35|A<script>' in native['text'], native
        report = dict(passed=True, checks=rendered, page_errors=errors,
                      generated_output=generated,
                      native_output=native,
                      scope='read_only_ui_no_model_calls_no_conversation_writes')
        if len(sys.argv) > 1:
            Path(sys.argv[1]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))
    finally:
        browser.close()
