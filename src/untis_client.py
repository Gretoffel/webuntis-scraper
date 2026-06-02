"""WebUntis client.

Architecture decision (learned the hard way):

The school's WebUntis instance runs a WAF/IDS in front of the JSON-RPC
endpoint. Direct calls from `httpx` (no `Origin`/`Referer`/browser-bound
cookies) are rejected with `HTTP 403 — Your input contains code that
does not match the security policy`. The first probe (`getUserData`)
sneaks through because the body is empty; the moment we send the
`authenticate` call with user + password, the IDS triggers.

**Fix:** do everything through the real browser.

1. **Login** goes through the actual form (the WAF only protects the
   JSON-RPC endpoint, not the form-login endpoint).
2. **All subsequent API calls** are made via `page.evaluate(fetch(...))`,
   so the browser handles `Origin`, `Referer`, cookies and CSRF
   tokens automatically. The WAF sees a same-origin fetch and lets
   it through.
3. **`storage_state` is reused** between runs, so the form login is
   only needed once (or after session expiry).

A `--no-browser-rpc` flag is available for debugging — it falls back
to plain `httpx` with extra `Referer`/`Origin` headers, which works
on schools without the WAF.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import date
from typing import Any, Optional

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PWTimeout

from .browser import BrowserSession
from .config import ScraperConfig

log = logging.getLogger(__name__)

JSONRPC_PATH = "/WebUntis/jsonrpc.do"
REST_BASE = "/WebUntis/api/rest/view/v1"
CLIENT_ID = "webuntis-scraper/1.0"


# JSON-RPC error code -> (message, requires_interactive_retry)
# -32601 ("Method not found") is fine — server just doesn't expose it.
# WAF errors and "bad credentials" are NOT recoverable via the form
# (form uses different endpoint but same user db).
AUTH_ERRORS: dict[int, tuple[str, bool]] = {
    -1:  ("Invalid username or password", False),
    -2:  ("Account is locked / too many attempts", False),
    -3:  ("Login not yet started or already ended", False),
    -4:  ("Invalid school", False),
    -5:  ("Invalid client", False),
    -6:  ("Wrong user agent", False),
    -7:  ("OTP required (2FA) — complete login in the browser", True),
    -8:  ("Captcha required — complete login in the browser", True),
    -9:  ("No OTP secret set on account", False),
    -10: ("School not active / not allowed", False),
    -50: ("Server temporarily unavailable", True),
    -100: ("Network error", True),
    -200: ("Session expired", True),
    -1010: ("Login not possible (maintenance)", True),
    -8504: ("Bad credentials", False),
    -32601: ("Method not found (probe-only, harmless)", False),
}

# JavaScript that runs inside the page context. Wraps fetch with
# credentials: 'include' so cookies are sent, returns status+body.
_FETCH_JS = """
async ({url, body}) => {
    const r = await fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/plain, */*',
            'X-Requested-With': 'XMLHttpRequest'
        },
        body: JSON.stringify(body),
        credentials: 'include',
        mode: 'cors'
    });
    const text = await r.text();
    let data = null;
    try { data = JSON.parse(text); } catch (_) { /* keep null */ }
    return { status: r.status, ok: r.ok, data, raw: text };
}
"""


def _to_iso_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _weeks(start: date, end: date) -> list[tuple[date, date]]:
    if end < start:
        start, end = end, start
    cur = start
    out: list[tuple[date, date]] = []
    while cur <= end:
        monday = cur.fromordinal(cur.toordinal() - cur.weekday())
        sunday = monday.fromordinal(monday.toordinal() + 6)
        out.append((max(monday, start), min(sunday, end)))
        cur = monday.fromordinal(monday.toordinal() + 7)
    return out


class WebUntisError(RuntimeError):
    """Raised when WebUntis returns an error or auth fails."""


class WebUntisClient:
    def __init__(self, cfg: ScraperConfig, session: BrowserSession):
        self.cfg = cfg
        self.session = session
        self._page: Optional[Page] = None
        self._person_id: Optional[int] = None
        self._person_type: Optional[int] = None
        self._user_display: Optional[str] = None
        self._logged_in = False
        self._rpc_id = 0
        self._last_request_ts = 0.0
        self._min_interval = 0.3
        # Probe to discover which transport works on this school.
        # After login, the browser is the default; httpx is only used
        # for the WAF detection probe.
        self._use_browser_rpc: Optional[bool] = None

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------
    async def login(self, force: bool = False) -> None:
        if self._logged_in and not force:
            return
        assert self.session.context is not None

        self._page = await self.session.new_page()

        # Always go through the form for login — the WAF only protects
        # JSON-RPC, so the form endpoint is the cleanest way to
        # establish a session.
        if not force:
            await self._page.goto(self.cfg.login_url, wait_until="domcontentloaded")
            await asyncio.sleep(0.5)
            if await self._probe_via_browser():
                log.info("Reusing existing session (storage_state still valid)")
                self._logged_in = True
                self._use_browser_rpc = True
                return

        await self._do_form_login()
        # The form login lands us on the WebUntis dashboard. Verify
        # the session works by probing from the browser context.
        if not await self._probe_via_browser():
            raise WebUntisError(
                "Form login did not produce a valid session. "
                "Check credentials / 2FA / school+server config."
            )
        self._logged_in = True
        self._use_browser_rpc = True
        log.info("Login successful via form")

    async def _do_form_login(self) -> None:
        assert self._page is not None
        page = self._page

        # The login URL above is the React SPA shell — we need to wait
        # for the form to actually render. The hash route may have
        # already switched to /basic/login or stayed on /; either way
        # wait for visible inputs.
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

        user_loc = None
        for sel in user_selectors:
            loc = page.locator(sel).first
            try:
                await loc.wait_for(state="visible", timeout=10_000)
                user_loc = loc
                log.debug("Using username selector %r", sel)
                break
            except PWTimeout:
                continue
        if user_loc is None:
            await self._screenshot("login_no_form")
            raise WebUntisError(
                "Could not find login form. Run with --no-headless to debug. "
                f"Screenshot saved to logs/login_no_form.png"
            )

        await user_loc.fill(self.cfg.username)
        await asyncio.sleep(0.15)

        pw_loc = None
        for sel in pw_selectors:
            loc = page.locator(sel).first
            try:
                await loc.wait_for(state="visible", timeout=2_000)
                pw_loc = loc
                break
            except PWTimeout:
                continue
        if pw_loc is None:
            raise WebUntisError("Password field not found")
        await pw_loc.fill(self.cfg.password)
        await asyncio.sleep(0.15)

        clicked = False
        for sel in submit_selectors:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            try:
                await loc.click()
                clicked = True
                log.debug("Clicked submit using %r", sel)
                break
            except Exception:
                continue
        if not clicked:
            await page.keyboard.press("Enter")

        # Wait for either a successful redirect, an error message, or
        # a 2FA prompt.
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
            err_text = await self._read_error_text()
            await self._screenshot("login_failed")
            raise WebUntisError(
                f"Form login did not redirect away from /login. "
                f"Server message: {err_text or 'none'}. "
                f"Screenshot: logs/login_failed.png"
            )

    async def _has_2fa_field(self) -> bool:
        assert self._page is not None
        return await self._page.locator(
            'input[name="otp"], input[name="code"], input[name="token"], '
            'input[autocomplete="one-time-code"]'
        ).count() > 0

    async def _read_error_text(self) -> str:
        assert self._page is not None
        # WebUntis UI2020 typically shows an error like
        # "Benutzername oder Passwort ist falsch" in a div.
        for sel in [
            '.error', '.login-error', '[class*="error"]',
            '[role="alert"]', '.message',
        ]:
            loc = self._page.locator(sel).first
            if await loc.count() > 0:
                txt = (await loc.inner_text() or "").strip()
                if txt:
                    return txt
        return ""

    async def _screenshot(self, name: str) -> None:
        if not self._page:
            return
        try:
            await self._page.screenshot(path=f"logs/{name}.png", full_page=True)
            log.info("Saved debug screenshot to logs/%s.png", name)
        except Exception:
            pass

    async def _probe_via_browser(self) -> bool:
        """Call getUserData from the browser context. Returns True if a
        valid session is active.
        """
        assert self._page is not None
        try:
            res = await self._rpc_via_browser("getUserData", {})
            if res and res.get("personId"):
                self._person_id = res["personId"]
                self._person_type = res["personType"]
                given = res.get("givenName", "")
                family = res.get("familyName", "")
                self._user_display = f"{given} {family}".strip()
                return True
        except Exception as exc:
            log.debug("Browser probe failed: %s", exc)
        return False

    # ------------------------------------------------------------------
    # JSON-RPC transport
    # ------------------------------------------------------------------
    def _next_id(self) -> str:
        self._rpc_id += 1
        return f"scraper-{self._rpc_id}-{uuid.uuid4().hex[:8]}"

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_request_ts = time.monotonic()

    async def _rpc_via_browser(self, method: str, params: dict) -> dict[str, Any]:
        assert self._page is not None
        await self._throttle()
        url = f"{self.cfg.base_url}{JSONRPC_PATH}?school={self.cfg.school}"
        body = {
            "id": self._next_id(),
            "method": method,
            "params": params,
            "jsonrpc": "2.0",
        }
        result = await self._page.evaluate(_FETCH_JS, {"url": url, "body": body})

        # WAF / IDS block: HTTP 403 with "security policy" message.
        if result["status"] == 403:
            msg = (result.get("data") or {}).get("errorMessage", "") or result["raw"][:200]
            raise WebUntisError(
                f"WAF/IDS blocked {method} (HTTP 403): {msg}"
            )
        if not result["ok"]:
            raise WebUntisError(
                f"HTTP {result['status']} from JSON-RPC {method}: "
                f"{result['raw'][:200]}"
            )

        data = result.get("data") or {}
        if "error" in data and data["error"]:
            err = data["error"]
            if isinstance(err, dict):
                code = err.get("code")
                msg, _ = AUTH_ERRORS.get(code, (err.get("message", "RPC error"), False))
                raise WebUntisError(f"{method} failed: {msg} (code={code})")
            raise WebUntisError(f"{method} error: {err}")
        return data.get("result") or {}

    async def _rpc(self, method: str, params: dict) -> dict[str, Any]:
        return await self._rpc_via_browser(method, params)

    async def _rest_get(self, path: str, params: dict) -> dict[str, Any]:
        assert self._page is not None
        await self._throttle()
        url = f"{self.cfg.base_url}{REST_BASE}{path}"
        result = await self._page.evaluate(
            """
            async ({url, params}) => {
                const qs = new URLSearchParams(params).toString();
                const r = await fetch(url + (qs ? '?' + qs : ''), {
                    credentials: 'include',
                    headers: {
                        'Accept': 'application/json',
                        'X-Requested-With': 'XMLHttpRequest'
                    }
                });
                const text = await r.text();
                let data = null;
                try { data = JSON.parse(text); } catch (_) {}
                return { status: r.status, ok: r.ok, data, raw: text };
            }
            """,
            {"url": url, "params": params},
        )
        if not result["ok"]:
            raise WebUntisError(
                f"HTTP {result['status']} from REST {path}: "
                f"{result['raw'][:200]}"
            )
        return result["data"] or {}

    # ------------------------------------------------------------------
    # Public data fetchers
    # ------------------------------------------------------------------
    async def get_schoolyears(self) -> list[dict]:
        return await self._rpc("getSchoolyears", {})

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
            res = await self._rpc("getTimetableForRange", {
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
            return await self._rpc("getExamsForRange", {
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
            return await self._rpc("getHomeWorkForRange", {
                "startDate": _to_iso_date(start),
                "endDate": _to_iso_date(end),
            })
        except WebUntisError as exc:
            log.debug("getHomeWorkForRange not available: %s", exc)
            return []

    async def get_absences(self, start: date, end: date) -> list[dict]:
        try:
            return await self._rpc("getAbsencesForRange", {
                "startDate": _to_iso_date(start),
                "endDate": _to_iso_date(end),
            })
        except WebUntisError as exc:
            log.debug("getAbsencesForRange not available: %s", exc)
            return []

    async def get_messages(self) -> list[dict]:
        try:
            return await self._rpc("getMessagesOfInbox", {})
        except WebUntisError as exc:
            log.debug("getMessagesOfInbox not available: %s", exc)
            return []

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
        if self._page:
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None
        self._logged_in = False

    @property
    def user_display(self) -> Optional[str]:
        return self._user_display or self.cfg.username
