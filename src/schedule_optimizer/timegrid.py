"""Convert between real datetimes and the solver's integer time slots.

CP-SAT only handles integers, so time is measured in 5-minute slots counted from
the start of the planning horizon. All arithmetic is done in UTC: subtracting two
local datetimes that share a tzinfo ignores DST changes in Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

SLOT_MINUTES = 5
SLOT = timedelta(minutes=SLOT_MINUTES)


def minutes_to_slots(minutes: int) -> int:
    """Round a duration up to whole slots."""
    return -(-minutes // SLOT_MINUTES)


def slots_to_minutes(slots: int) -> int:
    return slots * SLOT_MINUTES


def ceil_to_slot(dt: datetime) -> datetime:
    """Round an aware datetime up to the next 5-minute boundary."""
    _require_aware(dt)
    floored = dt.replace(second=0, microsecond=0, minute=dt.minute - dt.minute % SLOT_MINUTES)
    return floored if floored == dt else floored + SLOT


def _require_aware(dt: datetime) -> None:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"datetime must be timezone-aware: {dt!r}")


@dataclass(frozen=True)
class TimeGrid:
    """A horizon of `num_slots` 5-minute slots starting at `origin`.

    `tz` is the user's local time zone, used for day boundaries and awake hours.
    """

    origin: datetime
    num_slots: int

    def __post_init__(self) -> None:
        _require_aware(self.origin)

    @classmethod
    def starting(cls, now: datetime, days: int) -> TimeGrid:
        return cls(origin=ceil_to_slot(now), num_slots=days * 24 * 60 // SLOT_MINUTES)

    @property
    def tz(self):
        return self.origin.tzinfo

    @property
    def end(self) -> datetime:
        return self.to_datetime(self.num_slots)

    def to_slot_floor(self, dt: datetime) -> int:
        _require_aware(dt)
        seconds = (dt.astimezone(timezone.utc) - self.origin.astimezone(timezone.utc)).total_seconds()
        return int(seconds // (SLOT_MINUTES * 60))

    def to_slot_ceil(self, dt: datetime) -> int:
        _require_aware(dt)
        seconds = (dt.astimezone(timezone.utc) - self.origin.astimezone(timezone.utc)).total_seconds()
        return int(-(-seconds // (SLOT_MINUTES * 60)))

    def clamp(self, slot: int) -> int:
        return max(0, min(self.num_slots, slot))

    def to_datetime(self, slot: int) -> datetime:
        utc = self.origin.astimezone(timezone.utc) + slot * SLOT
        return utc.astimezone(self.tz)

    def local_dates(self) -> list[date]:
        """Every local calendar date the horizon touches."""
        first = self.origin.astimezone(self.tz).date()
        last = (self.end - timedelta(microseconds=1)).astimezone(self.tz).date()
        return [first + timedelta(days=i) for i in range((last - first).days + 1)]

    def local_window(self, day: date, start: time, end: time) -> tuple[int, int] | None:
        """Slots [lo, hi) for local clock times start–end on `day`, clipped to the horizon.

        `end` of 00:00 means midnight at the end of the day.
        """
        lo_dt = datetime.combine(day, start, tzinfo=self.tz)
        end_day = day + timedelta(days=1) if end == time(0) else day
        hi_dt = datetime.combine(end_day, end, tzinfo=self.tz)
        lo = self.clamp(self.to_slot_ceil(lo_dt))
        hi = self.clamp(self.to_slot_floor(hi_dt))
        return (lo, hi) if lo < hi else None
