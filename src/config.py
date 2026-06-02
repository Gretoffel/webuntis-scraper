"""Configuration loader.

Reads settings from a JSON file and sensitive credentials from .env.
JSON keys mirror the env-style names for transparency.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
SESSIONS_DIR = PROJECT_ROOT / "sessions"
OUT_DIR = PROJECT_ROOT / "out"
LOGS_DIR = PROJECT_ROOT / "logs"


@dataclass
class ScraperConfig:
    # WebUntis endpoints
    server: str = ""               # e.g. "mese"
    school: str = ""               # e.g. "htbla_kaindorf"
    base_url: str = ""             # derived
    login_url: str = ""            # derived

    # Credentials (sourced from .env)
    username: str = ""
    password: str = ""

    # Date range for timetable scraping
    days_back: int = 0
    days_forward: int = 14

    # Modules to enable
    scrape_timetable: bool = True
    scrape_exams: bool = True
    scrape_homework: bool = True
    scrape_absences: bool = True
    scrape_messages: bool = True

    # Browser behaviour
    headless: bool = True
    slow_mo_ms: int = 0
    timeout_ms: int = 45000
    locale: str = "de-DE"
    timezone: str = "Europe/Berlin"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    viewport: dict = field(
        default_factory=lambda: {"width": 1440, "height": 900}
    )

    # Output
    output_dir: str = str(OUT_DIR)
    pretty_json: bool = True
    include_raw: bool = False      # dump raw API responses

    # Storage state for session reuse
    storage_state_path: str = str(
        SESSIONS_DIR / "storage_state.json"
    )

    def derived_urls(self) -> None:
        if self.server and self.school:
            self.base_url = f"https://{self.server}.webuntis.com"
            self.login_url = (
                f"{self.base_url}/WebUntis/?school={self.school}"
                "#/basic/login"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.warning("Config file %s is not a JSON object, ignoring", path)
            return {}
        return data
    except json.JSONDecodeError as exc:
        log.error("Invalid JSON in %s: %s", path, exc)
        return {}


def load_config(
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    env_path: Path | str = DEFAULT_ENV_PATH,
) -> ScraperConfig:
    """Build a ScraperConfig from config.json and .env.

    Priority: .env values override config.json values.
    """
    cfg = ScraperConfig()
    json_data = _load_json(Path(config_path))
    env_data = dotenv_values(Path(env_path)) if Path(env_path).exists() else {}

    # Map env-style names to the actual ScraperConfig field names so users
    # can use either UNTIS_PASSWORD (shell style) or password (config.json style).
    ENV_ALIASES = {
        "UNTIS_SERVER": "server",
        "UNTIS_SCHOOL": "school",
        "UNTIS_USERNAME": "username",
        "UNTIS_PASSWORD": "password",
    }

    for k, v in {**json_data, **env_data}.items():
        if v is None or v == "":
            continue
        # Resolve alias
        field_name = ENV_ALIASES.get(k, k)
        # boolean coercion
        if field_name in {
            "headless", "pretty_json", "include_raw",
            "scrape_timetable", "scrape_exams", "scrape_homework",
            "scrape_absences", "scrape_messages",
        }:
            current = getattr(cfg, field_name, False)
            setattr(cfg, field_name, _coerce_bool(v, current))
            continue
        if field_name in {"days_back", "days_forward", "slow_mo_ms", "timeout_ms"}:
            try:
                setattr(cfg, field_name, int(v))
                continue
            except (TypeError, ValueError):
                log.warning("Config key %s=%r is not an int, ignoring", k, v)
                continue
        if hasattr(cfg, field_name):
            setattr(cfg, field_name, v)
        else:
            log.debug("Unknown config key: %s", k)

    cfg.derived_urls()

    if not cfg.server or not cfg.school:
        raise ValueError(
            "server and school must be set in config.json or .env. "
            "See config.example.json."
        )

    if cfg.username and not cfg.password:
        log.warning(
            "Username is set (%r) but password is empty — "
            "check UNTIS_PASSWORD in .env",
            cfg.username,
        )
    elif cfg.password and not cfg.username:
        log.warning("Password is set but username is empty")

    for d in (SESSIONS_DIR, OUT_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)

    pw_set = bool(cfg.password)
    log.info(
        "Config loaded: server=%s school=%s user=%s password=%s "
        "days_back=%d days_forward=%d",
        cfg.server, cfg.school, cfg.username or "<empty>",
        "<set>" if pw_set else "<MISSING>",
        cfg.days_back, cfg.days_forward,
    )
    return cfg
