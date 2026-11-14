"""Database tables (SQLModel). One SQLite file holds everything the app owns.

Calendar events themselves stay in Google; EventMeta and TitleRule store only
your extra parameters for them. All datetimes are stored as UTC.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from enum import Enum

from sqlalchemy import DateTime, TypeDecorator
from sqlmodel import JSON, Column, Field, SQLModel

from .domain import TimeOfDay


class UTCDateTime(TypeDecorator):
    """Store aware datetimes as naive UTC; return them as aware UTC.

    SQLite has no time-zone type, so without this a 9:00 Pacific deadline would
    silently come back as 9:00 with no zone.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"naive datetime not allowed in the database: {value!r}")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt(**kw):
    return Field(sa_type=UTCDateTime, **kw)


class EffortMode(str, Enum):
    TOTAL = "total"
    WEEKLY = "weekly"


class TaskStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    DONE = "done"


class ClaudeMode(str, Enum):
    OFF = "off"
    DESKTOP = "desktop"
    API_KEY = "api_key"


class BlockStatus(str, Enum):
    PROPOSED = "proposed"  # latest solve, not yet approved
    APPROVED = "approved"  # approved; written (or to be written) to the Optimizer calendar


class Task(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    priority: int = Field(default=3, ge=1, le=5)
    effort_mode: EffortMode = EffortMode.TOTAL
    total_minutes: int | None = None  # required if effort_mode == total
    weekly_minutes: int | None = None  # required if effort_mode == weekly
    deadline: datetime | None = _dt(default=None)  # total mode only
    earliest_start: datetime | None = _dt(default=None)
    min_block: int = 30
    ideal_block: int = 90
    max_block: int = 120
    time_of_day: TimeOfDay = TimeOfDay.ANY
    status: TaskStatus = TaskStatus.ACTIVE
    notes: str = ""
    created_at: datetime = _dt(default_factory=utcnow)
    completed_at: datetime | None = _dt(default=None)  # when marked done; done tasks are deleted 7 days later (v6)


class EventMeta(SQLModel, table=True):
    """Your settings for one calendar event instance (overrides title rules)."""

    google_event_id: str = Field(primary_key=True)
    skippable: bool = False
    skip_cost: float = 4.0  # points; only used if skippable
    updated_at: datetime = _dt(default_factory=utcnow)


class TitleRule(SQLModel, table=True):
    """Default for any event whose title contains `pattern` (case-insensitive).

    No rules exist by default; you create them manually or through Claude.
    """

    id: int | None = Field(default=None, primary_key=True)
    pattern: str
    skippable: bool = True
    skip_cost: float = 4.0
    created_at: datetime = _dt(default_factory=utcnow)


class Preferences(SQLModel, table=True):
    """A single row (id = 1) of global settings, editable in setup and Preferences."""

    id: int = Field(default=1, primary_key=True)
    awake_start: time = time(8, 0)
    awake_end: time = time(23, 0)
    max_focus_minutes_per_day: int = 360
    buffer_minutes: int = 5
    horizon_days: int = 7
    time_zone: str | None = None  # IANA name; None = this computer's zone
    busy_calendars: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    setup_complete: bool = False
    # Objective weights, in points (see domain.Preferences).
    chunk_cost: float = 1.0
    ideal_deviation_per_hour: float = 1.0
    time_of_day_bonus_per_hour: float = 1.0
    stability_per_hour: float = 1.0  # added in schema v2
    optimizer_calendar_id: str | None = None  # the app's own Google calendar (schema v2)
    claude_monthly_cap_usd: float = 5.0  # API-key spending cap (schema v3)
    claude_mode: ClaudeMode = ClaudeMode.OFF  # unused since schema v6 (kept so older files still open)
    desktop_helper_seen_at: datetime | None = _dt(default=None)  # last time Claude Desktop started the helper (v6)


class ProgressLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", index=True, ondelete="CASCADE")
    start: datetime = _dt()
    minutes: int = Field(gt=0)
    note: str = ""


class PlanBlock(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", index=True, ondelete="CASCADE")
    start: datetime = _dt()
    end: datetime = _dt()
    status: BlockStatus = BlockStatus.PROPOSED
    google_event_id: str | None = None  # set once written to the Optimizer calendar
    score: dict = Field(default_factory=dict, sa_column=Column(JSON))  # for "why?"
    solved_at: datetime = _dt(default_factory=utcnow)


# ---- schema v3: Claude integration ------------------------------------------------


class ProposalStatus(str, Enum):
    PENDING = "pending"
    APPLIED = "applied"
    DISMISSED = "dismissed"


class Proposal(SQLModel, table=True):
    """A set of input changes Claude suggested. Nothing changes until you apply it."""

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = _dt(default_factory=utcnow)
    source: str = "claude"  # "claude-desktop" or "api"
    summary: str = ""
    changes: list = Field(default_factory=list, sa_column=Column(JSON))
    preview: dict = Field(default_factory=dict, sa_column=Column(JSON))  # before/after, for review
    status: ProposalStatus = ProposalStatus.PENDING
    decided_at: datetime | None = _dt(default=None)


class Hold(SQLModel, table=True):
    """Time set aside, such as a break. Blocks planning, and is written to the Optimizer
    calendar (never your other calendars) on Approve plan."""

    id: int | None = Field(default=None, primary_key=True)
    title: str
    start: datetime = _dt()
    end: datetime = _dt()
    created_at: datetime = _dt(default_factory=utcnow)


class LastSolve(SQLModel, table=True):
    """A summary of the latest solve (single row, id = 1), so the app window and the
    Claude Desktop helper see the same plan details."""

    id: int = Field(default=1, primary_key=True)
    solved_at: datetime = _dt(default_factory=utcnow)
    status: str = ""
    objective_points: float = 0.0
    wall_seconds: float = 0.0
    unmet: dict = Field(default_factory=dict, sa_column=Column(JSON))  # solver task id → minutes
    events: list = Field(default_factory=list, sa_column=Column(JSON))  # busy events with attend decisions
    conflicts: list = Field(default_factory=list, sa_column=Column(JSON))
    source: str = "app"  # who ran it: app, cli, claude-desktop, api
    approved_at: datetime | None = _dt(default=None)  # when this solve was approved; None until then (schema v4)


class ApiUsage(SQLModel, table=True):
    """Spending per calendar month in API-key mode, to enforce the monthly cap."""

    month: str = Field(primary_key=True)  # "2026-11"
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class ChatCode(SQLModel, table=True):
    """Lets one Claude Desktop chat use the helper (schema v5).

    The app makes a code and copies "ACTIVATE SCHEDULE OPTIMIZER FOR THIS CHAT (code …)"
    for you to paste into a chat. Every helper call must carry an active code, so a chat
    you never pasted it into can't use the tools, even if the connector is on there.
    Codes last until you end them in the app, one at a time or all at once.
    """

    code: str = Field(primary_key=True)
    created_at: datetime = _dt(default_factory=utcnow)
    expires_at: datetime = _dt()  # unused since schema v7: codes last until you end them
    revoked: bool = False
    last_used_at: datetime | None = _dt(default=None)  # last tool call with this code (v7)
