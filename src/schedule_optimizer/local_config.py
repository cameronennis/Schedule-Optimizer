"""Temporary JSON settings file until Phase 4 moves settings into SQLite."""

from __future__ import annotations

import json
from pathlib import Path

from .paths import data_dir


def _path() -> Path:
    return data_dir() / "settings.json"


def load() -> dict:
    p = _path()
    return json.loads(p.read_text()) if p.exists() else {}


def save(settings: dict) -> None:
    _path().write_text(json.dumps(settings, indent=2))


def busy_calendar_ids() -> list[str] | None:
    return load().get("busy_calendars")


def set_busy_calendar_ids(ids: list[str]) -> None:
    s = load()
    s["busy_calendars"] = ids
    save(s)
