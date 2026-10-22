"""One local headed Chromium profile. No LCW record-write tools."""

import asyncio
import re
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout, async_playwright

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
PROFILE_PATH = ENV_PATH.parent / ".lcw-browser-profile"
AUTH_ORIGIN = "https://login.microsoftonline.com"
LOCAL_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173"}


def origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def local_request(request: Request):
    # CORS alone does not stop a hostile website from sending requests to localhost.
    if (request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}
            or request.url.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}
            or request.headers.get("x-lcg-local") != "1"
            or (request.headers.get("origin") is not None and request.headers["origin"] not in LOCAL_ORIGINS)):
        raise HTTPException(403, "Browser access is limited to the local Command Center.")


class LCWSession:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.playwright = self.browser = self.context = self.page = None
        self.config = {}
        self.owner = "agent"
        self.state = "closed"
        self.message = "Open LCW to begin read-only inspection."
        self.password_attempted = False
        self.navigation_failed = False

    def redact(self, text: str) -> str:
        for key in ("LCW_USERNAME", "LCW_PASSWORD"):
            if self.config.get(key):
                text = re.sub(re.escape(self.config[key]), "[redacted]", text, flags=re.I)
        return re.sub(r"[\w.+-]+@[\w.-]+", "[redacted]", text)

    def result(self):
        active = bool(self.page and not self.page.is_closed())
        # Never return query strings, fragments, cookies, raw DOM or exception strings.
        return {"state": self.state, "message": self.message, "owner": self.owner,
                "active": active, "read_only": True, "authenticated": self.state == "authenticated",
                "location": origin(self.page.url) if active else None,
                "preview_available": active and self.state in {"portal_unverified", "authenticated"}}

    async def observe(self):
        if not self.page or self.page.is_closed() or not self.browser.is_connected():
            self.state, self.message = "closed", "No active Chromium page. Open LCW to start a new session."
        elif self.navigation_failed:
            self.state, self.message = "navigation_error", "LCW navigation failed or timed out. Check access, then close and reopen the session."
        elif origin(self.page.url) == AUTH_ORIGIN:
            self.state, self.message = "waiting_for_login", "Sign-in required. Use configured sign-in or the local Chromium window."
            if await self.page.get_by_role("heading", name="Approve sign in request", exact=True).is_visible():
                self.state, self.message = "waiting_for_mfa", "Complete MFA in the local Chromium window, then return control."
                self.owner = "human"
            elif not await self.page.locator("#i0116").is_visible() and not await self.page.locator("#i0118").is_visible():
                self.state, self.message = "waiting_for_user", "Complete the sign-in prompt in local Chromium. No challenge is automated."
                self.owner = "human"
        elif origin(self.page.url) == origin(self.config.get("LCW_URL", "")):
            self.state, self.message = "portal_unverified", "LCW page reached. Authentication and child/family records still require inspection."
            if (await self.page.locator("#userProfileMenu").is_visible()
                    and await self.page.get_by_role("navigation").get_by_role("link", name="Family", exact=True).is_visible()):
                self.state, self.message = "authenticated", "Authenticated LCW portal. Child/family verification remains a separate read-only step."
        else:
            self.state, self.message = "unexpected_page", "Unrecognized page. Inspect the local window; automation is paused."
            self.owner = "human"
        return self.result()

    async def start(self):
        if self.context and self.browser and self.browser.is_connected():
            # Reuse the same context even if the operator closed its selected tab.
            if not self.page or self.page.is_closed():
                self.page = next((page for page in self.context.pages
                                  if not page.is_closed() and origin(page.url) in
                                  {origin(self.config.get("LCW_URL", "")), AUTH_ORIGIN}), None)
                if self.page is None:
                    self.page = await self.context.new_page()
                    await self.page.goto(self.config["LCW_URL"], wait_until="domcontentloaded", timeout=30000)
            return await self.observe()
        await self.close()
        self.config = dotenv_values(ENV_PATH, interpolate=False)
        target = self.config.get("LCW_URL", "")
        parsed = urlsplit(target)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HTTPException(503, "backend/.env must provide an HTTPS LCW_URL without credentials, query or fragment.")
        self.owner, self.password_attempted, self.navigation_failed = "agent", False, False
        try:
            # Dedicated to LCG; never use the user's everyday Chrome profile.
            # Chromium's profile lock prevents a second process opening this directory.
            PROFILE_PATH.mkdir(mode=0o700, parents=True, exist_ok=True)
            PROFILE_PATH.chmod(0o700)
            self.playwright = await async_playwright().start()
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_PATH), headless=False,
                viewport={"width": 1360, "height": 900}, accept_downloads=False,
            )
            self.browser = self.context.browser
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            self.page.set_default_timeout(8000)
            # No arbitrary URL/click/input endpoint. Only observed sign-in actions are implemented.
            self.page.on("dialog", lambda dialog: dialog.dismiss())
            response = await self.page.goto(target, wait_until="domcontentloaded", timeout=30000)
            if response and response.status >= 400:
                self.navigation_failed = True
            if not self.navigation_failed:
                try:
                    await self.page.locator("#i0116:visible, #i0118:visible, #userProfileMenu:visible, [role=heading]:visible").first.wait_for(timeout=8000)
                except PlaywrightTimeout:
                    pass  # Unknown sign-in screens need the operator, never guessed clicks.
            return await self.observe()
        except PlaywrightTimeout:
            self.navigation_failed = True
            return await self.observe()
        except (PlaywrightError, OSError):
            await self.close()
            raise HTTPException(503, "Could not open the dedicated LCW profile. Check browser installation, local permissions, or whether another LCG backend owns it. No replacement profile was created.") from None

    async def login(self):
        await self.observe()
        if self.owner != "agent" or self.state != "waiting_for_login":
            raise HTTPException(409, "Sign-in automation is paused. Complete the prompt locally, then return control.")
        if not self.config.get("LCW_USERNAME") or not self.config.get("LCW_PASSWORD"):
            raise HTTPException(503, "LCW credentials are missing from backend/.env. Use the local window to sign in.")
        # These IDs and button names were observed in the real Microsoft sign-in.
        if await self.page.locator("#i0116").is_visible():
            await self.page.locator("#i0116").fill(self.config["LCW_USERNAME"])
            await self.page.get_by_role("button", name="Next", exact=True).click()
            try:
                await self.page.locator("#i0118").wait_for(state="visible", timeout=10000)
            except PlaywrightTimeout:
                self.owner = "human"
                return await self.observe()
        if await self.page.locator("#i0118").is_visible():
            if self.password_attempted:
                raise HTTPException(409, "A password was already submitted. Resolve sign-in in local Chromium; automatic retries are disabled.")
            self.password_attempted = True
            await self.page.locator("#i0118").fill(self.config["LCW_PASSWORD"])
            await self.page.get_by_role("button", name="Sign in", exact=True).click()
            try:
                await self.page.locator("#i0118").wait_for(state="hidden", timeout=10000)
            except PlaywrightTimeout:
                pass
        # Human owns the next step, even if a challenge is not recognizable yet.
        self.owner = "human"
        return await self.observe()

    async def frame(self):
        await self.observe()
        if self.state not in {"portal_unverified", "authenticated"}:
            raise HTTPException(409, "Preview is paused during sign-in or an unrecognized page. Use local Chromium.")
        return await self.page.screenshot(type="jpeg", quality=65, timeout=5000,
                                          mask=[self.page.locator('input[type="password"]')])

    async def navigation(self):
        await self.observe()
        if self.state != "authenticated":
            raise HTTPException(409, "Reach LCW in the local window before inspecting navigation.")
        # Read visible link labels only. Do not click links or persist page content.
        items = await self.page.locator("a").evaluate_all("""elements => elements
            .filter(e => e.getClientRects().length && e.innerText.trim())
            .map(e => ({label: e.innerText.trim().slice(0, 100)})).slice(0, 100)""")
        return {"verified": False, "links": [{"label": self.redact(item["label"])} for item in items],
                "message": "Observed link labels only; routes and record matching are not yet verified."}

    async def close(self):
        try:
            if self.context:
                await self.context.close()
        except PlaywrightError:
            pass
        finally:
            if self.playwright:
                await self.playwright.stop()
            self.playwright = self.browser = self.context = self.page = None
            self.config = {}
            self.state, self.message = "closed", "Browser closed. The dedicated local profile is retained; Microsoft controls when sign-in is required again."
        return self.result()


session = LCWSession()
router = APIRouter(prefix="/api/browser", dependencies=[Depends(local_request)])


@router.get("/status")
async def status(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await perform("status")


@router.get("/frame")
async def frame():
    return Response(await perform("frame"), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/navigation")
async def navigation(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await perform("navigation")


@router.post("/{action}")
async def action(action: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    if action not in {"start", "login", "take-control", "return-control", "close"}:
        raise HTTPException(404, "Unsupported browser action. LCW record changes are not implemented.")
    return await perform(action)


async def perform(action: str):
    async with session.lock:
        try:
            if action == "start":
                return await session.start()
            if action == "close":
                return await session.close()
            if action == "login":
                return await session.login()
            if action == "frame":
                return await session.frame()
            if action == "navigation":
                return await session.navigation()
            if action == "take-control":
                session.owner = "human"
                if session.page and not session.page.is_closed():
                    await session.page.bring_to_front()
            elif action == "return-control":
                session.owner = "agent"
            return await session.observe()
        except PlaywrightTimeout:
            session.owner = "human"
            raise HTTPException(504, "The page did not respond in time. Inspect local Chromium before retrying.") from None
        except PlaywrightError:
            session.owner = "human"
            raise HTTPException(409, "The browser page changed or closed. Inspect the local window or reopen LCW.") from None
