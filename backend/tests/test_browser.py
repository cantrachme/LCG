"""Offline session/control tests. These never contact LCW or trigger MFA."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app import browser as module
from app.main import app


def test_browser_api_rejects_untrusted_requests_and_record_writes():
    client = TestClient(app)
    assert client.get("/api/browser/status").status_code == 403
    assert client.post("/api/browser/start", headers={"X-LCG-Local": "1", "Origin": "https://untrusted.example"}).status_code == 403
    assert client.get("/api/browser/status", headers={"X-LCG-Local": "1", "Host": "untrusted.example"}).status_code == 403
    assert client.post("/api/browser/save", headers={"X-LCG-Local": "1"}).status_code == 404
    response = client.get("/api/browser/status", headers={"X-LCG-Local": "1"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_reuses_active_page_without_launching_or_navigating():
    session = module.LCWSession()
    session.context = MagicMock()
    session.browser = MagicMock()
    session.browser.is_connected.return_value = True
    session.page = MagicMock()
    session.page.is_closed.return_value = False
    session.observe = AsyncMock(return_value={"state": "authenticated"})
    session.close = AsyncMock()
    assert asyncio.run(session.start()) == {"state": "authenticated"}
    session.close.assert_not_awaited()
    session.page.goto.assert_not_called()
    session.context.new_page.assert_not_called()


def test_persistent_profile_path_survives_close_and_restart(monkeypatch, tmp_path):
    profile = tmp_path / "browser-profile"
    monkeypatch.setattr(module, "PROFILE_PATH", profile)
    monkeypatch.setattr(module, "dotenv_values", lambda *a, **k: {"LCW_URL": "https://portal.example/"})
    page = MagicMock()
    page.goto = AsyncMock(return_value=MagicMock(status=200))
    page.locator.return_value.first.wait_for = AsyncMock()
    page.is_closed.return_value = False
    page.url = "https://portal.example/"
    context = MagicMock(pages=[page], close=AsyncMock())
    context.browser.is_connected.return_value = True
    engine = MagicMock(stop=AsyncMock())
    engine.chromium.launch_persistent_context = AsyncMock(return_value=context)
    manager = MagicMock(start=AsyncMock(return_value=engine))
    monkeypatch.setattr(module, "async_playwright", lambda: manager)
    session = module.LCWSession()
    session.observe = AsyncMock(return_value={"state": "authenticated"})

    async def run():
        await session.start()
        await session.close()
        assert profile.is_dir()
        await session.start()
        await session.close()

    asyncio.run(run())
    calls = engine.chromium.launch_persistent_context.call_args_list
    assert len(calls) == 2
    assert all(call.kwargs["user_data_dir"] == str(profile) and call.kwargs["headless"] is False for call in calls)
    assert profile.stat().st_mode & 0o777 == 0o700
    engine.chromium.launch.assert_not_called()
    context.new_page.assert_not_called()


def test_manual_control_blocks_sign_in_and_mfa_blocks_preview():
    session = module.LCWSession()
    session.owner, session.state = "human", "waiting_for_login"
    session.observe = AsyncMock()
    session.page = MagicMock()
    with pytest.raises(HTTPException) as blocked:
        asyncio.run(session.login())
    assert blocked.value.status_code == 409
    session.page.locator.assert_not_called()
    session.state = "waiting_for_mfa"
    with pytest.raises(HTTPException):
        asyncio.run(session.frame())
    session.page.screenshot.assert_not_called()


def test_password_is_not_retried():
    session = module.LCWSession()
    session.state, session.owner = "waiting_for_login", "agent"
    session.config = {"LCW_USERNAME": "synthetic-user", "LCW_PASSWORD": "synthetic-secret"}
    session.password_attempted = True
    session.observe = AsyncMock()
    session.page = MagicMock()
    email = MagicMock(is_visible=AsyncMock(return_value=False))
    password = MagicMock(is_visible=AsyncMock(return_value=True), fill=AsyncMock())
    session.page.locator.side_effect = lambda selector: email if selector == "#i0116" else password
    with pytest.raises(HTTPException):
        asyncio.run(session.login())
    password.fill.assert_not_awaited()


def test_timeout_is_sanitized_and_pauses_automation(monkeypatch):
    session = module.LCWSession()
    session.login = AsyncMock(side_effect=PlaywrightTimeout("sensitive page content"))
    monkeypatch.setattr(module, "session", session)
    with pytest.raises(HTTPException) as failed:
        asyncio.run(module.perform("login"))
    assert failed.value.status_code == 504
    assert "sensitive" not in failed.value.detail
    assert session.owner == "human"


def test_redaction_handles_account_case_changes():
    session = module.LCWSession()
    session.config = {"LCW_USERNAME": "SyntheticUser", "LCW_PASSWORD": "SyntheticSecret"}
    assert session.redact("syntheticuser SYNTHETICSECRET person@example.com") == "[redacted] [redacted] [redacted]"
