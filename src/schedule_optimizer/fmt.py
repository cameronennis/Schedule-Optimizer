"""How times and dates look to people: 12-hour clock with AM/PM, and MM-DD-YYYY dates.

Everything shown in the window or the CLI goes through here. What Claude reads
stays ISO 8601 (it's unambiguous for a machine), and the database stores UTC.
No %-I or %-d: those strftime codes don't work on Windows.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta


def clock(t: time | datetime) -> str:
    """9:05 AM, 12:00 PM, 11:30 PM."""
    hour = t.hour % 12 or 12
    return f"{hour}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def span(start: datetime | time, end: datetime | time) -> str:
    """9:00–10:30 AM when both share AM/PM, else 11:30 AM–1:20 PM."""
    a, b = clock(start), clock(end)
    if a[-2:] == b[-2:]:
        return f"{a[:-3]}–{b}"
    return f"{a}–{b}"


def day(d: date | datetime) -> str:
    """Thu, Nov 19."""
    return f"{d:%a, %b} {d.day}"


def when(dt: datetime) -> str:
    """Thu, Nov 19, 9:05 AM."""
    return f"{day(dt)}, {clock(dt)}"


def deadline(dt: datetime) -> str:
    """11-19-2026 9:00 AM, or "11-19-2026, end of day" for a midnight deadline."""
    if (dt.hour, dt.minute) == (0, 0):
        return f"{date_text(dt - timedelta(days=1))}, end of day"
    return f"{date_text(dt)} {clock(dt)}"


def date_text(d: date | datetime) -> str:
    """11-19-2026, the format date fields use."""
    return f"{d:%m-%d-%Y}"


def parse_date(text: str) -> date:
    """MM-DD-YYYY (or MM/DD/YYYY); ISO YYYY-MM-DD also works."""
    text = (text or "").strip()
    for pattern in ("%m-%d-%Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    raise ValueError(f"{text!r} isn't a date; use MM-DD-YYYY, e.g. 11-19-2026.")


_CLOCK = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m?\.?\s*$", re.IGNORECASE)


def parse_clock(text: str) -> time:
    """9:05 AM, 9am, 9:05pm, 12 PM; 24-hour 21:05 also works."""
    text = (text or "").strip()
    if m := _CLOCK.match(text):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if 1 <= hour <= 12 and 0 <= minute < 60:
            hour = hour % 12 + (12 if m.group(3).lower() == "p" else 0)
            return time(hour, minute)
    else:
        try:
            return time.fromisoformat(text)
        except ValueError:
            pass
    raise ValueError(f"{text!r} isn't a time; use something like 9:30 AM.")


def time_options(step_minutes: int = 15) -> list[str]:
    """Every step through the day, for time pickers: 12:00 AM, 12:15 AM, …, 11:45 PM."""
    return [clock(time(m // 60, m % 60)) for m in range(0, 24 * 60, step_minutes)]
