"""Browser setup with playwright-stealth.

Builds a Chromium context configured to look like a real user,
applies stealth evasions, and reuses storage_state when available
so we don't have to log in every run.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright_stealth import Stealth

from .config import ScraperConfig

log = logging.getLogger(__name__)


def _build_stealth() -> Stealth:
    """Create a Stealth instance with sensible defaults for WebUntis.

    chrome.runtime is left disabled (default in v2.x) because some
    enterprise Single-Sign-On flows probe chrome.runtime and the
    patched shim can break them.
    """
    return Stealth(
        navigator_languages_override=("de-DE", "de", "en-US", "en"),
    )


class BrowserSession:
    """Context manager wrapping a stealthy Playwright Chromium session."""

    def __init__(self, cfg: ScraperConfig):
        self.cfg = cfg
        self._pw: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.stealth = _build_stealth()

    async def __aenter__(self) -> "BrowserSession":
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            headless=self.cfg.headless,
            slow_mo=self.cfg.slow_mo_ms,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )

        state_path = Path(self.cfg.storage_state_path)
        storage_state = state_path if state_path.exists() else None

        self.context = await self.browser.new_context(
            viewport=self.cfg.viewport,
            user_agent=self.cfg.user_agent,
            locale=self.cfg.locale,
            timezone_id=self.cfg.timezone,
            color_scheme="light",
            java_script_enabled=True,
            storage_state=storage_state,
        )
        await self.stealth.apply_stealth_async(self.context)
        log.info(
            "Browser ready (headless=%s, storage_state=%s)",
            self.cfg.headless, "yes" if storage_state else "no",
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if self.context and self.cfg.storage_state_path:
                await self.context.storage_state(path=self.cfg.storage_state_path)
                log.debug("Saved storage_state to %s", self.cfg.storage_state_path)
        finally:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self._pw:
                await self._pw.stop()

    async def new_page(self) -> Page:
        assert self.context is not None
        page = await self.context.new_page()
        page.set_default_timeout(self.cfg.timeout_ms)
        return page

    async def clear_session(self) -> None:
        path = Path(self.cfg.storage_state_path)
        if path.exists():
            path.unlink()
            log.info("Cleared saved session %s", path)
