"""Offline only: synthetic records and mocked Playwright; never launches Chromium."""
import asyncio
import json
import re
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from app import browser as module
from app.contracts import extract_contract
from app.inspection import (InspectionError, build_result, child_links, expected_identity, parse_contract_rows,
                            parse_pra_rows, search_group, verify_identity)
from app.main import app
from test_contracts import pdf

HEADERS = {'X-LCG-Local': '1'}


def source():
    return extract_contract(pdf())


def attached():
    s = module.LCWSession()
    s.config = {'LCW_URL': 'https://portal.example/center/daycare.do'}
    s.page = MagicMock(url='https://portal.example/center/daycare.do')
    s.page.is_closed.return_value = False
    s.browser = MagicMock()
    s.browser.is_connected.return_value = True
    s.context = MagicMock()
    s.portal_authenticated = True
    s.verified_url = s.page.url
    s.state = 'authenticated'
    async def observe():
        return s.result()
    s.observe = AsyncMock(side_effect=observe)
    return s


def request(s, request_id=None):
    return module.InspectRequest(extraction_id=s.document_id, request_id=request_id or str(uuid4()))


def row(cells, href=None):
    return {'cells': cells, 'links': [{'href': href}] if href else []}


def test_browser_api_rejects_untrusted_requests_and_record_writes():
    client = TestClient(app)
    for route in ('status', 'inspection', 'document', 'frame'):
        assert client.get('/api/browser/' + route).status_code == 403
    assert client.post('/api/browser/start', headers={**HEADERS, 'Origin': 'https://untrusted.example'}).status_code == 403
    assert client.get('/api/browser/status', headers={**HEADERS, 'Host': 'untrusted.example'}).status_code == 403
    for action in ('save', 'edit', 'navigate', 'evaluate', 'clear-cookies'):
        assert client.post('/api/browser/' + action, headers=HEADERS).status_code == 404
    response = client.get('/api/browser/status', headers=HEADERS)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert client.post('/api/browser/inspect', headers=HEADERS, json={'extraction_id': str(uuid4()), 'request_id': str(uuid4()), 'url': 'https://evil.example'}).status_code == 422


def test_reuses_active_page_even_with_concurrent_starts():
    s = attached()
    s.page.goto = AsyncMock()
    async def run():
        await asyncio.gather(*(s.start() for _ in range(10)))
    asyncio.run(run())
    s.page.goto.assert_not_awaited()
    s.context.new_page.assert_not_called()
    assert s.owner_file is None


def test_profile_path_lock_blocks_second_backend_without_launching(monkeypatch, tmp_path):
    monkeypatch.setattr(module, 'PROFILE_PATH', tmp_path / 'profile')
    a, b = module.LCWSession(), module.LCWSession()
    a.claim_profile()
    try:
        with pytest.raises(HTTPException) as blocked:
            b.claim_profile()
        assert blocked.value.status_code == 409
        assert a.owner_file is not None and b.owner_file is None
        assert (tmp_path / 'profile').stat().st_mode & 0o777 == 0o700
    finally:
        a.release_profile()
    b.claim_profile()
    b.release_profile()


def test_existing_only_never_launches_or_logs_in(monkeypatch, tmp_path):
    monkeypatch.setattr(module, 'PROFILE_PATH', tmp_path / 'profile')
    monkeypatch.setattr(module, 'dotenv_values', lambda *a, **k: {'LCW_URL': 'https://portal.example/'})
    launch = MagicMock()
    monkeypatch.setattr(module, 'async_playwright', launch)
    s = module.LCWSession()
    s.owned_process_alive = MagicMock(return_value=False)
    asyncio.run(s.start(existing_only=True))
    launch.assert_not_called()
    assert s.owner_file is None


def test_locked_chromium_profile_never_creates_fallback(monkeypatch, tmp_path):
    path = tmp_path / 'profile'
    path.mkdir()
    (path / 'SingletonLock').symlink_to('missing-owner')
    monkeypatch.setattr(module, 'PROFILE_PATH', path)
    monkeypatch.setattr(module, 'dotenv_values', lambda *a, **k: {'LCW_URL': 'https://portal.example/'})
    engine = MagicMock(stop=AsyncMock())
    monkeypatch.setattr(module, 'async_playwright', lambda: MagicMock(start=AsyncMock(return_value=engine)))
    popen = MagicMock()
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    s = module.LCWSession()
    with pytest.raises(HTTPException) as failure:
        asyncio.run(s.start())
    assert failure.value.status_code == 409
    popen.assert_not_called()
    assert s.owner_file is None
    assert (path / 'SingletonLock').is_symlink()


def test_reconnect_uses_existing_process_and_exact_target_without_navigation(monkeypatch, tmp_path):
    path = tmp_path / 'profile'
    path.mkdir()
    monkeypatch.setattr(module, 'PROFILE_PATH', path)
    monkeypatch.setattr(module, 'dotenv_values', lambda *a, **k: {'LCW_URL': 'https://portal.example/'})
    s = module.LCWSession()
    s.metadata = MagicMock(return_value={'pid': 999, 'target': 'observed-target'})
    s.owned_process_alive = MagicMock(return_value=True)
    engine = MagicMock(stop=AsyncMock())
    monkeypatch.setattr(module, 'async_playwright', lambda: MagicMock(start=AsyncMock(return_value=engine)))
    async def attach(meta):
        assert meta['target'] == 'observed-target'
        s.browser, s.context, s.page = MagicMock(), MagicMock(), MagicMock()
        s.page.is_closed.return_value = False
        s.page.goto = AsyncMock()
    s.attach = AsyncMock(side_effect=attach)
    s.observe = AsyncMock(return_value={'state': 'authenticated'})
    popen = MagicMock()
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    asyncio.run(s.start(existing_only=True))
    popen.assert_not_called()
    s.page.goto.assert_not_awaited()
    s.release_profile()


def test_backend_shutdown_disconnects_without_closing_browser():
    s = attached()
    s.playwright = MagicMock(stop=AsyncMock())
    browser, context = s.browser, s.context
    asyncio.run(s.shutdown())
    browser.close.assert_not_called()
    context.close.assert_not_called()
    assert s.page is None


def test_closed_controlled_tab_does_not_switch_to_other_tabs():
    s = attached()
    s.page.is_closed.return_value = True
    with pytest.raises(HTTPException):
        asyncio.run(s.start())
    s.context.new_page.assert_not_called()
    s.page.goto.assert_not_called()


def test_status_upload_and_inspect_never_initiate_login():
    s = module.LCWSession()
    s.login = AsyncMock()
    s.start = AsyncMock()
    s.set_document(source())
    async def run():
        for _ in range(3):
            await s.status()
        with pytest.raises(HTTPException) as blocked:
            await s.begin(request(s))
        assert blocked.value.status_code == 409
    asyncio.run(run())
    s.login.assert_not_awaited()
    s.start.assert_not_awaited()


def test_auth_is_false_after_navigation_or_browser_closure():
    s = attached()
    assert s.result()['authenticated']
    s.page.url = 'https://login.microsoftonline.com/challenge'
    assert not s.result()['authenticated']
    assert not s.result()['preview_available']
    assert s.result()['state'] != 'authenticated'
    s.page.is_closed.return_value = True
    assert not s.result()['active']


def test_mfa_blocks_preview_and_repeated_login():
    s = attached()
    s.state, s.owner, s.portal_authenticated = 'awaiting_mfa', 'human', False
    s.page.url = module.AUTH_ORIGIN + '/challenge'
    s.page.screenshot = AsyncMock()
    with pytest.raises(HTTPException):
        asyncio.run(s.login())
    with pytest.raises(HTTPException):
        asyncio.run(s.frame())
    s.page.screenshot.assert_not_awaited()
    s.page.locator.assert_not_called()


def test_password_not_automatically_retried_after_prior_attempt():
    s = attached()
    s.state, s.portal_authenticated = 'awaiting_authentication', False
    s.page.url = module.AUTH_ORIGIN + '/login'
    s.login_attempted = True
    with pytest.raises(HTTPException):
        asyncio.run(s.login())
    s.page.locator.assert_not_called()


def test_duplicate_inspect_requests_share_one_task_and_source_is_snapshot():
    s = attached()
    doc = s.set_document(source())
    release = asyncio.Event()
    async def inspect(*args):
        await release.wait()
        return {'synthetic': True}
    s.inspect_records = AsyncMock(side_effect=inspect)
    async def run():
        responses = await asyncio.gather(*(s.begin(request(s)) for _ in range(5)))
        assert len({r['inspection']['id'] for r in responses}) == 1
        assert s.job['source'] is not s.document
        s.set_document(source())
        assert s.job['extraction_id'] == doc.extraction_id != s.document_id
        with pytest.raises(HTTPException):
            await s.begin(request(s))
        release.set()
        await s.task
    asyncio.run(run())
    s.inspect_records.assert_awaited_once()
    assert s.job['state'] == 'completed'


def test_completed_request_id_is_idempotent():
    s = attached()
    s.set_document(source())
    s.inspect_records = AsyncMock(return_value={'synthetic': True})
    async def run():
        req = request(s)
        await s.begin(req)
        await s.task
        await s.begin(req)
    asyncio.run(run())
    s.inspect_records.assert_awaited_once()


def test_take_control_returns_during_inflight_step_and_stops_next_action():
    s = attached()
    s.set_document(source())
    entered, release = asyncio.Event(), asyncio.Event()
    after = AsyncMock()
    async def operation():
        entered.set()
        await release.wait()
    async def inspect(doc, epoch):
        await s.step('Synthetic bounded read', operation, epoch)
        await s.step('Must not run after takeover', after, epoch)
    s.inspect_records = AsyncMock(side_effect=inspect)
    async def run():
        await s.begin(request(s))
        await entered.wait()
        await asyncio.wait_for(s.take_control(), timeout=0.2)
        assert s.owner == 'human'
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
        assert s.job['state'] == 'paused'
        assert not s.resume.is_set()
        after.assert_not_awaited()
        s.task.cancel()
        await asyncio.gather(s.task, return_exceptions=True)
    asyncio.run(run())


def test_return_control_reobserves_and_restarts_from_identity_search():
    s = attached()
    s.set_document(source())
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def inspect(doc, epoch):
        calls.append(epoch)
        if len(calls) == 1:
            async def op():
                entered.set()
                await release.wait()
            await s.step('First read', op, epoch)
        return {'verified_again': True}
    s.inspect_records = inspect
    async def run():
        await s.begin(request(s))
        await entered.wait()
        await s.take_control()
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
        await s.return_control()
        await asyncio.wait_for(s.task, 1)
    asyncio.run(run())
    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert s.job['state'] == 'completed'


@pytest.mark.parametrize('error', [PlaywrightTimeout('private data'), PlaywrightError('private data'), InspectionError('Synthetic layout mismatch')])
def test_failures_preserve_session_and_do_not_leak_playwright_errors(error):
    s = attached()
    s.set_document(source())
    s.inspect_records = AsyncMock(side_effect=error)
    async def run():
        await s.begin(request(s))
        await s.task
    asyncio.run(run())
    assert s.job['state'] == 'failed'
    assert s.active()
    assert 'private data' not in json.dumps(s.result())
    s.browser.close.assert_not_called()


def test_redaction_handles_account_case_changes():
    s = module.LCWSession()
    s.config = {'LCW_USERNAME': 'SyntheticUser', 'LCW_PASSWORD': 'SyntheticSecret'}
    assert s.redact('syntheticuser SYNTHETICSECRET person@example.com') == '[redacted] [redacted] [redacted]'


def test_pdf_source_drives_identifiers_and_alphabetic_search():
    expected = expected_identity(source())
    assert expected['child_id'] == '001234'
    assert search_group(expected['family_name']) == 'E - F'
    assert search_group('Zulu') == 'Y - Z'
    bad = source()
    bad.original.identity.child_id.status = 'needs_review'
    with pytest.raises(InspectionError):
        expected_identity(bad)


def test_search_refuses_missing_ids_duplicate_ids_and_name_conflicts():
    expected = expected_identity(source())
    good = {'href': 'cgchild.do?child_id=001234', 'label': 'Example Child'}
    assert child_links([good, {'href': 'cgchild.do?child_id=999', 'label': 'Example Child'}], expected) == good
    for links in ([], [good, good], [{**good, 'label': 'Different Child'}]):
        with pytest.raises(InspectionError):
            child_links(links, expected)


@pytest.mark.parametrize('text,first,last', [
    ('Family ID 005678\nFamily Center: 0001-Test Center\nChild/Student ID: 999', 'Example', 'Child'),
    ('Family ID 999\nFamily Center: 0001-Test Center\nChild/Student ID: 001234', 'Example', 'Child'),
    ('Family ID 005678\nFamily Center: Wrong\nChild/Student ID: 001234', 'Example', 'Child'),
    ('Family ID 005678\nFamily Center: 0001-Test Center\nChild/Student ID: 001234', 'Other', 'Child'),
])
def test_identity_requires_all_identifiers_and_name(text, first, last):
    with pytest.raises(InspectionError):
        verify_identity(text, first, last, expected_identity(source()))


def test_contract_and_pra_parsing_reads_only_observed_values():
    contracts = parse_contract_rows([row(['Agency', 'From', 'To']), row(['Fictional Training Agency', '09/01/2026', '09/30/2026', 'edit'], 'javascript:editContract(7654321)')])
    assignments = parse_pra_rows([row(['edit', '', '', 'Example Classroom ~ 0001-Test Center', '08/01/2026', '11/30/2026', 'Rate description ($180.00)'])])
    assert contracts[0]['contract_id'] == '7654321'
    assert assignments[0]['start_date'] == '2026-08-01'
    assert 'program' not in assignments[0]
    result = build_result(source(), {k:v for k,v in expected_identity(source()).items() if k != 'family_name'}, contracts, assignments)
    by_key = {r['field']: r for r in result['comparison']}
    assert by_key['identity.child_id']['status'] == 'matching'
    assert by_key['authorization.start_date']['status'] == 'matching'
    assert by_key['full_time.weekly_unit_rate']['status'] == 'not_verified'
    assert by_key['full_time.weekly_copay']['status'] == 'not_verified'
    assert by_key['schedule.program']['status'] == 'not_verified'
    assert by_key['schedule.five_day_tuition']['status'] == 'not_verified'
    assert result['coverage']['status'] == 'matching'
    assert all(r['approved_change'] is None for r in result['comparison'])


def test_comparison_distinguishes_difference_missing_unverified_and_coverage():
    doc = source()
    contracts = [{'contract_id':'9', 'agency':'Other agency', 'start_date':'2026-09-02', 'end_date':'2026-09-30', 'evidence':'Contract list'}]
    assignments = [{'classroom':'Other classroom','start_date':'2026-09-10','end_date':'2026-09-20','evidence':'Assignment list'}]
    result = build_result(doc, {}, contracts, assignments)
    by_key = {r['field']:r for r in result['comparison']}
    assert by_key['identity.agency']['status'] == 'different'
    assert by_key['authorization.start_date']['status'] == 'different'
    assert result['coverage']['status'] == 'different'
    missing = build_result(doc, {}, [], [], True, True)
    assert next(r for r in missing['comparison'] if r['field'] == 'authorization.start_date')['status'] == 'missing'
    unverified = build_result(doc, {}, [], [], False, False)
    assert next(r for r in unverified['comparison'] if r['field'] == 'authorization.start_date')['status'] == 'not_verified'


def test_multiple_records_do_not_silently_select_first():
    contract = {'contract_id':'9','agency':'Fictional Training Agency','start_date':'2026-09-01','end_date':'2026-09-30','evidence':'List'}
    result = build_result(source(), {}, [contract, {**contract,'contract_id':'10'}], [])
    assert result['contract_selection'] == 'ambiguous'
    assert next(r for r in result['comparison'] if r['field'] == 'authorization.start_date')['status'] == 'not_verified'
    result = build_result(source(), {}, [contract, {**contract,'start_date':'2026-01-01'}], [])
    assert result['contract_selection'] == 'exact'

class PortalLocator:
    """Small DOM double: allowlisted actions, real orchestration and real parsers."""
    def __init__(self, page, kind, name=None):
        self.page, self.kind, self.name = page, kind, name
        self.first = self

    def get_by_role(self, role, name=None, exact=False):
        return PortalLocator(self.page, role, name)

    def filter(self, **kwargs):
        return self

    async def is_visible(self):
        return self.page.phase != 'expired'

    async def all_text_contents(self):
        return []

    async def wait_for(self, **kwargs):
        if self.kind == 'text':
            text = self.page.text()
            if hasattr(self.name, 'search'):
                assert re.search(self.name.pattern, text, re.M), (self.name, text)
            else:
                assert self.name in text, (self.name, text)

    async def count(self):
        if self.kind == 'selector' and self.name.startswith('a[href='):
            href = json.loads(self.name[len('a[href='):-1])
            return sum(link['href'] == href for link in self.page.links())
        return 1

    async def click(self, **kwargs):
        p = self.page
        p.actions.append((self.kind, self.name))
        if self.kind == 'link' and self.name == '\uf002':
            p.phase = 'menu'
        elif self.kind == 'button' and self.name == 'E - F':
            assert p.phase == 'menu'
            p.phase = 'search'
            p.url = 'https://portal.example/center/search-results'
        elif self.kind == 'selector':
            href = json.loads(self.name[len('a[href='):-1])
            if href == 'cgchild.do?child_id=001234':
                p.phase = 'child'
                p.url = 'https://portal.example/center/cgchild.do?child_id=001234'
            elif href == "javascript:viewContracts(001234,'Child, Example')":
                assert p.phase == 'third'
                p.phase = 'contracts'
            else:
                raise AssertionError('Attempted a non-allowlisted action')
        elif self.kind == 'tab' and self.name == 'Program/Room Assignment':
            assert p.phase == 'child'
            p.phase = 'pra'
        elif self.kind == 'tab' and self.name == 'Third Party':
            assert p.phase == 'pra'
            p.phase = 'third'
            p.url = 'https://portal.example/center/familyThirdParty.do'
        else:
            raise AssertionError('Attempted a non-allowlisted action')

    async def input_value(self):
        return {'First Name*': 'Example', 'Last Name*': 'Child'}[self.name]

    async def inner_text(self):
        return self.page.text()

    async def evaluate_all(self, script):
        if self.name == 'a[href]':
            return self.page.links()
        assert self.name == 'table tr'
        if self.page.phase == 'pra':
            return [row(['edit', '', '', 'Example Classroom ~ 0001-Test Center', '08/01/2026', '11/30/2026', 'Example rate ($180.00)'])]
        if self.page.phase == 'contracts':
            return [row(['Fictional Training Agency', '09/01/2026', '09/30/2026', 'edit'], 'javascript:editContract(7654321)')]
        raise AssertionError('Read rows from unexpected screen')


class PortalPage:
    def __init__(self, mismatch=False):
        self.phase = 'home'
        self.url = 'https://portal.example/center/daycare.do'
        self.actions = []
        self.mismatch = mismatch

    def is_closed(self):
        return False

    def locator(self, selector):
        return PortalLocator(self, 'selector', selector)

    def get_by_role(self, role, name=None, exact=False):
        return PortalLocator(self, role, name)

    def get_by_label(self, name, exact=False):
        return PortalLocator(self, 'label', name)

    def get_by_text(self, text, exact=False):
        return PortalLocator(self, 'text', text)

    def text(self):
        family = 'wrong' if self.mismatch else '005678'
        text = f'Family ID {family}\nFamily Center: 0001-Test Center\n'
        if self.phase == 'search':
            return 'Family Search Results (E - F)\n'
        if self.phase == 'child':
            return text + 'Child/Student ID: 001234\n'
        if self.phase == 'pra':
            return text + 'Active Classroom Assignments - Child, Example\n'
        if self.phase == 'third':
            return text + 'Third Party Contract List\n'
        if self.phase == 'contracts':
            return text + 'Third Party Contract List (Child, Example)\n'
        return text

    def links(self):
        if self.phase == 'search':
            return [{'href':'cgchild.do?child_id=001234', 'label':'Example Child'},
                    {'href':'cgchild.do?child_id=999', 'label':'Example Child'}]
        if self.phase == 'third':
            return [{'href':"javascript:viewContracts(001234,'Child, Example')", 'label':'edit icon'},
                    {'href':'javascript:addContract(001234)', 'label':'add icon'}]
        return []


def test_offline_pdf_to_inspect_to_comparison_uses_real_orchestration_and_no_write_actions():
    s = attached()
    s.page = PortalPage()
    s.observe = module.LCWSession.observe.__get__(s)
    s.set_document(source())
    async def run():
        await s.begin(request(s))
        await asyncio.wait_for(s.task, 2)
    asyncio.run(run())
    assert s.job['state'] == 'completed', s.job['stage']
    result = s.job['result']
    assert result['identity']['child_id'] == '001234'
    assert result['contracts'][0]['contract_id'] == '7654321'
    assert result['assignments'][0]['end_date'] == '2026-11-30'
    assert result['coverage']['status'] == 'matching'
    assert len(s.page.actions) == 6
    assert all(not re.search(r'addContract|editContract|copyContract|Save|Sign in', str(action)) for action in s.page.actions)
    assert len(s.job['events']) >= 10


def test_identity_mismatch_stops_orchestration_before_contract_or_pra_navigation():
    s = attached()
    s.page = PortalPage(mismatch=True)
    s.observe = module.LCWSession.observe.__get__(s)
    s.set_document(source())
    async def run():
        await s.begin(request(s))
        await s.task
    asyncio.run(run())
    assert s.job['state'] == 'failed'
    assert s.job['result'] is None
    assert not any(action[0] == 'tab' for action in s.page.actions)


def test_frontend_refresh_can_restore_source_and_job_without_new_browser_commands(monkeypatch):
    s = attached()
    s.set_document(source())
    s.inspect_records = AsyncMock(return_value={'synthetic': True})
    async def run():
        await s.begin(request(s))
        await s.task
    asyncio.run(run())
    monkeypatch.setattr(module, 'session', s)
    client = TestClient(app)
    for _ in range(3):
        doc = client.get('/api/browser/document', headers=HEADERS)
        job = client.get('/api/browser/inspection', headers=HEADERS)
        assert doc.json()['extraction_id'] == s.document_id
        assert job.json()['result'] == {'synthetic': True}
        assert job.headers['cache-control'] == 'no-store'
    s.page.goto.assert_not_called()
    s.context.new_page.assert_not_called()


def test_failed_cdp_attachment_retains_process_but_releases_backend_ownership(monkeypatch, tmp_path):
    monkeypatch.setattr(module, 'PROFILE_PATH', tmp_path / 'profile')
    monkeypatch.setattr(module, 'dotenv_values', lambda *a, **k: {'LCW_URL': 'https://portal.example/'})
    s = module.LCWSession()
    s.owned_process_alive = MagicMock(return_value=True)
    s.metadata = MagicMock(return_value={'pid': 999, 'target':'old-target'})
    engine = MagicMock(stop=AsyncMock())
    monkeypatch.setattr(module, 'async_playwright', lambda: MagicMock(start=AsyncMock(return_value=engine)))
    s.attach = AsyncMock(side_effect=PlaywrightError('confidential endpoint'))
    with pytest.raises(HTTPException) as error:
        asyncio.run(s.start(existing_only=True))
    assert error.value.status_code == 503
    assert 'confidential' not in error.value.detail
    assert s.owner_file is None
    assert s.browser is None
    engine.stop.assert_awaited_once()


def test_mfa_detection_does_not_report_authentication_or_call_login():
    s = attached()
    s.observe = module.LCWSession.observe.__get__(s)
    s.page.url = module.AUTH_ORIGIN + '/challenge'
    s.page.get_by_role.return_value.is_visible = AsyncMock(return_value=True)
    s.login = AsyncMock()
    result = asyncio.run(s.status())
    assert result['state'] == 'awaiting_mfa'
    assert result['owner'] == 'human'
    assert not result['authenticated']
    s.login.assert_not_awaited()


def test_preview_uses_same_page_deduplicates_and_discards_navigation_frames():
    s = attached()
    s.page.screenshot = AsyncMock(return_value=b'synthetic jpeg')
    async def run():
        first = await s.frame()
        second = await s.frame()
        assert first == second
        s.last_frame_at = 0
        async def capture(**kwargs):
            s.page.url = module.AUTH_ORIGIN + '/challenge'
            return b'frame to discard'
        s.page.screenshot.side_effect = capture
        with pytest.raises(HTTPException):
            await s.frame()
    asyncio.run(run())
    assert s.page.screenshot.await_count == 2
    assert s.last_frame is None


def test_concurrent_cold_starts_launch_one_owned_process_with_fixed_profile(monkeypatch, tmp_path):
    path = tmp_path / 'profile'
    monkeypatch.setattr(module, 'PROFILE_PATH', path)
    monkeypatch.setattr(module, 'dotenv_values', lambda *a, **k: {'LCW_URL': 'https://portal.example/'})
    engine = MagicMock(stop=AsyncMock())
    engine.chromium.executable_path = '/synthetic/chromium'
    monkeypatch.setattr(module, 'async_playwright', lambda: MagicMock(start=AsyncMock(return_value=engine)))
    process = MagicMock(pid=123, poll=MagicMock(return_value=None))
    def launch(*args, **kwargs):
        (path / 'DevToolsActivePort').write_text('9999\n/devtools/browser/synthetic-id\n')
        return process
    popen = MagicMock(side_effect=launch)
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    s = module.LCWSession()
    s.owned_process_alive = MagicMock(return_value=False)
    async def attach(meta):
        assert meta['pid'] == 123
        s.browser, s.context, s.page = MagicMock(), MagicMock(), MagicMock()
        s.page.is_closed.return_value = False
        s.page.goto = AsyncMock(return_value=MagicMock(status=200))
    s.attach = AsyncMock(side_effect=attach)
    s.observe = AsyncMock(return_value={'state':'awaiting_authentication'})
    async def run():
        await asyncio.gather(*(s.start() for _ in range(6)))
        assert popen.call_count == 1
        s.page.goto.assert_awaited_once()
        s.context.new_context.assert_not_called()
        await s.shutdown()
    asyncio.run(run())
    args, kwargs = popen.call_args
    assert f'--user-data-dir={path}' in args[0]
    assert '--remote-debugging-address=127.0.0.1' in args[0]
    assert '--remote-debugging-port=0' in args[0]
    assert kwargs['start_new_session'] is True
    assert path.is_dir()


def test_cdp_reconnect_keeps_exact_target_among_multiple_tabs(monkeypatch, tmp_path):
    monkeypatch.setattr(module, 'PROFILE_PATH', tmp_path)
    (tmp_path / 'DevToolsActivePort').write_text('9999\n/devtools/browser/synthetic-id\n')
    pages = [MagicMock(), MagicMock()]
    for page in pages:
        page.is_closed.return_value = False
    sessions = []
    for target in ('unexpected-tab', 'controlled-tab'):
        sessions.append(MagicMock(send=AsyncMock(return_value={'targetInfo':{'targetId':target}}), detach=AsyncMock()))
    context = MagicMock(pages=pages, new_cdp_session=AsyncMock(side_effect=sessions))
    browser = MagicMock(contexts=[context])
    s = module.LCWSession()
    s.playwright = MagicMock()
    s.playwright.chromium.connect_over_cdp = AsyncMock(return_value=browser)
    asyncio.run(s.attach({'pid':123, 'target':'controlled-tab'}))
    assert s.page is pages[1]
    context.new_page.assert_not_called()
    for page in pages:
        page.goto.assert_not_called()


def test_expired_session_pauses_job_and_retains_source_without_login():
    s = attached()
    s.set_document(source())
    s.login = AsyncMock()
    entered = asyncio.Event()
    async def inspect(doc, epoch):
        s.portal_authenticated = False
        s.state = 'awaiting_authentication'
        s.owner = 'human'
        entered.set()
        raise module.AuthenticationRequired()
    s.inspect_records = inspect
    async def run():
        await s.begin(request(s))
        await entered.wait()
        for _ in range(10):
            await asyncio.sleep(0)
        assert s.job['state'] == 'awaiting_authentication'
        assert not s.resume.is_set()
        assert s.job['source'].original.identity.child_id.value == '001234'
        assert not s.task.done()
        await s.cancel_inspection()
    asyncio.run(run())
    s.login.assert_not_awaited()
    assert s.job['state'] == 'cancelled'
    assert s.page is not None


def test_inspection_result_does_not_mutate_extracted_source():
    doc = source()
    before = doc.model_dump(mode='json')
    build_result(doc, {}, [], [])
    assert doc.model_dump(mode='json') == before


def test_backend_refresh_reads_inflight_job_without_cancelling(monkeypatch):
    import httpx
    s = attached()
    s.set_document(source())
    entered, release = asyncio.Event(), asyncio.Event()
    async def inspect(doc, epoch):
        entered.set()
        await release.wait()
        return {'synthetic':True}
    s.inspect_records = inspect
    monkeypatch.setattr(module, 'session', s)
    async def run():
        await s.begin(request(s))
        await entered.wait()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver', headers=HEADERS) as client:
            for _ in range(3):
                status = await client.get('/api/browser/status')
                detail = await client.get('/api/browser/inspection')
                assert status.json()['inspection']['id'] == s.job['id']
                assert detail.json()['result'] is None
                assert not s.task.done()
        release.set()
        await s.task
    asyncio.run(run())
    assert s.job['state'] == 'completed'
    s.context.new_page.assert_not_called()


def test_takeover_during_authentication_check_prevents_action_dispatch():
    s = attached()
    s.job = {'state':'inspecting','stage':'requested','version':0,'events':[]}
    entered, release = asyncio.Event(), asyncio.Event()
    async def observe():
        entered.set()
        await release.wait()
        return s.result()
    s.observe = observe
    action = AsyncMock()
    async def run():
        operation = asyncio.create_task(s.step('Must not dispatch', action, s.epoch))
        await entered.wait()
        await s.take_control()
        release.set()
        with pytest.raises(module.Paused):
            await operation
    asyncio.run(run())
    action.assert_not_awaited()


def test_takeover_wins_over_overlapping_return_control():
    s = attached()
    s.owner = 'human'
    entered, release = asyncio.Event(), asyncio.Event()
    async def observe():
        entered.set()
        await release.wait()
    s.observe = observe
    async def run():
        returning = asyncio.create_task(s.return_control())
        await entered.wait()
        await s.take_control()
        release.set()
        await returning
    asyncio.run(run())
    assert s.owner == 'human'
    assert not s.resume.is_set()


def test_portal_expiry_notice_overrides_stale_authenticated_header():
    s = attached()
    s.observe = module.LCWSession.observe.__get__(s)
    s.page.locator.return_value.all_text_contents = AsyncMock(return_value=['Your session has expired.'])
    s.page.locator.return_value.is_visible = AsyncMock(return_value=True)
    s.page.get_by_role.return_value.get_by_role.return_value.is_visible = AsyncMock(return_value=True)
    result = asyncio.run(s.status())
    assert result['state'] == 'awaiting_authentication'
    assert result['owner'] == 'human'
    assert not result['authenticated']


def test_lost_transport_cannot_retain_profile_ownership(monkeypatch, tmp_path):
    monkeypatch.setattr(module, 'PROFILE_PATH', tmp_path / 'profile')
    s = attached()
    s.claim_profile()
    browser = s.browser
    s.playwright = MagicMock(stop=AsyncMock(side_effect=PlaywrightError('lost transport')))
    asyncio.run(s.shutdown())
    assert s.owner_file is None
    assert s.browser is None
    browser.close.assert_not_called()
    another = module.LCWSession()
    another.claim_profile()
    another.release_profile()
