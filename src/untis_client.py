"""WebUntis client: authenticates against the JSON-RPC API and uses
HTTP for all subsequent requests.

Two login paths are supported:

1. **JSON-RPC authenticate** (default, recommended)
   The endpoint `POST /WebUntis/jsonrpc.do?school=<slug>` accepts a
   JSON-RPC `authenticate` call with `{user, password, client}` and
   returns a session id + personId + personType. No DOM scraping, no
   rendering, no race with the React UI2020 SPA.

   We still need a `school` cookie (some schools set one on first
   visit) — Playwright is used briefly to obtain it, then closed.

2. **Form-based login** (fallback, for schools with custom SSO / 2FA)
   Drives the actual HTML form. Only kicks in if path 1 returns a
   known unrecoverable error or when the user passes `--form-login`.

The result of either path is saved as a Playwright `storage_state`
so the next run can skip the browser entirely.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import date, datetime
from typing import Any, Optional

import httpx
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PWTimeout

from .browser import BrowserSession
from .config import ScraperConfig

log = logging.getLogger(__name__)

JSONRPC_PATH = "/WebUntis/jsonrpc.do"
REST_BASE = "/WebUntis/api/rest/view/v1"
CLIENT_ID = "webuntis-scraper/1.0 (playwright)"


class WebUntisError(RuntimeError):
    """Raised when WebUntis returns an error or auth fails."""


# Map of known JSON-RPC authenticate error codes -> human readable.
# Sources: Untis mobile app reverse-engineering + community research.
# (The Untis API does not publish an official error code table.)
AUTH_ERRORS = {
    -1:  "Invalid username or password",
    -2:  "Account is locked / too many attempts",
    -3:  "Login not yet started or already ended",
    -4:  "Invalid school",
    -5:  "Invalid client",
    -6:  "Wrong user agent",
    -7:  "OTP required (2FA) — complete login in the browser",
    -8:  "Captcha required — complete login in the browser",
    -9:  "No OTP secret set on account",
    -10: "School not active / not allowed",
    -50: "Server temporarily unavailable",
    -100: "Network error",
    -200: "Session expired",
    -1010: "Login not possible (maintenance)",
    -8504: "Bad credentials (empty password? wrong username format?)",
}


def _to_iso_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _weeks(start: date, end: date) -> list[tuple[date, date]]:
    """Yield (Mon, Sun) pairs covering [start, end]."""
    if end < start:
        start, end = end, start
    cur = start
    out = []
    while cur <= end:
        monday = cur.fromordinal(cur.toordinal() - cur.weekday())
        sunday = monday.fromordinal(monday.toordinal() + 6)
        out.append((max(monday, start), min(sunday, end)))
        cur = monday.fromordinal(monday.toordinal() + 7)
    return out


class WebUntisClient:
    """Drives a real browser to bootstrap cookies, then HTTP for everything."""

    def __init__(self, cfg: ScraperConfig, session: BrowserSession):
        self.cfg = cfg
        self.session = session
        self._page: Optional[Page] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._person_id: Optional[int] = None
        self._person_type: Optional[int] = None
        self._user_display: Optional[str] = None
        self._logged_in = False
        self._rpc_id = 0
        self._last_request_ts = 0.0
        self._min_interval = 0.4

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------
    async def login(self, force: bool = False) -> None:
        if self._logged_in and not force:
            return
        assert self.session.context is not None

        self._page = await self.session.new_page()
        self._client = httpx.AsyncClient(
            timeout=self.cfg.timeout_ms / 1000,
            headers={
                "User-Agent": self.cfg.user_agent,
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
            },
            follow_redirects=True,
        )

        # 1. Hit the login page so the server can set school-bound cookies
        #    (JSESSIONID, school-info, etc.) that JSON-RPC needs.
        log.info("Bootstrapping cookies via %s", self.cfg.login_url)
        try:
            await self._page.goto(self.cfg.login_url, wait_until="domcontentloaded")
        except Exception as exc:
            log.warning("Initial page load failed: %s", exc)
        await asyncio.sleep(0.5)

        # Mirror those cookies into our httpx client.
        await self._sync_cookies_to_httpx()

        # 2. Try an existing session first.
        if not force and await self._probe_session():
            log.info("Reusing existing WebUntis session")
            self._logged_in = True
            return

        # 3. Try JSON-RPC authenticate.
        if not self.cfg.force_form_login:
            try:
                await self._login_via_jsonrpc()
                if await self._probe_session():
                    log.info("Login successful via JSON-RPC")
                    self._logged_in = True
                    await self._sync_cookies_to_playwright()
                    return
            except WebUntisError as exc:
                # Only fall back to form for recoverable errors (2FA, captcha, SSO).
                # For "bad credentials" the form path will fail the same way.
                if self._is_recoverable(exc):
                    log.info(
                        "JSON-RPC login needs interactive step (%s); "
                        "falling back to form", exc,
                    )
                else:
                    log.error("JSON-RPC login failed: %s", exc)
                    raise

        # 4. Form-based login fallback (handles 2FA, captcha, custom SSO).
        await self._login_via_form()
        if not await self._probe_session():
            raise WebUntisError(
                "Login did not produce a valid session. Check credentials."
            )
        self._logged_in = True
        log.info("Login successful via form")

    async def _login_via_jsonrpc(self) -> None:
        log.info("Authenticating via JSON-RPC as %r", self.cfg.username)
        res = await self._rpc_raw("authenticate", {
            "user": self.cfg.username,
            "password": self.cfg.password,
            "client": CLIENT_ID,
        })
        # Result looks like:
        #   { "sessionId": "...", "personType": 5, "personId": 12345,
        #     "givenName": "...", "familyName": "..." }
        if not res or "sessionId" not in res:
            code = (res or {}).get("code")
            msg = AUTH_ERRORS.get(code, f"unknown error code {code}")
            raise WebUntisError(f"Authenticate failed: {msg} (code={code})")
        self._person_id = res.get("personId")
        self._person_type = res.get("personType")
        self._user_display = (
            f"{res.get('givenName','')} {res.get('familyName','')}".strip()
        )
        log.info(
            "Authenticated: personId=%s personType=%s",
            self._person_id, self._person_type,
        )

    async def _login_via_form(self) -> None:
        assert self._page is not None
        log.info("Falling back to form-based login")
        page = self._page

        # WebUntis UI2020 is a React SPA. The form is rendered after JS
        # hydration. We try multiple selectors and pick the first that
        # actually resolves.
        user_selectors = [
            'input[name="j_username"]',
            'input[name="username"]',
            'input[name="user"]',
            'input[autocomplete="username"]',
            'input[type="text"]',
        ]
        pw_selectors = [
            'input[name="j_password"]',
            'input[name="password"]',
            'input[type="password"]',
        ]
        submit_selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Anmelden")',
            'button:has-text("Login")',
            'button:has-text("Log in")',
            'button:has-text("Sign in")',
        ]

        # Wait for the React form to appear.
        user_locator = None
        for sel in user_selectors:
            loc = page.locator(sel).first
            try:
                await loc.wait_for(state="visible", timeout=10_000)
                user_locator = loc
                log.debug("Using username selector %s", sel)
                break
            except PWTimeout:
                continue
        if user_locator is None:
            await self._screenshot_for_debug("login_no_form")
            raise WebUntisError(
                "Could not find login form. Run with --no-headless to debug."
            )

        await user_locator.fill(self.cfg.username)
        await asyncio.sleep(0.1)

        pw_locator = None
        for sel in pw_selectors:
            loc = page.locator(sel).first
            try:
                await loc.wait_for(state="visible", timeout=2_000)
                pw_locator = loc
                break
            except PWTimeout:
                continue
        if pw_locator is None:
            raise WebUntisError("Password field not found")
        await pw_locator.fill(self.cfg.password)
        await asyncio.sleep(0.1)

        clicked = False
        for sel in submit_selectors:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            try:
                await loc.click()
                clicked = True
                log.debug("Clicked submit using %s", sel)
                break
            except Exception:
                continue
        if not clicked:
            # Press Enter as a last resort.
            await page.keyboard.press("Enter")

        # Wait for either a successful redirect or a 2FA prompt.
        try:
            await page.wait_for_url(
                lambda url: "/login" not in url and "WebUntis" in url,
                timeout=self.cfg.timeout_ms,
            )
        except PWTimeout:
            if await self._has_2fa_field():
                raise WebUntisError(
                    "2FA required. Run with --no-headless and complete it once; "
                    "the session will be saved for next time."
                )
            await self._screenshot_for_debug("login_failed")
            raise WebUntisError(
                "Form login did not redirect away from /login. "
                "See logs/login_failed.png for details."
            )

        await self._sync_cookies_to_httpx()

    async def _has_2fa_field(self) -> bool:
        assert self._page is not None
        return await self._page.locator(
            'input[name="otp"], input[name="code"], input[name="token"], '
            'input[autocomplete="one-time-code"]'
        ).count() > 0

    async def _screenshot_for_debug(self, name: str) -> None:
        if not self._page:
            return
        try:
            await self._page.screenshot(path=f"logs/{name}.png", full_page=True)
            log.info("Saved debug screenshot to logs/%s.png", name)
        except Exception:
            pass

    @staticmethod
    def _is_recoverable(exc: WebUntisError) -> bool:
        msg = str(exc).lower()
        return any(tok in msg for tok in ("2fa", "captcha", "otp", "sso"))

    # ------------------------------------------------------------------
    # Cookie / session management
    # ------------------------------------------------------------------
    async def _sync_cookies_to_httpx(self) -> None:
        """Copy cookies from the Playwright context into the httpx client."""
        if not self._client or not self.session.context:
            return
        jar = httpx.Cookies()
        for c in await self.session.context.cookies():
            jar.set(
                c["name"], c["value"],
                domain=c.get("domain"),
                path=c.get("path", "/"),
            )
        self._client.cookies = jar
        log.debug("Synced %d cookies to httpx", len(jar))

    async def _sync_cookies_to_playwright(self) -> None:
        """Copy cookies from the httpx client into the Playwright context."""
        if not self._client or not self.session.context:
            return
        cookies: list[dict] = []
        for name, value in self._client.cookies.items():
            host = (self._client.cookies.get(name) or value)
            # httpx stores domain; for a single-school session this is fine
            cookies.append({
                "name": name,
                "value": value,
                "url": self.cfg.base_url,
            })
        if cookies:
            try:
                await self.session.context.add_cookies(cookies)
                log.debug("Mirrored %d cookies into browser context", len(cookies))
            except Exception as exc:
                log.debug("Could not mirror cookies to browser: %s", exc)

    async def _probe_session(self) -> bool:
        try:
            res = await self._rpc("getUserData", {})
            if res and res.get("result"):
                self._person_id = res["result"].get("personId")
                self._person_type = res["result"].get("personType")
                given = res["result"].get("givenName", "")
                family = res["result"].get("familyName", "")
                self._user_display = f"{given} {family}".strip()
                return True
        except WebUntisError as exc:
            log.debug("Session probe failed: %s", exc)
        except Exception as exc:
            log.debug("Session probe transport error: %s", exc)
        return False

    # ------------------------------------------------------------------
    # HTTP layer
    # ------------------------------------------------------------------
    def _next_id(self) -> str:
        self._rpc_id += 1
        return f"scraper-{self._rpc_id}-{uuid.uuid4().hex[:8]}"

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_request_ts = time.monotonic()

    async def _rpc_raw(self, method: str, params: dict) -> dict[str, Any]:
        assert self._client is not None
        await self._throttle()
        url = f"{self.cfg.base_url}{JSONRPC_PATH}?school={self.cfg.school}"
        body = {
            "id": self._next_id(),
            "method": method,
            "params": params,
            "jsonrpc": "2.0",
        }
        r = await self._client.post(url, json=body)
        if r.status_code != 200:
            raise WebUntisError(
                f"HTTP {r.status_code} from JSON-RPC: {r.text[:200]}"
            )
        data = r.json()
        if "error" in data and data["error"]:
            err = data["error"]
            if isinstance(err, dict):
                raise WebUntisError(
                    f"{err.get('message','RPC error')}: "
                    f"code={err.get('code')}"
                )
            raise WebUntisError(f"RPC error: {err}")
        return data.get("result") or {}

    async def _rpc(self, method: str, params: dict) -> dict[str, Any]:
        return {"result": await self._rpc_raw(method, params)}

    async def _rest_get(self, path: str, params: dict) -> dict[str, Any]:
        assert self._client is not None
        await self._throttle()
        url = f"{self.cfg.base_url}{REST_BASE}{path}"
        r = await self._client.get(url, params=params)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # Public data fetchers (JSON-RPC)
    # ------------------------------------------------------------------
    async def get_schoolyears(self) -> list[dict]:
        return await self._rpc_raw("getSchoolyears", {})

    async def get_current_schoolyear(self) -> Optional[dict]:
        years = await self.get_schoolyears()
        for y in years:
            if y.get("isCurrent"):
                return y
        return years[0] if years else None

    async def get_timetable(
        self, start: date, end: date,
        element_id: Optional[int] = None,
        element_type: Optional[int] = None,
    ) -> list[dict]:
        eid = element_id or self._person_id
        etype = element_type or self._person_type
        if eid is None or etype is None:
            raise WebUntisError("No element id/type known; did login succeed?")

        all_lessons: list[dict] = []
        for week_start, week_end in _weeks(start, end):
            res = await self._rpc_raw("getTimetableForRange", {
                "id": int(eid),
                "type": int(etype),
                "startDate": _to_iso_date(week_start),
                "endDate": _to_iso_date(week_end),
                "showOnlyBaseTimetable": False,
                "showLs": True,
                "showSb": True,
                "showOh": True,
                "showCs": True,
                "showRo": True,
            })
            all_lessons.extend(res or [])
        return all_lessons

    async def get_exams(
        self, start: date, end: date,
        element_id: Optional[int] = None,
        element_type: Optional[int] = None,
    ) -> list[dict]:
        eid = element_id or self._person_id
        etype = element_type or self._person_type
        try:
            return await self._rpc_raw("getExamsForRange", {
                "id": int(eid) if eid else 0,
                "type": int(etype) if etype else 0,
                "startDate": _to_iso_date(start),
                "endDate": _to_iso_date(end),
            })
        except WebUntisError as exc:
            log.debug("getExamsForRange not available: %s", exc)
            return []

    async def get_homework(self, start: date, end: date) -> list[dict]:
        try:
            return await self._rpc_raw("getHomeWorkForRange", {
                "startDate": _to_iso_date(start),
                "endDate": _to_iso_date(end),
            })
        except WebUntisError as exc:
            log.debug("getHomeWorkForRange not available: %s", exc)
            return []

    async def get_absences(self, start: date, end: date) -> list[dict]:
        try:
            return await self._rpc_raw("getAbsencesForRange", {
                "startDate": _to_iso_date(start),
                "endDate": _to_iso_date(end),
            })
        except WebUntisError as exc:
            log.debug("getAbsencesForRange not available: %s", exc)
            return []

    async def get_messages(self) -> list[dict]:
        try:
            return await self._rpc_raw("getMessagesOfInbox", {})
        except WebUntisError as exc:
            log.debug("getMessagesOfInbox not available: %s", exc)
            return []

    # ------------------------------------------------------------------
    # REST v1 (newer UI2020 backend)
    # ------------------------------------------------------------------
    async def get_timetable_grid(self, start: date, end: date) -> dict[str, Any]:
        try:
            app_data = await self._rest_get("/app/data", {})
        except Exception as exc:
            log.debug("REST /app/data failed: %s", exc)
            return {}

        students = (app_data.get("user") or {}).get("students") or []
        if not students:
            log.warning("REST v1: no students in /app/data response")
            return {}
        sid = students[0].get("id")
        if not sid:
            return {}

        params = {
            "start": _to_iso_date(start),
            "end": _to_iso_date(end),
            "format": "1",
            "resourceType": "STUDENT",
            "resources": str(sid),
            "periodTypes": "",
            "timetableType": "MY_TIMETABLE",
            "layout": "START_TIME",
        }
        return await self._rest_get("/timetable/entries", params)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def close(self) -> None:
        try:
            if self._client:
                await self._client.aclose()
        finally:
            if self._page:
                await self._page.close()
                self._page = None
            self._logged_in = False

    @property
    def user_display(self) -> Optional[str]:
        return self._user_display or self.cfg.username
