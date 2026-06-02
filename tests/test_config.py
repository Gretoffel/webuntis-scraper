"""Tests for the config loader — especially the UNTIS_* env aliases."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.config import load_config  # noqa: E402


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_env_aliases_mapped():
    env = _write(Path(tempfile.mktemp(suffix=".env")),
                 "UNTIS_SERVER=m\nUNTIS_SCHOOL=s\nUNTIS_USERNAME=u\n"
                 "UNTIS_PASSWORD=p\n")
    js = _write(Path(tempfile.mktemp(suffix=".json")), "{}")
    cfg = load_config(js, env)
    assert cfg.server == "m"
    assert cfg.school == "s"
    assert cfg.username == "u"
    assert cfg.password == "p"


def test_env_overrides_json():
    env = _write(Path(tempfile.mktemp(suffix=".env")),
                 "UNTIS_USERNAME=from_env\n")
    js = _write(Path(tempfile.mktemp(suffix=".json")),
                json.dumps({"server": "s", "school": "sc",
                            "username": "from_json"}))
    cfg = load_config(js, env)
    assert cfg.username == "from_env"


def test_missing_server_school_raises():
    js = _write(Path(tempfile.mktemp(suffix=".json")), "{}")
    env = _write(Path(tempfile.mktemp(suffix=".env")), "UNTIS_PASSWORD=x\n")
    try:
        load_config(js, env)
        assert False, "should have raised"
    except ValueError as e:
        assert "server" in str(e) and "school" in str(e)


def test_warns_when_password_missing_with_user():
    """The original bug: UNTIS_PASSWORD env var was being silently
    dropped, sending an empty password to authenticate -> -8504."""
    import logging
    env = _write(Path(tempfile.mktemp(suffix=".env")),
                 "UNTIS_USERNAME=foo\n")  # no password
    js = _write(Path(tempfile.mktemp(suffix=".json")),
                json.dumps({"server": "s", "school": "sc"}))
    caplog = []
    import logging as _logging
    handler = _logging.Handler()
    handler.emit = lambda r: caplog.append(r.getMessage())
    logger = _logging.getLogger("src.config")
    logger.addHandler(handler)
    try:
        cfg = load_config(js, env)
    finally:
        logger.removeHandler(handler)
    assert cfg.username == "foo"
    assert cfg.password == ""
    assert any("password is empty" in m for m in caplog), caplog


def test_derived_urls():
    js = _write(Path(tempfile.mktemp(suffix=".json")),
                json.dumps({"server": "mese", "school": "htl"}))
    env = _write(Path(tempfile.mktemp(suffix=".env")), "")
    cfg = load_config(js, env)
    assert cfg.base_url == "https://mese.webuntis.com"
    assert "school=htl" in cfg.login_url
