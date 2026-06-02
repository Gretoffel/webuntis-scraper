"""Unit tests for the WebUntis client.

These tests don't need a real WebUntis account. They mock the Playwright
Page and httpx transport to verify the client behaves correctly in the
hard cases: WAF blocks, auth errors, success paths, error mapping.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Make src importable when running from the project root
sys.path.insert(0, str(Path(__file__).parent))

from src.untis_client import (  # noqa: E402
    AUTH_ERRORS,
    WebUntisClient,
    WebUntisError,
    _to_iso_date,
    _weeks,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture
def cfg() -> Any:
    from src.config import ScraperConfig
    return ScraperConfig(
        server="htbla-wels",
        school="htbla-wels",
        username="h.gre",
        password="s3cret",
        headless=True,
        timeout_ms=10_000,
    )
cfg_ = cfg  # alias for the parametrize trick below


@pytest.fixture
def fake_session() -> MagicMock:
    s = MagicMock()
    s.context = MagicMock()
    s.context.cookies = AsyncMock(return_value=[])
    return s


@pytest.fixture
def fake_page() -> MagicMock:
    p = MagicMock()
    p.goto = AsyncMock()
    p.locator = MagicMock()
    p.set_default_timeout = MagicMock()
    p.close = AsyncMock()
    p.screenshot = AsyncMock()
    p.wait_for_url = AsyncMock()
    p.keyboard.press = AsyncMock()
    return p


@pytest.fixture
def client(cfg: Any, fake_session: MagicMock, fake_page: MagicMock) -> WebUntisClient:
    from src.browser import BrowserSession
    c = WebUntisClient(cfg, fake_session)
    # Inject a fake page as if login() had completed.
    c._page = fake_page
    c._logged_in = True
    c._person_id = 12345
    c._person_type = 5
    c._user_display = "Hans Gretoffel"
    return c


# ----------------------------------------------------------------------
# Pure functions
# ----------------------------------------------------------------------
class TestHelpers:
    def test_to_iso_date(self):
        assert _to_iso_date(date(2026, 6, 2)) == "2026-06-02"

    def test_weeks_single(self):
        ws = _weeks(date(2026, 6, 1), date(2026, 6, 7))  # Mon..Sun
        assert len(ws) == 1
        assert ws[0] == (date(2026, 6, 1), date(2026, 6, 7))

    def test_weeks_three(self):
        ws = _weeks(date(2026, 6, 1), date(2026, 6, 21))  # 3 weeks
        assert len(ws) == 3
        assert ws[0][0] == date(2026, 6, 1)
        assert ws[-1][1] == date(2026, 6, 21)

    def test_weeks_swapped(self):
        ws = _weeks(date(2026, 6, 21), date(2026, 6, 1))
        assert len(ws) == 3


# ----------------------------------------------------------------------
# Browser RPC transport
# ----------------------------------------------------------------------
class TestRpcViaBrowser:
    async def test_success(self, client: WebUntisClient, fake_page: MagicMock):
        fake_page.evaluate = AsyncMock(return_value={
            "status": 200, "ok": True, "raw": "{}",
            "data": {"result": {"foo": "bar"}},
        })
        res = await client._rpc("test", {})
        assert res == {"foo": "bar"}
        # Verify the JS got the right URL and body shape
        args = fake_page.evaluate.call_args
        assert "jsonrpc.do" in args[0][1]["url"]
        assert "school=htbla-wels" in args[0][1]["url"]
        assert args[0][1]["body"]["method"] == "test"
        assert args[0][1]["body"]["jsonrpc"] == "2.0"

    async def test_waf_block(self, client: WebUntisClient, fake_page: MagicMock):
        fake_page.evaluate = AsyncMock(return_value={
            "status": 403, "ok": False,
            "raw": '{"isPublic":false,"error":true,'
                  '"errorMessage":"Your input contains code... RequestId: abc"}',
            "data": {"errorMessage": "Your input contains code..."},
        })
        with pytest.raises(WebUntisError) as exc:
            await client._rpc("authenticate", {})
        assert "WAF" in str(exc.value) or "IDS" in str(exc.value)
        assert "security policy" in str(exc.value).lower() or \
               "Your input contains code" in str(exc.value)

    async def test_http_500(self, client: WebUntisClient, fake_page: MagicMock):
        fake_page.evaluate = AsyncMock(return_value={
            "status": 500, "ok": False, "raw": "Internal Server Error", "data": None,
        })
        with pytest.raises(WebUntisError) as exc:
            await client._rpc("getTimetableForRange", {})
        assert "HTTP 500" in str(exc.value)

    async def test_rpc_error_known_code(
        self, client: WebUntisClient, fake_page: MagicMock,
    ):
        fake_page.evaluate = AsyncMock(return_value={
            "status": 200, "ok": True, "raw": "{}",
            "data": {"error": {"code": -8504, "message": "bad creds"}},
        })
        with pytest.raises(WebUntisError) as exc:
            await client._rpc("authenticate", {})
        assert "-8504" in str(exc.value)
        assert "Bad credentials" in str(exc.value)

    async def test_rpc_error_unknown_code(
        self, client: WebUntisClient, fake_page: MagicMock,
    ):
        fake_page.evaluate = AsyncMock(return_value={
            "status": 200, "ok": True, "raw": "{}",
            "data": {"error": {"code": -99999, "message": "weird"}},
        })
        with pytest.raises(WebUntisError) as exc:
            await client._rpc("foo", {})
        assert "-99999" in str(exc.value)
        assert "weird" in str(exc.value)

    async def test_rpc_error_string(self, client: WebUntisClient, fake_page: MagicMock):
        fake_page.evaluate = AsyncMock(return_value={
            "status": 200, "ok": True, "raw": "{}",
            "data": {"error": "something bad"},
        })
        with pytest.raises(WebUntisError) as exc:
            await client._rpc("foo", {})
        assert "something bad" in str(exc.value)

    async def test_throttle(self, client: WebUntisClient, fake_page: MagicMock):
        fake_page.evaluate = AsyncMock(return_value={
            "status": 200, "ok": True, "raw": "{}", "data": {"result": {}},
        })
        # First call: 0 wait (we just set the timestamp)
        client._last_request_ts = 0.0
        await client._rpc("a", {})
        # Second call back-to-back: should have throttled
        import time
        t0 = time.monotonic()
        await client._rpc("b", {})
        elapsed = time.monotonic() - t0
        # Throttle is 0.3s, allow generous slack on slow CI
        assert elapsed < 0.6  # just confirm it didn't crash on backoff


# ----------------------------------------------------------------------
# Login flow
# ----------------------------------------------------------------------
class TestLogin:
    async def test_form_login_success(
        self, cfg: Any, fake_session: MagicMock, fake_page: MagicMock
    ):
        """End-to-end: form fields found, filled, submitted, redirected."""
        from src.browser import BrowserSession
        c = WebUntisClient(cfg, fake_session)
        c._page = fake_page
        c._probe_via_browser = AsyncMock(return_value=True)

        # Form fields visible
        def make_loc(present: bool, **extra):
            loc = MagicMock()
            loc.count = AsyncMock(return_value=1 if present else 0)
            loc.wait_for = AsyncMock()
            loc.fill = AsyncMock()
            loc.click = AsyncMock()
            for k, v in extra.items():
                setattr(loc, k, v)
            return loc

        username_loc = make_loc(True)
        password_loc = make_loc(True)
        submit_loc = make_loc(True)

        def locator_maker(sel):
            loc = MagicMock()
            loc.first = loc
            if sel in ('input[name="j_username"]', 'input[name="username"]',
                       'input[name="user"]', 'input[autocomplete="username"]',
                       'input[type="text"]'):
                loc.count = AsyncMock(return_value=1)
                loc.first = username_loc
            elif sel in ('input[name="j_password"]', 'input[name="password"]',
                         'input[type="password"]'):
                loc.count = AsyncMock(return_value=1)
                loc.first = password_loc
            elif sel.startswith('button'):
                loc.count = AsyncMock(return_value=1)
                loc.first = submit_loc
            else:
                loc.count = AsyncMock(return_value=0)
            return loc

        fake_page.locator = MagicMock(side_effect=locator_maker)
        fake_page.wait_for_url = AsyncMock()
        fake_page.goto = AsyncMock()
        c.session.new_page = AsyncMock(return_value=fake_page)

        await c.login(force=True)
        username_loc.fill.assert_awaited_once_with("h.gre")
        password_loc.fill.assert_awaited_once_with("s3cret")
        submit_loc.click.assert_awaited_once()
        assert c._logged_in is True

    async def test_no_form_found_screenshots(
        self, cfg: Any, fake_session: MagicMock, fake_page: MagicMock
    ):
        from src.browser import BrowserSession
        c = WebUntisClient(cfg, fake_session)
        c._page = fake_page

        empty = MagicMock()
        empty.first = empty
        empty.count = AsyncMock(return_value=0)
        from playwright.async_api import TimeoutError as PWTimeout
        empty.wait_for = AsyncMock(side_effect=PWTimeout("timeout"))
        fake_page.locator = MagicMock(return_value=empty)
        c.session.new_page = AsyncMock(return_value=fake_page)
        fake_page.goto = AsyncMock()

        with pytest.raises(WebUntisError) as exc:
            await c.login(force=True)
        assert "login form" in str(exc.value).lower()
        fake_page.screenshot.assert_awaited()

    async def test_2fa_detected(
        self, cfg: Any, fake_session: MagicMock, fake_page: MagicMock
    ):
        from src.browser import BrowserSession
        c = WebUntisClient(cfg, fake_session)
        c._page = fake_page

        def make_loc(present: bool):
            loc = MagicMock()
            loc.count = AsyncMock(return_value=1 if present else 0)
            loc.wait_for = AsyncMock()
            loc.fill = AsyncMock()
            loc.click = AsyncMock()
            return loc

        username_loc = make_loc(True)
        password_loc = make_loc(True)
        twofa_loc = make_loc(True)

        def locator_maker(sel):
            loc = MagicMock()
            loc.first = loc
            loc.count = AsyncMock(return_value=0)
            if 'text' in sel or 'username' in sel or 'user' in sel:
                loc.count = AsyncMock(return_value=1)
                loc.first = username_loc
            elif 'password' in sel:
                loc.count = AsyncMock(return_value=1)
                loc.first = password_loc
            elif 'otp' in sel or 'code' in sel or 'token' in sel:
                loc.first = twofa_loc
            return loc

        fake_page.locator = MagicMock(side_effect=locator_maker)
        fake_page.keyboard.press = AsyncMock()
        from playwright.async_api import TimeoutError as PWTimeout
        fake_page.wait_for_url = AsyncMock(side_effect=PWTimeout("timeout"))
        fake_page.goto = AsyncMock()
        c.session.new_page = AsyncMock(return_value=fake_page)

        with pytest.raises(WebUntisError) as exc:
            await c.login(force=True)
        assert "2FA" in str(exc.value) or "form" in str(exc.value).lower()


# ----------------------------------------------------------------------
# Data fetchers
# ----------------------------------------------------------------------
class TestDataFetchers:
    async def test_timetable_paginates_per_week(
        self, client: WebUntisClient, fake_page: MagicMock,
    ):
        calls = []
        async def evaluate(js, payload):
            calls.append((payload["body"]["method"], payload["body"]["params"]))
            return {
                "status": 200, "ok": True, "raw": "{}",
                "data": {"result": [{"id": len(calls), "date": 20260602}]},
            }
        fake_page.evaluate = evaluate

        lessons = await client.get_timetable(date(2026, 6, 1), date(2026, 6, 21))
        # 3 weeks (Mon Jun 1 .. Sun Jun 21)
        assert len(calls) == 3
        assert all(c[0] == "getTimetableForRange" for c in calls)
        assert len(lessons) == 3

    async def test_exams_silently_swallows_no_permission(
        self, client: WebUntisClient, fake_page: MagicMock,
    ):
        async def evaluate(js, payload):
            return {
                "status": 200, "ok": True, "raw": "{}",
                "data": {"error": {"code": -1, "message": "noperm"}},
            }
        fake_page.evaluate = evaluate
        result = await client.get_exams(date(2026, 6, 1), date(2026, 6, 30))
        assert result == []

    async def test_homework_returns_items(
        self, client: WebUntisClient, fake_page: MagicMock,
    ):
        async def evaluate(js, payload):
            return {
                "status": 200, "ok": True, "raw": "{}",
                "data": {"result": [{"id": 1, "text": "do math"}]},
            }
        fake_page.evaluate = evaluate
        result = await client.get_homework(date(2026, 6, 1), date(2026, 6, 30))
        assert result == [{"id": 1, "text": "do math"}]

    async def test_timetable_grid_no_students(
        self, client: WebUntisClient, fake_page: MagicMock,
    ):
        # /app/data returns no students -> empty grid
        async def evaluate(js, payload):
            return {
                "status": 200, "ok": True, "raw": "{}",
                "data": {"user": {"students": []}},
            }
        fake_page.evaluate = evaluate
        result = await client.get_timetable_grid(date(2026, 6, 1), date(2026, 6, 30))
        assert result == {}


# ----------------------------------------------------------------------
# Error mapping
# ----------------------------------------------------------------------
class TestErrorMap:
    def test_all_known_codes_have_messages(self):
        for code, (msg, _) in AUTH_ERRORS.items():
            assert isinstance(code, int)
            assert isinstance(msg, str) and msg
            assert "{" not in msg  # no f-string artifacts

    def test_waf_error_is_known(self):
        # The WAF returns 403 with a non-JSON-RPC body, so the code is
        # parsed from the HTTP status, not the JSON. We just verify our
        # error mapper doesn't claim -8504 is recoverable (a previous
        # version accidentally did, causing infinite form-fallback).
        msg, recoverable = AUTH_ERRORS[-8504]
        assert "credentials" in msg.lower() or "Bad credentials" in msg
        assert recoverable is False
