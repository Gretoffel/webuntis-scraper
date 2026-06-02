"""Command-line entry point."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .browser import BrowserSession
from .config import load_config
from .exporter import write_json, write_latest
from .scraper import Scraper
from .untis_client import WebUntisClient


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down playwright's chatty loggers unless in debug mode.
    if not verbose:
        for noisy in ("playwright", "pyee"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Scrape WebUntis data via Playwright (timetable, exams, "
                    "homework, absences, messages).",
    )
    ap.add_argument(
        "--config", default="config.json",
        help="Path to config.json (default: ./config.json)",
    )
    ap.add_argument(
        "--env", default=".env",
        help="Path to .env file (default: ./.env)",
    )
    ap.add_argument(
        "--no-headless", action="store_true",
        help="Run browser with a visible window (useful for first login / 2FA).",
    )
    ap.add_argument(
        "--clear-session", action="store_true",
        help="Delete the saved storage_state and force a fresh login.",
    )
    ap.add_argument(
        "--keep-raw", action="store_true",
        help="Include raw API payloads in the output JSON.",
    )
    ap.add_argument(
        "--days-back", type=int, default=None,
        help="Override config: how many days in the past to scrape.",
    )
    ap.add_argument(
        "--days-forward", type=int, default=None,
        help="Override config: how many days in the future to scrape.",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return ap.parse_args()


async def _async_main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config, args.env)
    if args.no_headless:
        cfg.headless = False
    if args.keep_raw:
        cfg.include_raw = True
    if args.days_back is not None:
        cfg.days_back = args.days_back
    if args.days_forward is not None:
        cfg.days_forward = args.days_forward

    async with BrowserSession(cfg) as session:
        if args.clear_session:
            await session.clear_session()

        client = WebUntisClient(cfg, session)
        try:
            await client.login()
            scraper = Scraper(cfg, client)
            payload = await scraper.run()
        finally:
            await client.close()

    write_json(
        payload,
        cfg.output_dir,
        pretty=cfg.pretty_json,
        keep_raw=cfg.include_raw,
    )
    write_latest(
        payload,
        cfg.output_dir,
        keep_raw=cfg.include_raw,
    )
    return 0


def main() -> int:
    args = _parse_args()
    _setup_logging(args.verbose)
    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        print("\nAborted by user", file=sys.stderr)
        return 130
    except Exception as exc:
        logging.exception("Fatal error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
