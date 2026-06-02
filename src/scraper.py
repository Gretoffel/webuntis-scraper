"""High-level orchestrator: coordinates the WebUntisClient and the
normalizer to produce a single structured payload."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any

from .config import ScraperConfig
from .normalize import (
    normalize_absence,
    normalize_exam,
    normalize_homework,
    normalize_message,
    normalize_timetable_grid,
    normalize_timetable_lesson,
)
from .untis_client import WebUntisClient

log = logging.getLogger(__name__)


class Scraper:
    def __init__(self, cfg: ScraperConfig, client: WebUntisClient):
        self.cfg = cfg
        self.client = client

    async def run(self) -> dict[str, Any]:
        today = date.today()
        start = today - timedelta(days=self.cfg.days_back)
        end = today + timedelta(days=self.cfg.days_forward)
        log.info("Scraping window: %s .. %s", start, end)

        result: dict[str, Any] = {
            "meta": {
                "school": self.cfg.school,
                "server": self.cfg.server,
                "user": self.client.user_display or self.cfg.username,
                "generated_at": date.today().isoformat(),
                "window": {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
            }
        }

        tasks: list[tuple[str, Any]] = []
        if self.cfg.scrape_timetable:
            tasks.append(("timetable", self._scrape_timetable(start, end)))
        if self.cfg.scrape_exams:
            tasks.append(("exams", self._scrape_exams(start, end)))
        if self.cfg.scrape_homework:
            tasks.append(("homework", self._scrape_homework(start, end)))
        if self.cfg.scrape_absences:
            tasks.append(("absences", self._scrape_absences(start, end)))
        if self.cfg.scrape_messages:
            tasks.append(("messages", self._scrape_messages()))

        # Sequential, not parallel - the server throttles per session.
        for name, coro in tasks:
            try:
                result[name] = await coro
                log.info("✓ %-10s : %d entries", name, _count(result[name]))
            except Exception as exc:
                log.error("✗ %-10s : %s", name, exc)
                result[name] = {"error": str(exc)}

        return result

    # --- module scrapers ------------------------------------------------

    async def _scrape_timetable(self, start: date, end: date) -> dict[str, Any]:
        """Try the new REST v1 grid first, fall back to JSON-RPC."""
        try:
            grid = await self.client.get_timetable_grid(start, end)
            if grid and (grid.get("days") or []):
                return {
                    "source": "rest_v1",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    **normalize_timetable_grid(grid),
                }
        except Exception as exc:
            log.debug("REST v1 grid failed, falling back to JSON-RPC: %s", exc)

        raw = await self.client.get_timetable(start, end)
        lessons = [normalize_timetable_lesson(x) for x in raw]
        return {
            "source": "jsonrpc",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "lessons": lessons,
        }

    async def _scrape_exams(self, start: date, end: date) -> dict[str, Any]:
        raw = await self.client.get_exams(start, end)
        if raw:
            return {
                "source": "jsonrpc",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "exams": [normalize_exam(x) for x in raw],
            }
        # Fallback: extract exam lessons from the timetable (Klausur entries)
        # so students without getExams access still get their data.
        log.info("Exam endpoint not available, deriving exams from timetable")
        tt = await self._scrape_timetable(start, end)
        lessons = tt.get("lessons") or []
        exams = [
            l for l in lessons
            if l.get("is_exam")
            or "Klausur" in (l.get("type") or "")
            or "Prüfung" in (l.get("type") or "")
        ]
        return {
            "source": "timetable_fallback",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "exams": exams,
            "note": "Derived from timetable activity types; no dedicated exam endpoint.",
        }

    async def _scrape_homework(self, start: date, end: date) -> dict[str, Any]:
        raw = await self.client.get_homework(start, end)
        return {
            "source": "jsonrpc" if raw else "none",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "items": [normalize_homework(x) for x in raw],
        }

    async def _scrape_absences(self, start: date, end: date) -> dict[str, Any]:
        raw = await self.client.get_absences(start, end)
        return {
            "source": "jsonrpc" if raw else "none",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "items": [normalize_absence(x) for x in raw],
        }

    async def _scrape_messages(self) -> dict[str, Any]:
        raw = await self.client.get_messages()
        return {
            "source": "jsonrpc" if raw else "none",
            "items": [normalize_message(x) for x in raw],
        }


def _count(value: Any) -> int:
    if isinstance(value, dict):
        if "items" in value and isinstance(value["items"], list):
            return len(value["items"])
        if "lessons" in value and isinstance(value["lessons"], list):
            return len(value["lessons"])
        if "exams" in value and isinstance(value["exams"], list):
            return len(value["exams"])
        if "days" in value and isinstance(value["days"], list):
            return sum(len(d.get("entries") or []) for d in value["days"])
        return 0
    if isinstance(value, list):
        return len(value)
    return 0
