"""Verify actual history report preview and PDF download, without synthetic UI state."""
import json
import os
from pathlib import Path
import sys
from playwright.sync_api import sync_playwright

conv_id=sys.argv[1];output=Path(sys.argv[2]);output.mkdir(parents=True,exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE') or
        'C:/Users/18779/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe')
    try:
        page=browser.new_page(viewport={'width':1360,'height':1000})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto('http://127.0.0.1:8092/ui/');page.wait_for_load_state('networkidle')
        page.evaluate('(id)=>loadGlobalConversation(id)',conv_id)
        page.wait_for_function('!globalAgent.loading')
        details=page.locator('#globalReport > details');details.wait_for(state='visible',timeout=10000)
        collapsed=not details.evaluate('e=>e.open')
        if collapsed:details.locator('summary').click()
        frame=page.frame_locator('#globalReport iframe')
        frame.locator('body').wait_for(state='visible',timeout=15000)
        text=frame.locator('body').inner_text()
        if len(text.strip())<30:raise ValueError('Report preview body is empty')
        page.locator('#globalReport').screenshot(path=str(output/'preview.png'))
        link=page.locator('#globalReport a').filter(has_text='下载 PDF')
        with page.expect_download(timeout=60000) as download:
            link.click()
        file=output/'browser-download.pdf';download.value.save_as(str(file))
        if not file.read_bytes().startswith(b'%PDF-'):raise ValueError('Browser download is not a PDF')
        print(json.dumps({'preview_text_chars':len(text),'collapsed_by_default':collapsed,
                          'pdf_download':True,'page_errors':errors,'screenshot':str(output/'preview.png')},ensure_ascii=False))
    finally:browser.close()
