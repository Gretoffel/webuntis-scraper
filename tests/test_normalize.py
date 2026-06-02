"""Tests for the normalizer."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest
from src.normalize import (  # noqa: E402
    normalize_absence,
    normalize_exam,
    normalize_homework,
    normalize_message,
    normalize_timetable_grid,
    normalize_timetable_lesson,
)


class TestNormalizeLesson:
    def test_extracts_subjects_teachers_rooms(self):
        lesson = {
            "id": 1, "date": 20260602, "startTime": 800, "endTime": 845,
            "actType": "Unterricht", "code": "m",
            "su": [{"name": "M", "longname": "Math"}],
            "te": [{"name": "GRI", "longname": "Gruber"}],
            "ro": [{"name": "A101"}],
            "kl": [], "lsw": "", "info": "",
        }
        out = normalize_timetable_lesson(lesson)
        assert out["subjects"][0]["short"] == "M"
        assert out["subjects"][0]["long"] == "Math"
        assert out["teachers"][0]["short"] == "GRI"
        assert out["rooms"][0]["short"] == "A101"
        assert out["is_exam"] is False
        assert out["is_cancelled"] is False

    def test_marks_klausur_as_exam(self):
        lesson = {
            "id": 2, "date": 20260610, "startTime": 900, "endTime": 1100,
            "actType": "Klausur",
            "su": [], "te": [], "ro": [], "kl": [],
        }
        out = normalize_timetable_lesson(lesson)
        assert out["is_exam"] is True

    def test_marks_cancellation(self):
        lesson = {
            "id": 3, "actType": "Entfall",
            "su": [], "te": [], "ro": [], "kl": [],
        }
        out = normalize_timetable_lesson(lesson)
        assert out["is_cancelled"] is True

    def test_handles_missing_fields(self):
        out = normalize_timetable_lesson({})
        assert out["subjects"] == []
        assert out["teachers"] == []
        assert out["is_exam"] is False


class TestNormalizeGrid:
    def test_groups_by_day_and_extracts_positions(self):
        grid = {
            "days": [{
                "date": "2026-06-02", "status": "REGULAR",
                "gridEntries": [{
                    "duration": {"start": "2026-06-02T08:00", "end": "2026-06-02T08:45"},
                    "status": "REGULAR", "type": "NORMAL_TEACHING_PERIOD",
                    "lessonText": "", "substitutionText": "",
                    "position1": [{"current": {"shortName": "M", "longName": "Math", "type": "SUBJECT"}}],
                    "position2": [{"current": {"shortName": "GRI", "type": "TEACHER"}}],
                    "position3": [],
                    "position4": [{"current": {"shortName": "B2", "type": "ROOM"}}],
                }],
            }],
        }
        out = normalize_timetable_grid(grid)
        assert len(out["days"]) == 1
        assert len(out["days"][0]["entries"]) == 1
        e = out["days"][0]["entries"][0]
        assert e["subjects"][0]["short"] == "M"
        assert e["rooms"][0]["short"] == "B2"
        assert e["is_exam"] is False

    def test_skips_empty_days(self):
        out = normalize_timetable_grid({"days": []})
        assert out["days"] == []


class TestOtherNormalizers:
    def test_exam(self):
        e = normalize_exam({
            "id": 1, "examDate": 20260610, "name": "SA Mathe",
            "su": [{"name": "M"}], "te": [], "ro": [], "kl": [],
        })
        assert e["name"] == "SA Mathe"
        assert e["date"] == 20260610

    def test_homework(self):
        h = normalize_homework({
            "id": 1, "text": "S. 42", "dueDate": 20260605, "completed": True,
            "su": [], "te": [],
        })
        assert h["text"] == "S. 42"
        assert h["completed"] is True

    def test_absence(self):
        a = normalize_absence({
            "id": 1, "date": 20260601, "status": "EXCUSED",
            "su": [], "text": "",
        })
        assert a["status"] == "EXCUSED"

    def test_message(self):
        m = normalize_message({
            "id": 1, "subject": "Hi", "preview": "...",
            "sender": "X", "date": 20260601, "read": False,
        })
        assert m["subject"] == "Hi"
        assert m["read"] is False
