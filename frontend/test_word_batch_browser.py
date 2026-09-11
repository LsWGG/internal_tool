"""End-to-end browser check using a disposable API instance and generated fixtures."""
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from playwright.sync_api import sync_playwright

BACKEND = Path(__file__).resolve().parents[1] / 'backend'
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / 'tests'))
from app.word_batch_api import make_router
from test_word_batch import template_bytes, excel_bytes, image_bytes


with tempfile.TemporaryDirectory(prefix='word-batch-browser-') as root:
    app = FastAPI()
    app.include_router(make_router(root))
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8001, log_level='error'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started, 'Test API did not start'
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            def proxy(route):
                if '/api/word-batch/' in route.request.url:
                    response = route.fetch(url=route.request.url.replace('127.0.0.1:5174', '127.0.0.1:8001'))
                    route.fulfill(response=response)
                else:
                    route.abort()
            page.route('**/api/**', proxy)
            page.goto('http://127.0.0.1:5174/#word-batch')
            page.get_by_role('heading', name='Word 批量生成', exact=True).wait_for()
            inputs = page.locator('.wb-page input[type=file]')
            inputs.nth(0).set_input_files({'name': '通知模板.docx', 'mimeType': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'buffer': template_bytes()})
            inputs.nth(1).set_input_files({'name': '员工.xlsx', 'mimeType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'buffer': excel_bytes(embedded=True)})
            inputs.nth(2).set_input_files({'name': 'photo.png', 'mimeType': 'image/png', 'buffer': image_bytes()})
            page.get_by_role('button', name='解析模板与 Excel', exact=True).click()
            page.get_by_role('button', name='试生成首行', exact=True).wait_for()
            page.get_by_role('button', name='试生成首行', exact=True).click()
            page.wait_for_function('document.querySelector(".wb-preview-content section.wb-docx") && document.querySelector(".wb-preview-content img")?.naturalWidth>0')
            assert '张三' in page.locator('.wb-preview-content').inner_text()
            assert '{{姓名}}' not in page.locator('.wb-preview-content').inner_text()
            page.locator('.wb-config').evaluate('(el)=>el.scrollTop=0')
            page.evaluate('window.scrollTo(0,0)')
            page.screenshot(path='/private/tmp/word-batch-desktop.png', full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), 'Mobile overflow'
            page.screenshot(path='/private/tmp/word-batch-mobile.png', full_page=True)
            page.get_by_role('button', name='批量生成 2 份', exact=True).click()
            page.get_by_text('全部成功', exact=True).wait_for(timeout=30000)
            assert page.locator('.wb-page input[type=file]').nth(0).input_value() == '', 'Form not reset'
            page.get_by_role('button', name='查看结果', exact=True).click()
            page.locator('.wb-result').nth(1).wait_for()
            page.locator('.wb-result').first.get_by_role('button', name='预览', exact=True).click()
            page.wait_for_function('document.querySelector(".wb-preview-content img")?.naturalWidth>0')
            with page.expect_download() as info:
                page.get_by_role('link', name='下载 ZIP', exact=True).click()
            assert info.value.failure() is None
            page.locator('.wb-task-actions').get_by_role('button', name='删除', exact=True).click()
            page.get_by_role('button', name='确认删除', exact=True).click()
            page.wait_for_function('!document.querySelector(".wb-task")')
            assert not list(Path(root).iterdir()), 'Task/input/output files not cleaned'
            assert not errors, errors
            print('PASS: template mapping, Excel embedded image, DOCX preview, mobile layout, batch ZIP, reset and cleanup')
            page.unroute_all(behavior='wait')
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
