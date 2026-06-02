"""Export the scraped result to disk."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _maybe_strip_raw(payload: dict, keep_raw: bool) -> dict:
    if keep_raw:
        return payload
    out = {}
    for k, v in payload.items():
        if isinstance(v, dict) and "raw" in v and k != "meta":
            new_v = {kk: vv for kk, vv in v.items() if kk != "raw"}
            out[k] = new_v
        else:
            out[k] = v
    return out


def write_json(
    payload: dict[str, Any],
    out_dir: str,
    pretty: bool = True,
    keep_raw: bool = False,
) -> Path:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = out_path / f"untis_{ts}.json"
    cleaned = _maybe_strip_raw(payload, keep_raw)
    with target.open("w", encoding="utf-8") as f:
        if pretty:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
        else:
            json.dump(cleaned, f, ensure_ascii=False, separators=(",", ":"))
    log.info("Wrote %s (%.1f KB)", target, target.stat().st_size / 1024)
    return target


def write_latest(payload: dict[str, Any], out_dir: str, keep_raw: bool = False) -> Path:
    out_path = Path(out_dir) / "latest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = _maybe_strip_raw(payload, keep_raw)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)
    return out_path
