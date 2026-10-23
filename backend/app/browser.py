"""Single local Chromium owner, interruptible read-only LCW inspection."""
import asyncio
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
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout, async_playwright

from .contracts import ContractExtraction
from .inspection import (InspectionError, build_result, child_links, expected_identity, normalized,
                         parse_contract_rows, parse_pra_rows, search_group, verify_family_header, verify_identity)

ENV_PATH = Path(__file__).resolve().parents[1] / '.env'
PROFILE_PATH = ENV_PATH.parent / '.lcw-browser-profile'
AUTH_ORIGIN = 'https://login.microsoftonline.com'
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


class LCWSession:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.frame_lock = asyncio.Lock()
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
        self.last_frame = None
        self.last_frame_at = 0.0
        self.last_frame_url = None
        self.last_frame_captured = None

    def set_document(self, document):
        self.document_id = str(uuid4())
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
                'preview_available': auth, 'busy': self.lock.locked(),
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
                    raise HTTPException(409, 'A recorded browser process is still running but ownership could not be verified. Resolve the existing window before reopening.')
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
                        '--no-first-run', '--no-default-browser-check', 'about:blank',
                    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                    metadata = {'pid': process.pid}
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
                        raise InspectionError('LCW failed to load. The existing window is preserved; check local network access.')
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
        if location == AUTH_ORIGIN:
            if await self.page.get_by_role('heading', name='Approve sign in request', exact=True).is_visible():
                self.state, self.message = 'awaiting_mfa', 'MFA required. Complete it manually in the existing window only when ready.'
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
            self.state, self.message = 'awaiting_authentication', 'LCW authentication could not be independently verified. Inspect the existing window; no login was attempted.'
            self.owner = 'human'
        return self.result()

    async def status(self):
        if not self.lock.locked() and not (self.task and not self.task.done()):
            async with self.lock:
                try:
                    await self.observe()
                except PlaywrightError:
                    self.portal_authenticated = False
                    self.state, self.message = 'failed', 'The browser page changed or closed. Inspect the existing window.'
        return self.result()

    async def login(self):
        async with self.lock:
            await self.observe()
            if self.authenticated():
                return self.result()
            if self.state == 'awaiting_mfa' or self.login_attempted:
                raise HTTPException(409, 'A challenge or previous login attempt is pending. Use the existing window; credentials will not be resubmitted.')
            if self.state != 'awaiting_authentication' or origin(self.page.url) != AUTH_ORIGIN:
                raise HTTPException(409, 'Observed Microsoft sign-in is required. No login attempt was made.')
            if not self.config.get('LCW_USERNAME') or not self.config.get('LCW_PASSWORD'):
                raise HTTPException(503, 'Configured credentials are unavailable. Use the existing window manually.')
            self.login_attempted = True
            metadata = self.metadata()
            metadata['login_attempted'] = True
            self.save_metadata(metadata)
            if await self.page.locator('#i0116').is_visible():
                await self.page.locator('#i0116').fill(self.config['LCW_USERNAME'])
                await self.page.get_by_role('button', name='Next', exact=True).click()
                await self.page.locator('#i0118').wait_for(state='visible', timeout=8000)
            if await self.page.locator('#i0118').is_visible():
                self.password_attempted = True
                await self.page.locator('#i0118').fill(self.config['LCW_PASSWORD'])
                await self.page.get_by_role('button', name='Sign in', exact=True).click()
            self.owner = 'human'
            return await self.observe()

    async def frame(self):
        async with self.frame_lock:
            if not self.lock.locked():
                async with self.lock:
                    await self.observe()
            if not self.authenticated():
                self.last_frame = None
                raise HTTPException(409, 'Preview paused until the existing session is authenticated.')
            if self.last_frame and self.last_frame_url == self.page.url and time.monotonic() - self.last_frame_at < 1:
                return self.last_frame
            captured_url = self.page.url
            image = await self.page.screenshot(type='jpeg', quality=65, timeout=4000,
                                               mask=[self.page.locator('input[type="password"], #userProfileMenu')])
            if not self.authenticated() or self.page.url != captured_url:
                self.last_frame = None
                raise HTTPException(409, 'The page navigated during capture; the frame was discarded.')
            self.last_frame = image
            self.last_frame_at = time.monotonic()
            self.last_frame_url = captured_url
            self.last_frame_captured = datetime.now(timezone.utc).isoformat()
            return self.last_frame

    def progress(self, stage, state='inspecting'):
        self.job['state'], self.job['stage'] = state, stage
        self.job['version'] += 1
        self.job['events'].append({'stage': stage, 'state': state, 'at': datetime.now(timezone.utc).isoformat()})
        self.job['events'] = self.job['events'][-30:]
        self.message = stage

    async def step(self, stage, operation, epoch):
        if self.owner != 'agent' or epoch != self.epoch:
            raise Paused()
        async with self.lock:
            if self.owner != 'agent' or epoch != self.epoch:
                raise Paused()
            await self.observe()
            if not self.authenticated():
                raise AuthenticationRequired()
            if self.owner != 'agent' or epoch != self.epoch:
                raise Paused()
            self.progress(stage)
            value = await asyncio.wait_for(operation(), timeout=12)
            # Ownership may have changed while the single bounded operation awaited.
            if self.owner != 'agent' or epoch != self.epoch:
                raise Paused()
            return value

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
                self.progress('Authentication is required in the existing window. Inspection paused; no login was attempted.', self.state)
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
                self.progress('The browser changed, closed or failed during inspection. Check the existing window before retrying.', 'failed')
                self.portal_authenticated = False
                self.owner = 'human'
                return
            except asyncio.CancelledError:
                self.progress('Inspection interrupted by backend shutdown. Browser was retained; start a new inspection after reconnecting.', 'paused')
                raise
            except Exception:
                # Never leak raw HTML, URLs, identifiers or Playwright call logs via errors.
                self.progress('The portal layout could not be safely inspected. Review the existing window before retrying.', 'failed')
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
        if not self.lock.locked() and self.active():
            async with self.lock:
                await self.page.bring_to_front()
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
            self.last_frame = None
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
                        raise HTTPException(409, 'Browser close was not confirmed. The existing window and profile were retained.') from None
            await self.shutdown()
            self.state, self.message = 'idle', 'Browser explicitly closed. The dedicated profile was retained; no logout was performed.'
            return self.result()


session = LCWSession()
router = APIRouter(prefix='/api/browser', dependencies=[Depends(local_request)])


@router.get('/status')
async def status(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return await session.status()


@router.get('/frame')
async def frame():
    try:
        image = await session.frame()
        return Response(image, media_type='image/jpeg', headers={
            'Cache-Control': 'no-store', 'X-LCG-Captured-At': session.last_frame_captured or '',
        })
    except PlaywrightError:
        raise HTTPException(409, 'Preview interrupted by page navigation or closure. The existing session is preserved.') from None


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
        raise HTTPException(409, 'The page changed while reading navigation. Inspect the existing window.') from None


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
        raise HTTPException(504, 'The page timed out. Inspect the existing window before explicitly retrying.') from None
    except PlaywrightError:
        session.owner = 'human'
        session.portal_authenticated = False
        raise HTTPException(409, 'The controlled browser changed or closed. No replacement browser or login was started.') from None
