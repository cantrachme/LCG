"""Single local Chromium owner, interruptible read-only LCW inspection."""
import asyncio
import secrets
from contextlib import suppress
from typing import Literal
import fcntl
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from dotenv import dotenv_values
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout, async_playwright

from .contracts import ContractExtraction
from . import database
from .inspection import (InspectionError, build_result, child_links, expected_identity, normalized,
                         parse_contract_rows, parse_pra_rows, search_group, verify_family_header, verify_identity)

ENV_PATH = Path(__file__).resolve().parents[1] / '.env'
PROFILE_PATH = ENV_PATH.parent / '.lcw-browser-profile'
AUTH_ORIGIN = 'https://login.microsoftonline.com'
AUTH_ORIGINS = {AUTH_ORIGIN, 'https://login.microsoft.com', 'https://device.login.microsoftonline.com'}
VIEW_WIDTH, VIEW_HEIGHT = 1280, 800
LOCAL_ORIGINS = {'http://localhost:5173', 'http://127.0.0.1:5173'}


def origin(url):
    p = urlsplit(url)
    return f'{p.scheme}://{p.netloc}'


def local_request(request: Request):
    if (request.client is None or request.client.host not in {'127.0.0.1', '::1', 'testclient'}
            or request.url.hostname not in {'localhost', '127.0.0.1', '::1', 'testserver'}
            or request.headers.get('x-lcg-local') != '1'
            or (request.headers.get('origin') is not None and request.headers['origin'] not in LOCAL_ORIGINS)):
        raise HTTPException(403, 'Browser access is limited to the local Command Center.')


class Paused(Exception):
    pass


class AuthenticationRequired(Exception):
    pass


class InspectRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    extraction_id: str = Field(min_length=36, max_length=36)
    request_id: str = Field(min_length=36, max_length=36)


class BrowserInput(BaseModel):
    """Finite input vocabulary; no caller-supplied CDP, scripts or URLs."""
    model_config = ConfigDict(extra='forbid', strict=True)
    type: Literal['click', 'wheel', 'key', 'text', 'back', 'forward', 'reload', 'home']
    epoch: int = Field(ge=0)
    x: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    y: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    delta_x: float = Field(default=0, ge=-1600, le=1600, allow_inf_nan=False)
    delta_y: float = Field(default=0, ge=-1600, le=1600, allow_inf_nan=False)
    text: str = Field(default='', max_length=2000)
    key: Literal['Tab', 'Enter', 'Backspace', 'Delete', 'Escape', 'ArrowLeft', 'ArrowRight',
                 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown', 'a'] = 'Tab'
    shift: bool = False
    control: bool = False
    meta: bool = False


class LCWSession:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.viewer = None
        self.tickets = {}
        self.loading = False
        self.agent_operation = False
        self.launch_mode = 'existing'
        self.process = None
        self.playwright = self.browser = self.context = self.page = None
        self.config = {}
        self.owner_file = None
        self.owner = 'agent'
        self.state, self.message = 'idle', 'Open or reconnect to the dedicated LCW browser when ready.'
        self.portal_authenticated = False
        self.verified_url = None
        self.password_attempted = False
        self.login_attempted = False
        self.job = None
        self.task = None
        self.epoch = 0
        self.resume = asyncio.Event()
        self.resume.set()
        self.document = None
        self.document_id = None

    def set_document(self, document):
        self.document_id = document.extraction_id or str(uuid4())
        self.document = document.model_copy(deep=True, update={'extraction_id': self.document_id})
        return self.document

    def redact(self, text):
        for key in ('LCW_USERNAME', 'LCW_PASSWORD'):
            if self.config.get(key):
                text = re.sub(re.escape(self.config[key]), '[redacted]', text, flags=re.I)
        return re.sub(r'[\w.+-]+@[\w.-]+', '[redacted]', text)

    def active(self):
        return bool(self.page and not self.page.is_closed() and self.browser and self.browser.is_connected())

    def authenticated(self):
        return bool(self.active() and self.portal_authenticated and self.page.url == self.verified_url
                    and origin(self.page.url) == origin(self.config.get('LCW_URL', '')))

    def result(self):
        auth = self.authenticated()
        workflow = self.job['state'] if self.job else None
        state = workflow if workflow in {'inspecting', 'paused', 'failed', 'cancelled', 'completed'} and auth else self.state
        if not auth and state == 'authenticated':
            state = 'inspecting' if workflow == 'inspecting' else 'awaiting_authentication'
        return {'state': state, 'message': self.message, 'owner': self.owner, 'active': self.active(),
                'read_only': True, 'authenticated': auth, 'location': origin(self.page.url) if self.active() else None,
                'preview_available': self.active() and self.allowed_url(self.page.url), 'busy': self.lock.locked(),
                'epoch': self.epoch, 'loading': self.loading,
                'launch_mode': self.launch_mode, 'control_pending': self.agent_operation,
                'inspection': {k: v for k, v in self.job.items() if k not in {'source', 'result'}} if self.job else None,
                'document_id': self.document_id}

    def claim_profile(self):
        if self.owner_file:
            return
        PROFILE_PATH.mkdir(mode=0o700, parents=True, exist_ok=True)
        PROFILE_PATH.chmod(0o700)
        handle = (PROFILE_PATH / '.lcg-owner.lock').open('a+')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise HTTPException(409, 'Another backend owns the LCW browser. Use one worker without reload; no browser was launched.') from None
        self.owner_file = handle

    def release_profile(self):
        if self.owner_file:
            fcntl.flock(self.owner_file, fcntl.LOCK_UN)
            self.owner_file.close()
            self.owner_file = None

    def metadata(self):
        try:
            return json.loads((PROFILE_PATH / '.lcg-browser.json').read_text())
        except (OSError, ValueError):
            return {}

    def owned_process_alive(self, metadata):
        pid = metadata.get('pid')
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            command = subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='], stderr=subprocess.DEVNULL, timeout=2).decode()
            return bool(re.search(r'(?:^|\s)' + re.escape(f'--user-data-dir={PROFILE_PATH}') + r'(?:\s|$)', command))
        except (subprocess.SubprocessError, OSError):
            return False

    def save_metadata(self, data):
        target = PROFILE_PATH / '.lcg-browser.json'
        target.write_text(json.dumps(data))
        target.chmod(0o600)

    async def attach(self, metadata):
        self.launch_mode = metadata.get('mode', 'existing')
        # CDP stays loopback-only; neither endpoint nor cookies are exposed through the API.
        lines = (PROFILE_PATH / 'DevToolsActivePort').read_text().splitlines()
        if len(lines) != 2 or not lines[0].isdigit() or not 1 <= int(lines[0]) <= 65535 or not re.fullmatch(r'/devtools/browser/[\w-]+', lines[1]):
            raise OSError('invalid local endpoint')
        self.browser = await self.playwright.chromium.connect_over_cdp(f'ws://127.0.0.1:{lines[0]}{lines[1]}', timeout=8000)
        if len(self.browser.contexts) != 1:
            raise HTTPException(409, 'Unexpected browser contexts. Existing browser was left intact; resolve ownership locally.')
        self.context = self.browser.contexts[0]
        target = metadata.get('target')
        matches = []
        for page in self.context.pages:
            if page.is_closed():
                continue
            cdp = await self.context.new_cdp_session(page)
            try:
                info = await cdp.send('Target.getTargetInfo')
                if info['targetInfo']['targetId'] == target:
                    matches.append(page)
            finally:
                await cdp.detach()
        if target and len(matches) != 1:
            raise HTTPException(409, 'The controlled tab was closed. Existing tabs were not selected automatically. Close the browser explicitly before reopening.')
        if target:
            self.page = matches[0]
        else:
            pages = [p for p in self.context.pages if not p.is_closed()]
            if len(pages) != 1:
                raise HTTPException(409, 'Browser has multiple tabs without a controlled target. Resolve the local window before reconnecting.')
            self.page = pages[0]
            cdp = await self.context.new_cdp_session(self.page)
            try:
                metadata['target'] = (await cdp.send('Target.getTargetInfo'))['targetInfo']['targetId']
            finally:
                await cdp.detach()
            self.save_metadata(metadata)
        self.page.set_default_timeout(8000)
        self.page.on('dialog', lambda dialog: dialog.dismiss())
        await self.secure_page()
        # Additional tabs never become the agent target automatically.

    async def start(self, existing_only=False):
        async with self.lock:
            if self.task and not self.task.done() and not self.active():
                raise HTTPException(409, 'The inspection lost its browser. Stop inspection before explicitly reconnecting; no browser was launched.')
            if self.context and self.browser and self.browser.is_connected():
                if not self.page or self.page.is_closed():
                    raise HTTPException(409, 'The controlled tab is closed. Other tabs were left intact; close the browser explicitly before reopening.')
                return await self.observe()
            if self.playwright:
                await self.disconnect()
            self.config = dotenv_values(ENV_PATH, interpolate=False)
            target = self.config.get('LCW_URL', '')
            p = urlsplit(target)
            if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment:
                raise HTTPException(503, 'Configure an HTTPS LCW_URL without embedded credentials, query or fragment in backend/.env.')
            self.claim_profile()
            metadata = self.metadata()
            alive = self.owned_process_alive(metadata)
            if not alive and isinstance(metadata.get('pid'), int):
                try:
                    os.kill(metadata['pid'], 0)
                except ProcessLookupError:
                    pass
                except OSError:
                    self.release_profile()
                    raise HTTPException(409, 'Existing browser ownership could not be verified. No replacement process was launched.') from None
                else:
                    self.release_profile()
                    raise HTTPException(409, 'A recorded browser process is still running but ownership could not be verified. Resolve the browser panel before reopening.')
            if not alive and ((PROFILE_PATH / 'SingletonLock').exists() or (PROFILE_PATH / 'SingletonLock').is_symlink()):
                self.release_profile()
                raise HTTPException(409, 'The dedicated LCW profile is locked by an existing browser. No new browser was launched.')
            if not alive and existing_only:
                self.release_profile()
                return self.result()
            try:
                self.playwright = await async_playwright().start()
                if not alive:
                    # Never erase a Chromium lock or fall back to another profile.
                    if (PROFILE_PATH / 'SingletonLock').exists() or (PROFILE_PATH / 'SingletonLock').is_symlink():
                        raise HTTPException(409, 'The dedicated LCW profile is locked by an existing browser. No new browser was launched.')
                    endpoint_file = PROFILE_PATH / 'DevToolsActivePort'
                    previous_stamp = endpoint_file.stat().st_mtime_ns if endpoint_file.exists() else None
                    process = subprocess.Popen([
                        self.playwright.chromium.executable_path, f'--user-data-dir={PROFILE_PATH}',
                        '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                        '--headless=new', '--window-size=1280,800',
                        '--no-first-run', '--no-default-browser-check', 'about:blank',
                    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                    self.process = process
                    metadata = {'pid': process.pid, 'mode': 'headless'}
                    self.save_metadata(metadata)
                    # Bounded condition wait for Chromium's own endpoint file; no portal sleeps.
                    deadline = time.monotonic() + 10
                    while not endpoint_file.exists() or endpoint_file.stat().st_mtime_ns == previous_stamp:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise OSError('browser startup failed')
                        await asyncio.sleep(0.05)
                await self.attach(metadata)
                self.login_attempted = bool(metadata.get('login_attempted'))
                if not alive:
                    response = await self.page.goto(target, wait_until='domcontentloaded', timeout=20000)
                    if response and response.status >= 400:
                        raise InspectionError('LCW failed to load. The browser panel is preserved; check local network access.')
                return await self.observe()
            except HTTPException:
                await self.disconnect()
                raise
            except (PlaywrightError, OSError, InspectionError):
                self.state, self.message = 'failed', 'Could not connect to the dedicated LCW browser. Inspect local ownership/network access; no replacement session was launched.'
                await self.disconnect()
                raise HTTPException(503, self.message) from None
            finally:
                if not self.browser:
                    if self.playwright:
                        await self.playwright.stop()
                        self.playwright = None
                    self.release_profile()

    async def observe(self):
        self.portal_authenticated = False
        if not self.active():
            self.state, self.message = 'idle', 'No active controlled browser. Inspection requires an existing authenticated session.'
            return self.result()
        location = origin(self.page.url)
        if location in AUTH_ORIGINS:
            if await self.page.get_by_role('heading', name='Approve sign in request', exact=True).is_visible():
                self.state, self.message = 'awaiting_mfa', 'MFA required. Complete it manually manually in the browser panel when ready.'
            else:
                self.state, self.message = 'awaiting_authentication', 'Authentication required. No login will be attempted automatically.'
            self.owner = 'human'
        elif location != origin(self.config.get('LCW_URL', '')):
            self.state, self.message = 'failed', 'The controlled page is outside LCW. Return to the existing portal window; no alternate tab was selected.'
            self.owner = 'human'
        elif any(re.search(r'\bsession\s+(?:has\s+)?(?:expired|timed\s*out)\b', notice, re.I)
                 for notice in await self.page.locator('[role="alert"]:visible').all_text_contents()):
            self.state, self.message = 'awaiting_authentication', 'LCW reports session expiration. Inspection is paused; no login was attempted.'
            self.owner = 'human'
        elif (await self.page.locator('#userProfileMenu').is_visible()
              and await self.page.get_by_role('navigation').get_by_role('link', name='Family', exact=True).is_visible()):
            self.portal_authenticated = True
            self.verified_url = self.page.url
            self.state = 'authenticated'
            if not self.job:
                self.message = 'Authenticated LCW session verified. Ready for read-only inspection.'
        else:
            self.state, self.message = 'awaiting_authentication', 'LCW authentication could not be independently verified. Inspect the browser panel; no login was attempted.'
            self.owner = 'human'
        return self.result()

    async def status(self):
        if not self.lock.locked() and not (self.task and not self.task.done()):
            async with self.lock:
                try:
                    await self.observe()
                except PlaywrightError:
                    self.portal_authenticated = False
                    self.state, self.message = 'failed', 'The browser page changed or closed. Inspect the browser panel.'
        return self.result()

    def allowed_url(self, url):
        try:
            parsed = urlsplit(url)
            return (parsed.scheme == 'https' and not parsed.username and not parsed.password
                    and origin(url) in {origin(self.config.get('LCW_URL', '')), *AUTH_ORIGINS})
        except ValueError:
            return False

    async def secure_page(self):
        # Guard page/frame navigations, including redirects and links. Assets may
        # use the portal's CDNs. No wildcard identity-provider navigation grants.
        async def guard(route):
            request = route.request
            if request.is_navigation_request() and not self.allowed_url(request.url):
                self.owner = 'human'
                self.epoch += 1
                self.resume.clear()
                self.message = 'Navigation blocked outside LCW and supported Microsoft sign-in pages.'
                await route.abort()
            else:
                await route.fallback()
        await self.context.route('**/*', guard)
        async def popup(page):
            if page != self.page:
                await page.close()
                self.message = 'A separate tab was blocked. Use same-page sign-in; native-only authentication requires operator review.'
        self.context.on('page', popup)
        def navigated(frame):
            if frame == self.page.main_frame:
                self.portal_authenticated = False
                if origin(frame.url) != origin(self.config.get('LCW_URL', '')):
                    self.owner = 'human'
                    self.epoch += 1
                    self.resume.clear()
        self.page.on('framenavigated', navigated)
        cdp = await self.context.new_cdp_session(self.page)
        try:
            await cdp.send('Network.setBypassServiceWorker', {'bypass': True})
            await cdp.send('Page.setDownloadBehavior', {'behavior': 'deny'})
        finally:
            await cdp.detach()

    async def login(self):
        # Kept as a compatible lifecycle action, but credentials are never submitted.
        await self.take_control()
        self.message = 'Complete sign-in and MFA manually in the browser panel. No credentials were submitted.'
        return self.result()

    async def human_input(self, event, viewer):
        async with self.lock:
            if (self.viewer is not viewer or self.owner != 'human' or event.epoch != self.epoch
                    or not self.active() or (not self.allowed_url(self.page.url) and event.type not in {'home', 'back', 'forward'})):
                raise HTTPException(409, 'Input paused. Take control of the connected browser and retry.')
            x, y = min(event.x * VIEW_WIDTH, VIEW_WIDTH - 1), min(event.y * VIEW_HEIGHT, VIEW_HEIGHT - 1)
            if event.type == 'click':
                await self.page.mouse.click(x, y)
            elif event.type == 'wheel':
                await self.page.mouse.move(x, y)
                await self.page.mouse.wheel(event.delta_x, event.delta_y)
            elif event.type == 'text':
                await self.page.keyboard.insert_text(event.text)
            elif event.type == 'key':
                # Restrict shortcuts to editing/navigation; never browser/devtools shortcuts.
                if (event.control or event.meta) and event.key not in {'a', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'Backspace', 'Delete'}:
                    raise HTTPException(422, 'Unsupported keyboard shortcut.')
                prefix = ('Shift+' if event.shift else '') + ('Control+' if event.control else '') + ('Meta+' if event.meta else '')
                await self.page.keyboard.press(prefix + event.key)
            elif event.type == 'home':
                await self.page.goto(self.config['LCW_URL'], wait_until='commit', timeout=10000)
            elif event.type == 'reload':
                await self.page.reload(wait_until='commit', timeout=10000)
            else:
                # Check history destination before asking Chromium to navigate.
                cdp = await self.context.new_cdp_session(self.page)
                try:
                    history = await cdp.send('Page.getNavigationHistory')
                    index = history['currentIndex'] + (-1 if event.type == 'back' else 1)
                    if 0 <= index < len(history['entries']):
                        entry = history['entries'][index]
                        if self.allowed_url(entry['url']):
                            await cdp.send('Page.navigateToHistoryEntry', {'entryId': entry['id']})
                finally:
                    await cdp.detach()

    def viewer_ticket(self):
        now = time.monotonic()
        self.tickets = {key: expiry for key, expiry in self.tickets.items() if expiry > now}
        if len(self.tickets) >= 8:
            raise HTTPException(429, 'Too many pending viewer connections.')
        ticket = secrets.token_urlsafe(32)
        self.tickets[ticket] = now + 15
        return ticket

    async def stream(self, websocket):
        # Only one viewer receives sensitive frames and can submit human input.
        if self.viewer is not None:
            await websocket.close(code=4409, reason='Another Command Center viewer is connected.')
            return
        lease = object()
        self.viewer = lease
        cdp = None
        tasks = []
        pending = asyncio.Queue(maxsize=1)
        frame_ack = asyncio.Event()
        frame_ack.set()
        async def receive_frame(event):
            # Keep at most the latest frame; no frame ever goes to disk or logs.
            old = pending.get_nowait() if pending.full() else None
            pending.put_nowait(event)
            if old:
                with suppress(PlaywrightError):
                    await cdp.send('Page.screencastFrameAck', {'sessionId': old['sessionId']})
        async def send_frames():
            while self.active():
                await websocket.send_json({'type': 'status', 'session': self.result()})
                try:
                    event = await asyncio.wait_for(pending.get(), 1)
                except asyncio.TimeoutError:
                    continue
                if not self.active():
                    return
                if self.allowed_url(self.page.url):
                    await asyncio.wait_for(frame_ack.wait(), 10)
                    frame_ack.clear()
                    await websocket.send_json({'type': 'frame', 'data': event['data']})
                await cdp.send('Page.screencastFrameAck', {'sessionId': event['sessionId']})
                await asyncio.sleep(0.05)  # Cap transport at 20 fps; Chromium emits on change.
        async def receive_inputs():
            while True:
                raw = await asyncio.wait_for(websocket.receive_text(), 20)
                if len(raw) > 12000:
                    await websocket.close(code=1009)
                    return
                try:
                    data = json.loads(raw)
                    if data == {'type': 'ack'}:
                        frame_ack.set()
                        continue
                    if data == {'type': 'ping'}:
                        continue
                    event = BrowserInput.model_validate(data)
                    await asyncio.wait_for(self.human_input(event, lease), 15)
                    await websocket.send_json({'type': 'input_ack'})
                except (ValueError, ValidationError):
                    await websocket.send_json({'type': 'input_error', 'message': 'Unsupported browser input.'})
                except (HTTPException, PlaywrightError, asyncio.TimeoutError):
                    await websocket.send_json({'type': 'input_error', 'message': 'Input paused or interrupted. Check control and browser status before retrying.'})
        try:
            if not self.active():
                await websocket.close(code=4410, reason='Reconnect the existing browser first.')
                return
            async with self.lock:
                cdp = await self.context.new_cdp_session(self.page)
                await self.page.set_viewport_size({'width': VIEW_WIDTH, 'height': VIEW_HEIGHT})
                cdp.on('Page.screencastFrame', receive_frame)
                cdp.on('Page.frameStartedLoading', lambda _: setattr(self, 'loading', True))
                cdp.on('Page.frameStoppedLoading', lambda _: setattr(self, 'loading', False))
                await cdp.send('Page.enable')
                await cdp.send('Page.startScreencast', {'format': 'jpeg', 'quality': 75, 'maxWidth': VIEW_WIDTH, 'maxHeight': VIEW_HEIGHT})
            tasks = [asyncio.create_task(send_frames()), asyncio.create_task(receive_inputs())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, PlaywrightError, OSError, RuntimeError, KeyError, asyncio.TimeoutError):
            pass  # Never expose protocol errors, frame contents or input values.
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if cdp:
                cdp.remove_listener('Page.screencastFrame', receive_frame)
                with suppress(PlaywrightError):
                    await cdp.send('Page.stopScreencast')
                    await cdp.detach()
            self.viewer = None
            # Transport loss never launches, closes, logs in, or resumes automation.
            with suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close()

    def progress(self, stage, state='inspecting'):
        self.job['state'], self.job['stage'] = state, stage
        self.job['version'] += 1
        self.job['events'].append({'stage': stage, 'state': state, 'at': datetime.now(timezone.utc).isoformat()})
        self.job['events'] = self.job['events'][-30:]
        self.message = stage
        if database.configured():
            try:
                database.save_workflow(self.job)
            except HTTPException:
                self.owner = 'human'
                self.epoch += 1
                self.resume.clear()
                self.job['state'] = 'failed'
                self.message = self.job['stage'] = 'Workflow stopped because its state could not be persisted. Restore database access before retrying.'

    async def step(self, stage, operation, epoch):
        if self.owner != 'agent' or epoch != self.epoch:
            raise Paused()
        async with self.lock:
            self.agent_operation = True
            try:
                if self.owner != 'agent' or epoch != self.epoch:
                    raise Paused()
                await self.observe()
                if not self.authenticated():
                    raise AuthenticationRequired()
                if self.owner != 'agent' or epoch != self.epoch:
                    raise Paused()
                self.progress(stage)
                if self.owner != 'agent':
                    raise Paused()
                value = await asyncio.wait_for(operation(), timeout=12)
                # Ownership may have changed while the single bounded operation awaited.
                if self.owner != 'agent' or epoch != self.epoch:
                    raise Paused()
                return value
            finally:
                self.agent_operation = False

    async def begin(self, request):
        async with self.lock:
            if self.task and not self.task.done():
                if self.job['extraction_id'] == request.extraction_id:
                    return self.result()
                raise HTTPException(409, 'An inspection is already active. Take control or wait before inspecting a different document.')
            if self.job and self.job['request_id'] == request.request_id:
                return self.result()
            if not self.document or request.extraction_id != self.document_id:
                raise HTTPException(409, 'The uploaded extraction is unavailable or changed. Upload/review the PDF before inspecting.')
            try:
                expected_identity(self.document)
            except InspectionError as error:
                raise HTTPException(422, str(error)) from None
            await self.observe()
            if not self.authenticated():
                raise HTTPException(409, 'No authenticated controlled LCW session is available. Inspection did not start and no login was attempted.')
            if self.owner != 'agent':
                raise HTTPException(409, 'Human control is active. Return control to the agent before inspection.')
            self.job = {'id': str(uuid4()), 'request_id': request.request_id, 'extraction_id': self.document_id,
                        'state': 'inspecting', 'stage': 'Inspection requested', 'version': 0, 'events': [],
                        'source': self.document.model_copy(deep=True), 'result': None}
            if database.configured():
                try:
                    database.save_workflow(self.job)
                except HTTPException:
                    self.job = None
                    raise
            self.resume.set()
            self.task = asyncio.create_task(self.run_inspection())
            return self.result()

    async def run_inspection(self):
        while True:
            await self.resume.wait()
            try:
                result = await asyncio.wait_for(self.inspect_records(self.job['source'], self.epoch), timeout=120)
                self.job['result'] = result
                self.progress('Read-only comparison completed. Review differences and unverified fields.', 'completed')
                return
            except Paused:
                if self.owner == 'human':
                    self.progress('Inspection paused for human control. Return control to reverify and restart the read-only inspection.', 'paused')
                    self.resume.clear()
            except AuthenticationRequired:
                self.progress('Authentication is required in the browser panel. Inspection paused; no login was attempted.', self.state)
                self.owner = 'human'
                self.resume.clear()
            except (PlaywrightTimeout, asyncio.TimeoutError):
                self.progress('LCW did not respond in time. The session is preserved; return control and explicitly retry inspection.', 'failed')
                self.owner = 'human'
                return
            except InspectionError as error:
                self.progress(str(error), 'failed')
                return
            except PlaywrightError:
                self.progress('The browser changed, closed or failed during inspection. Check the browser panel before retrying.', 'failed')
                self.portal_authenticated = False
                self.owner = 'human'
                return
            except asyncio.CancelledError:
                self.progress('Inspection interrupted by backend shutdown. Browser was retained; start a new inspection after reconnecting.', 'paused')
                raise
            except Exception:
                # Never leak raw HTML, URLs, identifiers or Playwright call logs via errors.
                self.progress('The portal layout could not be safely inspected. Review the browser panel before retrying.', 'failed')
                return

    async def inspect_records(self, source, epoch):
        expected = expected_identity(source)
        async def click(locator):
            if self.owner != 'agent' or epoch != self.epoch:
                raise Paused()
            await locator.click(timeout=8000)
        async def ready():
            await self.page.locator('#userProfileMenu').wait_for(state='visible', timeout=8000)
        await self.step('Opening the verified family search menu', lambda: click(self.page.get_by_role('link', name='\uf002', exact=True)), epoch)
        await self.step('Searching the PDF family alphabetically', lambda: click(self.page.get_by_role('button', name=search_group(expected['family_name']), exact=True)), epoch)
        async def read_search():
            await self.page.get_by_text(re.compile(r'^Family Search Results')).first.wait_for(timeout=8000)
            try:
                await self.page.locator('a[href*="cgchild.do?child_id="]').first.wait_for(state='visible', timeout=8000)
            except PlaywrightTimeout:
                pass  # Read actual results and report not found; never select another child.
            return await self.page.locator('a[href]').evaluate_all("els => els.filter(e=>e.getClientRects().length).map(e=>({href:e.getAttribute('href'),label:e.innerText.trim()}))")
        links = await self.step('Reading child identifiers in the search results', read_search, epoch)
        selected = child_links(links, expected)
        parsed = urlsplit(selected['href'])
        if parsed.scheme or parsed.netloc or parsed.path not in {'cgchild.do', '/center/cgchild.do'} or set(parse_qs(parsed.query)) != {'child_id'}:
            raise InspectionError('The child link target is not a recognized read-only detail route.')
        await self.step('Opening the child record selected by ID', lambda: self.click_observed_link(selected['href']), epoch)
        async def identity():
            await ready()
            await self.page.get_by_role('textbox', name='First Name*', exact=True).wait_for(timeout=8000)
            return verify_identity(await self.page.locator('body').inner_text(),
                                   await self.page.get_by_role('textbox', name='First Name*', exact=True).input_value(),
                                   await self.page.get_by_role('textbox', name='Last Name*', exact=True).input_value(), expected)
        verified = await self.step('Verifying child ID, family ID, name and center', identity, epoch)
        await self.step('Opening the existing Program/Room Assignment', lambda: click(self.page.get_by_role('tab', name='Program/Room Assignment', exact=True)), epoch)
        async def assignments():
            await self.page.get_by_text(re.compile(r'^Active Classroom Assignments')).first.wait_for(timeout=8000)
            try:
                await self.page.locator('table tr').filter(has_text=re.compile(r'\s~\s')).filter(has_text=re.compile(r'\d{2}/\d{2}/\d{4}')).first.wait_for(state='visible', timeout=8000)
            except PlaywrightTimeout:
                pass  # Empty-state mapping is unverified; do not infer absence.
            text = await self.page.locator('body').inner_text()
            verify_family_header(text, expected)
            current_child = self.page.get_by_role('link', name=re.compile('^' + re.escape(expected['child_name']) + r'\s+Age\b'))
            child_query = parse_qs(urlsplit(self.page.url).query).get('child_id')
            if (await current_child.count() != 1 or not await current_child.is_visible()
                    or (child_query is not None and child_query != [expected['child_id']])):
                raise InspectionError('The active child context changed before reading assignments. Inspection stopped.')
            rows = await self.rows()
            for row in rows:
                for link in row['links']:
                    target = urlsplit(link['href'])
                    if target.path == '/viewQuestionsAnswersPDF' and parse_qs(target.query).get('childId') != [expected['child_id']]:
                        raise InspectionError('An assignment identifies a different child. Inspection stopped.')
            records = parse_pra_rows(rows)
            if any(normalized(record['center']) != normalized(expected['center']) for record in records):
                raise InspectionError('An assignment belongs to a different center. Inspection stopped for manual review.')
            # Empty-state selectors are not yet verified. A global "0 records"
            # may belong to another table; do not turn it into confirmed absence.
            return records, bool(records)
        pra, pra_verified = await self.step('Reading existing assignment dates, classroom and displayed rate', assignments, epoch)
        await self.step('Opening the family Third Party section', lambda: click(self.page.get_by_role('tab', name='Third Party', exact=True)), epoch)
        async def view_contracts():
            await self.page.get_by_text('Third Party Contract List', exact=True).wait_for(timeout=8000)
            verify_family_header(await self.page.locator('body').inner_text(), expected)
            links = await self.page.locator('a[href]').evaluate_all("els => els.filter(e=>e.getClientRects().length).map(e=>({href:e.getAttribute('href'),label:e.innerText.trim()}))")
            pattern = r'javascript:viewContracts\(' + re.escape(expected['child_id']) + r",'([^']*)'\);?"
            matches = [link for link in links if re.fullmatch(pattern, link['href'])]
            if len(matches) != 1:
                raise InspectionError('The child contract-list control is missing or ambiguous. No add/edit action was attempted.')
            await self.click_observed_link(matches[0]['href'])
            return re.fullmatch(pattern, matches[0]['href'])[1]
        display_name = await self.step('Opening the verified child contract list', view_contracts, epoch)
        async def contracts():
            await self.page.get_by_text(f'Third Party Contract List ({display_name})', exact=True).wait_for(timeout=8000)
            try:
                await self.page.locator('a[href^="javascript:editContract("]').first.wait_for(state='visible', timeout=8000)
            except PlaywrightTimeout:
                pass  # Report unreadable/empty list as not verified, without opening edit.
            text = await self.page.locator('body').inner_text()
            verify_family_header(text, expected)
            records = parse_contract_rows(await self.rows())
            return records, bool(records)
        contracts, contracts_verified = await self.step('Reading existing contract IDs, agency and dates', contracts, epoch)
        await self.step('Confirming the authenticated portal before comparison', ready, epoch)
        return build_result(source, verified, contracts, pra, contracts_verified, pra_verified)

    async def click_observed_link(self, href):
        # JSON quoting is CSS attribute quoting here; href must come from inspected DOM.
        locator = self.page.locator('a[href=' + json.dumps(href) + ']')
        if await locator.count() != 1:
            raise InspectionError('The observed read-only navigation link is no longer unique. Inspection stopped.')
        if self.owner != 'agent':
            raise Paused()
        await locator.click(timeout=8000)

    async def rows(self):
        return await self.page.locator('table tr').evaluate_all("""rows => rows.filter(r=>r.getClientRects().length).map(r=>({
            cells:Array.from(r.cells).map(c=>c.innerText.trim()),
            links:Array.from(r.querySelectorAll('a[href]')).map(a=>({href:a.getAttribute('href')}))
        }))""")

    async def take_control(self):
        self.owner = 'human'
        self.epoch += 1
        self.resume.clear()
        if self.job and self.task and not self.task.done():
            self.progress('Take Control requested. Pausing after the current bounded browser operation.', 'paused')
        return self.result()

    async def return_control(self):
        requested_epoch = self.epoch
        async with self.lock:
            await self.observe()
            if not self.authenticated() or requested_epoch != self.epoch:
                return self.result()
            self.owner = 'agent'
            self.epoch += 1
            self.resume.set()
            return self.result()

    async def cancel_inspection(self):
        self.owner = 'human'
        self.epoch += 1
        self.resume.clear()
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.job:
            self.progress('Inspection stopped by the operator. Session and PDF are retained; return control before starting a new inspection.', 'cancelled')
        return self.result()

    async def navigation(self):
        if self.lock.locked():
            raise HTTPException(409, 'Navigation is in progress. Retry reading links at the next boundary.')
        async with self.lock:
            await self.observe()
            if not self.authenticated():
                raise HTTPException(409, 'An authenticated controlled LCW page is required; no login was attempted.')
            labels = await self.page.locator('a').evaluate_all("els => els.filter(e=>e.getClientRects().length && e.innerText.trim()).map(e=>e.innerText.trim().slice(0,100)).slice(0,100)")
            return {'verified': False, 'links': [{'label': self.redact(label)} for label in labels]}

    async def disconnect(self):
        """Detach transport only. Never close a browser on failed attach/reload."""
        try:
            if self.playwright:
                await self.playwright.stop()
        except PlaywrightError:
            pass  # An already-lost transport must not retain backend ownership.
        finally:
            self.playwright = self.browser = self.context = self.page = None
            self.portal_authenticated = False
            self.release_profile()

    async def shutdown(self):
        # Stop API automation, disconnect Playwright, retain the independently owned
        # local Chromium process. The next backend reconnects to the exact target.
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.disconnect()

    async def close(self):
        if self.task and not self.task.done():
            raise HTTPException(409, 'An inspection is active or paused. Finish it or stop the backend before explicitly closing the browser.')
        async with self.lock:
            if self.browser:
                # Browser.close on a CDP attachment disconnects rather than closing
                # Chromium; Browser.close via its internal CDP session closes it.
                try:
                    cdp = await self.browser.new_browser_cdp_session()
                    await cdp.send('Browser.close')
                except PlaywrightError:
                    if self.browser.is_connected():
                        raise HTTPException(409, 'Browser close was not confirmed. The browser panel and profile were retained.') from None
            if self.process:
                try:
                    await asyncio.to_thread(self.process.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    raise HTTPException(409, 'Browser is still exiting. The profile is retained; wait before reopening.') from None
                self.process = None
            await self.shutdown()
            self.state, self.message = 'idle', 'Browser explicitly closed. The dedicated profile was retained; no logout was performed.'
            return self.result()


session = LCWSession()
router = APIRouter(prefix='/api/browser', dependencies=[Depends(local_request)])


@router.get('/status')
async def status(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return await session.status()


@router.post('/viewer-ticket')
async def viewer_ticket(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return {'ticket': session.viewer_ticket()}


# WebSockets have their own mandatory Origin/Host/loopback guard (browsers cannot
# add X-LCG-Local to a WebSocket). The one-use ticket arrives in the first message,
# never a URL, cookie, access log or localStorage.
viewer_router = APIRouter()


@viewer_router.websocket('/api/browser/stream')
async def browser_stream(websocket: WebSocket):
    if (websocket.client is None or websocket.client.host not in {'127.0.0.1', '::1', 'testclient'}
            or websocket.url.hostname not in {'localhost', '127.0.0.1', '::1', 'testserver'}
            or websocket.headers.get('origin') not in LOCAL_ORIGINS):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    try:
        ticket = await asyncio.wait_for(websocket.receive_text(), 5)
        if len(ticket) > 100 or session.tickets.pop(ticket, 0) <= time.monotonic():
            await websocket.close(code=4403)
            return
        await session.stream(websocket)
    except (WebSocketDisconnect, KeyError, asyncio.TimeoutError):
        with suppress(RuntimeError):
            await websocket.close(code=4403)


@router.get('/document')
async def document(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return session.document


@router.get('/inspection')
async def inspection(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    if not session.job:
        return None
    return {**{k: v for k, v in session.job.items() if k != 'source'},
            'source': session.job['source'].model_dump(mode='json')}


@router.get('/navigation')
async def navigation(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return await session.navigation()
    except PlaywrightError:
        raise HTTPException(409, 'The page changed while reading navigation. Inspect the browser panel.') from None


@router.post('/inspect', status_code=202)
async def inspect(request: InspectRequest, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return await session.begin(request)
    except PlaywrightError:
        raise HTTPException(409, 'The browser changed or closed. No inspection or login was started.') from None


@router.post('/{action}')
async def action(action: str, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    if action not in {'start', 'reconnect', 'login', 'take-control', 'return-control', 'cancel-inspection', 'close'}:
        raise HTTPException(404, 'Unsupported browser action. LCW record changes are not implemented.')
    return await perform(action)


async def perform(action):
    try:
        if action in {'start', 'reconnect'}:
            return await session.start(existing_only=action == 'reconnect')
        if action == 'login':
            return await session.login()
        if action == 'take-control':
            return await session.take_control()
        if action == 'return-control':
            return await session.return_control()
        if action == 'cancel-inspection':
            return await session.cancel_inspection()
        if action == 'close':
            return await session.close()
        return await session.status()
    except PlaywrightTimeout:
        session.owner = 'human'
        raise HTTPException(504, 'The page timed out. Inspect the browser panel before explicitly retrying.') from None
    except PlaywrightError:
        session.owner = 'human'
        session.portal_authenticated = False
        raise HTTPException(409, 'The controlled browser changed or closed. No replacement browser or login was started.') from None
