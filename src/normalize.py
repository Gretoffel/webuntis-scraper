"""Normalize raw WebUntis JSON responses into clean, flat structures."""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any, Optional

log = logging.getLogger(__name__)


def _parse_time(s: str) -> Optional[time]:
    if not s:
        return None
    s = str(s)
    # WebUntis returns times as "0800" or "08:00"
    if len(s) == 4 and s.isdigit():
        return time(int(s[:2]), int(s[2:]))
    if ":" in s:
        h, m, *rest = s.split(":")
        sec = int(rest[0]) if rest else 0
        return time(int(h), int(m), sec)
    return None


def _parse_iso_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def normalize_timetable_lesson(lesson: dict) -> dict[str, Any]:
    """Flatten a JSON-RPC timetable element."""
    subjects = lesson.get("su") or []
    teachers = lesson.get("te") or []
    classes = lesson.get("kl") or []
    rooms = lesson.get("ro") or []
    activity_type = lesson.get("actType") or ""
    code = lesson.get("code") or ""
    code_color = (lesson.get("lst") or "").strip()
    return {
        "id": lesson.get("id"),
        "date": lesson.get("date"),
        "start_time": lesson.get("startTime"),
        "end_time": lesson.get("endTime"),
        "type": activity_type,           # "Unterricht", "Klausur", "Standort", ...
        "code": code,                    # e.g. "ph"  (lesson code)
        "is_exam": "Klausur" in activity_type or "Prüfung" in activity_type
                  or "Exam" in activity_type,
        "is_substitution": "Vertretung" in activity_type or "Substitution" in activity_type,
        "is_cancelled": "Entfall" in activity_type or "Cancel" in activity_type,
        "subjects": [
            {"short": s.get("name"), "long": s.get("longname") or s.get("displayname")}
            for s in subjects
        ],
        "teachers": [
            {"short": t.get("name"), "long": t.get("longname") or t.get("displayname")}
            for t in teachers
        ],
        "classes": [
            {"short": c.get("name"), "long": c.get("longname") or c.get("displayname")}
            for c in classes
        ],
        "rooms": [
            {"short": r.get("name"), "long": r.get("longname") or r.get("displayname")}
            for r in rooms
        ],
        "info": lesson.get("info") or "",
        "text": lesson.get("lsw") or "",
        "activity_type_raw": activity_type,
        "raw": lesson,
    }


def normalize_exam(exam: dict) -> dict[str, Any]:
    subjects = exam.get("su") or []
    teachers = exam.get("te") or []
    classes = exam.get("kl") or []
    rooms = exam.get("ro") or []
    return {
        "id": exam.get("id"),
        "date": exam.get("examDate") or exam.get("date"),
        "start_time": exam.get("startTime"),
        "end_time": exam.get("endTime"),
        "name": exam.get("name"),
        "type": exam.get("examType"),
        "subjects": [
            {"short": s.get("name"), "long": s.get("longname") or s.get("displayname")}
            for s in subjects
        ],
        "teachers": [
            {"short": t.get("name"), "long": t.get("longname") or t.get("displayname")}
            for t in teachers
        ],
        "classes": [
            {"short": c.get("name"), "long": c.get("longname") or c.get("displayname")}
            for c in classes
        ],
        "rooms": [
            {"short": r.get("name"), "long": r.get("longname") or r.get("displayname")}
            for r in rooms
        ],
        "raw": exam,
    }


def normalize_homework(hw: dict) -> dict[str, Any]:
    subjects = hw.get("su") or []
    teachers = hw.get("te") or []
    return {
        "id": hw.get("id"),
        "date": hw.get("date"),
        "due_date": hw.get("dueDate"),
        "lesson_date": hw.get("lessonDate"),
        "text": (hw.get("text") or "").strip(),
        "remark": (hw.get("remark") or "").strip(),
        "completed": bool(hw.get("completed")),
        "subjects": [
            {"short": s.get("name"), "long": s.get("longname") or s.get("displayname")}
            for s in subjects
        ],
        "teachers": [
            {"short": t.get("name"), "long": t.get("longname") or t.get("displayname")}
            for t in teachers
        ],
        "raw": hw,
    }


def normalize_absence(ab: dict) -> dict[str, Any]:
    return {
        "id": ab.get("id"),
        "date": ab.get("date"),
        "start_time": ab.get("startTime"),
        "end_time": ab.get("endTime"),
        "reason": ab.get("reason"),
        "status": ab.get("status"),     # "OPEN", "EXCUSED", "UNEXCUSED"
        "type": ab.get("type"),
        "subjects": [
            {"short": s.get("name"), "long": s.get("longname") or s.get("displayname")}
            for s in (ab.get("su") or [])
        ],
        "text": ab.get("text") or "",
        "raw": ab,
    }


def normalize_message(msg: dict) -> dict[str, Any]:
    return {
        "id": msg.get("id"),
        "subject": msg.get("subject"),
        "preview": msg.get("preview"),
        "from": msg.get("sender"),
        "date": msg.get("date"),
        "read": bool(msg.get("read")),
        "folder": msg.get("folder"),
        "raw": msg,
    }


def normalize_timetable_grid(grid: dict) -> dict[str, Any]:
    """Convert REST v1 timetable/entries into a per-day structure."""
    days_out: list[dict] = []
    for day in grid.get("days") or []:
        entries_out = []
        for entry in day.get("gridEntries") or []:
            duration = entry.get("duration") or {}
            start = duration.get("start") or ""
            end = duration.get("end") or ""
            entries_out.append({
                "start": start,
                "end": end,
                "status": entry.get("status"),       # REGULAR, CANCEL, SUBSTITUTION, EXAM
                "is_cancelled": entry.get("status") == "CANCEL",
                "is_exam": entry.get("status") == "EXAM",
                "is_substitution": entry.get("status") == "SUBSTITUTION",
                "type": (entry.get("type") or ""),
                "lesson_text": entry.get("lessonText") or "",
                "substitution_text": entry.get("substitutionText") or "",
                "subjects": _collect_position(entry, "position1"),
                "teachers": _collect_position(entry, "position2"),
                "classes": _collect_position(entry, "position3"),
                "rooms": _collect_position(entry, "position4"),
                "raw": entry,
            })
        days_out.append({
            "date": day.get("date"),
            "status": day.get("status"),
            "entries": entries_out,
        })
    return {"days": days_out, "raw": grid}


def _collect_position(entry: dict, key: str) -> list[dict]:
    items = entry.get(key) or []
    out = []
    for it in items:
        cur = it.get("current") or {}
        if not cur:
            continue
        out.append({
            "short": cur.get("shortName"),
            "long": cur.get("longName") or cur.get("displayName"),
            "type": cur.get("type"),
        })
    return out
