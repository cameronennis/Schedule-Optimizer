"""Plain data types the solver reads and returns.

These have no database or Google dependencies, so the solver can be tested with
hard-coded data. Later phases convert SQLModel rows and calendar events into them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum


class TimeOfDay(str, Enum):
    ANY = "any"
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"


# Local clock windows for each time-of-day preference (end 00:00 = midnight).
TIME_OF_DAY_WINDOWS: dict[TimeOfDay, tuple[time, time]] = {
    TimeOfDay.MORNING: (time(5, 0), time(12, 0)),
    TimeOfDay.AFTERNOON: (time(12, 0), time(17, 0)),
    TimeOfDay.EVENING: (time(17, 0), time(0, 0)),
}


def priority_to_points_per_hour(priority: int) -> int:
    """Priority 1–5 → 2–10 points per hour."""
    if not 1 <= priority <= 5:
        raise ValueError(f"priority must be 1–5, got {priority}")
    return 2 * priority


@dataclass
class TaskInput:
    """One task as the solver sees it: work still to place, in minutes."""

    id: str
    title: str
    remaining_minutes: int
    priority: int = 3
    deadline: datetime | None = None
    earliest_start: datetime | None = None
    min_block: int = 30
    ideal_block: int = 90
    max_block: int = 120
    time_of_day: TimeOfDay = TimeOfDay.ANY

    @property
    def points_per_hour(self) -> int:
        return priority_to_points_per_hour(self.priority)


@dataclass
class WeeklyTask:
    """A "N minutes per week" intention; expanded into one TaskInput per week."""

    id: str
    title: str
    weekly_minutes: int
    logged_this_week: int = 0
    priority: int = 3
    min_block: int = 30
    ideal_block: int = 90
    max_block: int = 120
    time_of_day: TimeOfDay = TimeOfDay.ANY


@dataclass
class EventInput:
    """A calendar event. Fixed events always block time; skippable ones may be dropped."""

    id: str
    title: str
    start: datetime
    end: datetime
    skippable: bool = False
    skip_cost: float = 0.0  # points; only used if skippable


@dataclass
class Preferences:
    awake_start: time = time(8, 0)
    awake_end: time = time(23, 0)  # 00:00 means midnight
    max_focus_minutes_per_day: int = 360
    buffer_minutes: int = 5
    horizon_days: int = 7
    # Objective weights, all in points (per hour where noted).
    chunk_cost: float = 1.0  # f: fixed cost per block, discourages fragmenting
    ideal_deviation_per_hour: float = 1.0  # a: per hour a block differs from ideal_block
    time_of_day_bonus_per_hour: float = 1.0  # b: per hour inside the preferred window
    stability_per_hour: float = 1.0  # per hour a block stays where the last approved plan put it
    solve_seconds: float = 5.0


@dataclass
class PlanBlock:
    task_id: str
    title: str
    start: datetime
    end: datetime
    # Score terms in points, so "why?" can be explained later.
    score: dict[str, float] = field(default_factory=dict)

    @property
    def minutes(self) -> int:
        return int((self.end - self.start) / timedelta(minutes=1))


@dataclass
class EventDecision:
    event_id: str
    title: str
    start: datetime
    end: datetime
    attend: bool
    skip_cost: float


@dataclass
class Plan:
    status: str  # OPTIMAL, FEASIBLE, INFEASIBLE, ...
    objective_points: float
    blocks: list[PlanBlock]
    events: list[EventDecision]  # skippable events only
    unmet_minutes: dict[str, int]  # task id → minutes the plan couldn't place
    wall_seconds: float

    @property
    def skipped(self) -> list[EventDecision]:
        return [e for e in self.events if not e.attend]
