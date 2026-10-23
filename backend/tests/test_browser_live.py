"""Opt-in real Chromium + React acceptance, entirely synthetic and network-contained.

Run with the frontend at localhost:5173:
  LCG_BROWSER_TEST=1 ../.venv/bin/python -m pytest tests/test_browser_live.py -q

Uses named reusable synthetic profiles under /tmp/lcg-step1-tests, NEVER the LCW
profile, .env, LCW domain, credentials, or a real Microsoft endpoint. A second
synthetic browser is the test driver for the React UI, not a second portal page.
No screenshots, frame contents, cookies, inputs or browser logs are persisted.
"""
import asyncio
import os
import socket
from pathlib import Path

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect, Error as PlaywrightError

from app import browser as module
from app import main
from app.contracts import extract_contract
from test_contracts import pdf

pytestmark = pytest.mark.skipif(os.environ.get('LCG_BROWSER_TEST') != '1', reason='Opt-in real synthetic browser test')

HTML = '''<!doctype html><html><body style="margin:0;height:2400px;background:#e8f2ff;font:20px Arial">
<div id="userProfileMenu">Synthetic operator</div><nav><a href="/family">Family</a></nav>
<form onsubmit="event.preventDefault();window.submitted=(window.submitted||0)+1">
<input id="first" aria-label="First" style="position:absolute;top:120px;left:80px;width:300px;height:30px">
<input id="second" aria-label="Second" style="position:absolute;top:165px;left:80px;width:300px;height:30px">
<button id="submit" style="position:absolute;top:210px;left:80px">Submit synthetic form</button></form>
<a id="next" href="/next" style="position:absolute;top:260px;left:80px">Next page</a>
<a href="https://evil.example/" style="position:absolute;top:300px;left:80px">Blocked site</a>
<div style="position:absolute;top:1000px">Synthetic scroll destination</div>
</body></html>'''
AUTH_HTML = '<h1>Approve sign in request</h1><input aria-label="Synthetic MFA" type="password"><p>Offline fixture only</p>'


def test_real_embedded_browser_input_refresh_ownership_and_persistence(monkeypatch):
    root = Path('/tmp/lcg-step1-tests')
    root.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setattr(module, 'PROFILE_PATH', root / 'controlled-profile')
    monkeypatch.setattr(module, 'dotenv_values', lambda *a, **k: {'LCW_URL': 'https://portal.example/'})
    intercepted = []
    class SyntheticSession(module.LCWSession):
        async def secure_page(self):
            async def synthetic(route):
                # This fixture cannot reach the internet, even for assets.
                url = route.request.url
                intercepted.append(url)
                if url.startswith('https://portal.example/'):
                    await route.fulfill(content_type='text/html', body=HTML)
                elif url.startswith(module.AUTH_ORIGIN + '/'):
                    await route.fulfill(content_type='text/html', body=AUTH_HTML)
                else:
                    await route.abort()
            await self.context.route('**/*', synthetic)
            await super().secure_page()
    s = SyntheticSession()
    monkeypatch.setattr(module, 'session', s)
    monkeypatch.setattr(main, 'session', s)

    async def run():
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(main.app, log_level='critical', access_log=False, ws_max_size=16384))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        driver = ui_context = None
        try:
            while not server.started:
                await asyncio.sleep(0.02)
            await s.start()
            assert s.authenticated()
            assert s.metadata()['mode'] == 'headless'
            original_pid = s.metadata()['pid']
            original_target = s.metadata()['target']
            page = s.page
            await page.context.add_cookies([{'name': 'synthetic_persistence', 'value': 'present', 'url': 'https://portal.example', 'expires': 2000000000}])
            await page.evaluate("localStorage.setItem('syntheticPersistence','retained')")
            driver = await async_playwright().start()
            ui_context = await driver.chromium.launch_persistent_context(str(root / 'ui-profile'), headless=True, viewport={'width': 1450, 'height': 1050})
            ui = ui_context.pages[0]
            # Route the real app to this isolated test backend, without changing
            # application configuration or touching the running operator backend.
            await ui.add_init_script(f'''(() => {{
                const rewrite = value => typeof value === 'string' ? value.replace('127.0.0.1:8000', '127.0.0.1:{port}') : value;
                const originalFetch = window.fetch;
                window.fetch = (url, options) => originalFetch(rewrite(url), options);
                const OriginalSocket = window.WebSocket;
                window.WebSocket = class extends OriginalSocket {{ constructor(url, protocols) {{ super(rewrite(url), protocols); window.testSocket = this; }} }};
            }})()''')
            await ui.goto('http://localhost:5173')
            image = ui.get_by_alt_text('Live controlled LCW page')
            await expect(image).to_be_visible(timeout=15000)
            assert await image.evaluate('(img) => img.naturalWidth') == 1280
            await ui.get_by_role('button', name='Take Control', exact=True).click()
            await expect(ui.get_by_text('Human control', exact=True)).to_be_visible()

            async def click_remote(selector):
                await image.scroll_into_view_if_needed()
                remote = await page.locator(selector).bounding_box()
                rect = await image.bounding_box()
                await ui.mouse.click(rect['x'] + (remote['x'] + remote['width']/2) / 1280 * rect['width'],
                                     rect['y'] + (remote['y'] + remote['height']/2) / 800 * rect['height'])
            await click_remote('#first')
            await ui.keyboard.type('hello')
            await expect(page.locator('#first')).to_have_value('hello')
            await ui.keyboard.press('Backspace')
            await expect(page.locator('#first')).to_have_value('hell')
            await ui.keyboard.press('Tab')
            await ui.keyboard.type('world')
            await expect(page.locator('#second')).to_have_value('world')
            await ui.keyboard.press('Enter')
            await page.wait_for_function('window.submitted === 1')

            # A responsive layout and expanded view retain the exact coordinate map.
            await ui.set_viewport_size({'width': 1100, 'height': 1000})
            await click_remote('#first')
            await ui.keyboard.type('!')
            await expect(page.locator('#first')).to_have_value('hell!')
            await ui.get_by_role('button', name='Expand browser').click()
            await click_remote('#submit')
            await page.wait_for_function('window.submitted === 2')
            await image.hover()
            await ui.mouse.wheel(0, 600)
            await page.wait_for_function('scrollY > 300')
            await ui.mouse.wheel(0, -1200)
            await page.wait_for_function('scrollY === 0')
            await ui.get_by_role('button', name='Exit expanded view').click()

            await click_remote('#next')
            await expect(page).to_have_url('https://portal.example/next')
            await ui.get_by_role('button', name='Back', exact=True).click()
            await expect(page).to_have_url('https://portal.example/')
            await ui.get_by_role('button', name='Forward', exact=True).click()
            await expect(page).to_have_url('https://portal.example/next')
            await ui.get_by_role('button', name='Reload page').click()
            await expect(image).to_be_visible()

            # React refresh reconnects to the same process and exact target.
            await ui.reload()
            await expect(image).to_be_visible(timeout=15000)
            await expect(ui.get_by_text('Human control', exact=True)).to_be_visible()
            assert s.metadata()['pid'] == original_pid and s.metadata()['target'] == original_target
            assert s.page is page
            # WebSocket interruption must reconnect without creating any page.
            await ui.evaluate('window.testSocket.close()')
            await expect(ui.get_by_text('Viewer disconnected. Reconnecting to the same session…').first).to_be_visible()
            await expect(image).to_be_visible(timeout=15000)
            assert s.page is page
            await click_remote('#first')
            await ui.keyboard.type('recovered')
            await expect(page.locator('#first')).to_have_value('recovered')

            await ui.get_by_role('button', name='Return Control', exact=True).click()
            await expect(ui.get_by_text('Agent control · input disabled')).to_be_visible()
            await click_remote('#first')
            await ui.keyboard.type('blocked')
            await expect(page.locator('#first')).to_have_value('recovered')
            # Agent's existing action boundary operates on precisely the displayed page.
            s.job = {'id': 'synthetic', 'extraction_id': 'synthetic', 'source': extract_contract(pdf()), 'result': None, 'state': 'inspecting', 'stage': 'test', 'version': 0, 'events': []}
            await s.step('Synthetic agent input', lambda: page.locator('#first').fill('agent'), s.epoch)
            await expect(page.locator('#first')).to_have_value('agent')
            s.job = None

            # Synthetic MFA is routed locally; no Microsoft request leaves the process.
            await page.goto(module.AUTH_ORIGIN + '/synthetic-challenge')
            await s.observe()
            assert s.state == 'awaiting_mfa' and s.owner == 'human'
            assert s.result()['preview_available']
            await expect(image).to_be_visible()
            await page.goto('https://portal.example/')
            try:
                await page.goto('https://evil.example/')
            except PlaywrightError:
                pass
            assert 'https://evil.example/' not in intercepted
            await expect(ui.get_by_role('heading', name='Browser paused')).to_be_visible()
            await ui.get_by_role('button', name='LCW home', exact=True).click()
            await expect(page).to_have_url('https://portal.example/')
            await expect(image).to_be_visible()

            await ui_context.close()
            ui_context = None
            for _ in range(100):
                if s.viewer is None:
                    break
                await asyncio.sleep(0.02)
            # Backend detach and exact-target attachment reuse the running process.
            await s.shutdown()
            await s.start(existing_only=True)
            assert s.metadata()['pid'] == original_pid and s.metadata()['target'] == original_target
            assert await s.page.evaluate("localStorage.getItem('syntheticPersistence')") == 'retained'
            assert any(c['name'] == 'synthetic_persistence' for c in await s.context.cookies())
            # A second backend cannot own or launch against the same test profile.
            second = SyntheticSession()
            with pytest.raises(module.HTTPException) as error:
                await second.start()
            assert error.value.status_code == 409
            assert second.browser is None
            # Explicit graceful close/reopen reuses profile data in headless Chromium.
            await s.close()
            for _ in range(100):
                if not (module.PROFILE_PATH / 'SingletonLock').is_symlink():
                    break
                await asyncio.sleep(0.02)
            await s.start()
            assert await s.page.evaluate("localStorage.getItem('syntheticPersistence')") == 'retained'
            assert any(c['name'] == 'synthetic_persistence' for c in await s.context.cookies())
            assert all(url.startswith(('https://portal.example/', module.AUTH_ORIGIN + '/')) for url in intercepted)
        finally:
            if ui_context:
                await ui_context.close()
            if driver:
                await driver.stop()
            await s.close()
            server.should_exit = True
            await serving
    asyncio.run(run())
