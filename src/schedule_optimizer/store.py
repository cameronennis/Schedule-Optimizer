"""Read and write app data, and turn it into solver inputs.

Everything that touches the database goes through here, so the UI, the CLI, and
the Claude Desktop helper all share one set of rules.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, delete, select
from tzlocal import get_localzone_name

from . import domain
from .domain import EventInput, Plan, TaskInput, WeeklyTask
from .solver import solve
from .tables import (
    BlockStatus,
    EffortMode,
    EventMeta,
    PlanBlock,
    Preferences,
    ProgressLog,
    Task,
    TaskStatus,
    TitleRule,
)

# ---- preferences ---------------------------------------------------------------


def get_prefs(s: Session) -> Preferences:
    prefs = s.get(Preferences, 1)
    if prefs is None:
        prefs = Preferences(id=1)
        s.add(prefs)
        s.commit()
        s.refresh(prefs)
    return prefs


PREF_FIELDS = {
    "awake_start", "awake_end", "max_focus_minutes_per_day", "buffer_minutes", "horizon_days",
    "time_zone", "busy_calendars", "claude_mode", "setup_complete",
    "chunk_cost", "ideal_deviation_per_hour", "time_of_day_bonus_per_hour",
}


def update_prefs(s: Session, **changes) -> Preferences:
    unknown = set(changes) - PREF_FIELDS
    if unknown:
        raise ValueError(f"unknown preference(s): {', '.join(sorted(unknown))}")
    prefs = get_prefs(s)
    for k, v in changes.items():
        setattr(prefs, k, v)
    validated = Preferences.model_validate(prefs.model_dump())
    if validated.awake_end != time(0) and validated.awake_end <= validated.awake_start:
        raise ValueError("awake_end must be after awake_start (use 00:00 for midnight)")
    if not 1 <= validated.horizon_days <= 28:
        raise ValueError("horizon_days must be 1–28")
    s.add(prefs)
    s.commit()
    s.refresh(prefs)
    return prefs


def local_tz(prefs: Preferences) -> tzinfo:
    return ZoneInfo(prefs.time_zone or get_localzone_name())


def solver_prefs(prefs: Preferences) -> domain.Preferences:
    return domain.Preferences(
        awake_start=prefs.awake_start,
        awake_end=prefs.awake_end,
        max_focus_minutes_per_day=prefs.max_focus_minutes_per_day,
        buffer_minutes=prefs.buffer_minutes,
        horizon_days=prefs.horizon_days,
        chunk_cost=prefs.chunk_cost,
        ideal_deviation_per_hour=prefs.ideal_deviation_per_hour,
        time_of_day_bonus_per_hour=prefs.time_of_day_bonus_per_hour,
    )


def import_legacy_settings(s: Session) -> None:
    """Move Phase 3's settings.json calendar choice into the database, once."""
    from . import local_config

    prefs = get_prefs(s)
    legacy = local_config.busy_calendar_ids()
    if legacy and not prefs.busy_calendars:
        update_prefs(s, busy_calendars=legacy)


# ---- tasks -----------------------------------------------------------------------


def _validate_task(t: Task) -> None:
    Task.model_validate(t.model_dump())  # range checks such as priority 1–5
    if not t.title.strip():
        raise ValueError("title is required")
    if t.effort_mode == EffortMode.TOTAL and not t.total_minutes:
        raise ValueError("total_minutes is required for a total-effort task")
    if t.effort_mode == EffortMode.WEEKLY:
        if not t.weekly_minutes:
            raise ValueError("weekly_minutes is required for a weekly task")
        if t.deadline:
            raise ValueError("weekly tasks can't have a deadline")
    if not 5 <= t.min_block <= t.ideal_block <= t.max_block:
        raise ValueError("block sizes must satisfy 5 ≤ min ≤ ideal ≤ max")
    for name in ("deadline", "earliest_start"):
        v = getattr(t, name)
        if v is not None and v.tzinfo is None:
            raise ValueError(f"{name} must include a time zone")
    if t.deadline and t.earliest_start and t.earliest_start >= t.deadline:
        raise ValueError("earliest_start must be before the deadline")


def add_task(s: Session, **fields) -> Task:
    if fields.get("weekly_minutes") and "effort_mode" not in fields:
        fields["effort_mode"] = EffortMode.WEEKLY
    task = Task.model_validate(fields)  # also turns strings like "evening" into enums
    _validate_task(task)
    s.add(task)
    s.commit()
    s.refresh(task)
    return task


def update_task(s: Session, task_id: int, **changes) -> Task:
    task = s.get(Task, task_id)
    if task is None:
        raise KeyError(f"no task with id {task_id}")
    for k in changes:
        if k in ("id", "created_at") or k not in Task.model_fields:
            raise ValueError(f"can't set {k!r}")
    validated = Task.model_validate({**task.model_dump(), **changes})
    for k in changes:
        setattr(task, k, getattr(validated, k))
    _validate_task(task)
    s.add(task)
    s.commit()
    s.refresh(task)
    return task


def delete_task(s: Session, task_id: int) -> None:
    task = s.get(Task, task_id)
    if task is None:
        raise KeyError(f"no task with id {task_id}")
    s.delete(task)
    s.commit()


def list_tasks(s: Session, include_done: bool = False) -> list[Task]:
    q = select(Task).order_by(Task.id)
    if not include_done:
        q = q.where(Task.status != TaskStatus.DONE)
    return list(s.exec(q))


# ---- progress ----------------------------------------------------------------------


def log_progress(s: Session, task_id: int, minutes: int, start: datetime, note: str = "") -> ProgressLog:
    if s.get(Task, task_id) is None:
        raise KeyError(f"no task with id {task_id}")
    entry = ProgressLog(task_id=task_id, minutes=minutes, start=start, note=note)
    ProgressLog.model_validate(entry.model_dump())
    s.add(entry)
    s.commit()
    s.refresh(entry)
    return entry


def logged_minutes(s: Session, task_id: int, since: datetime | None = None) -> int:
    q = select(ProgressLog.minutes).where(ProgressLog.task_id == task_id)
    if since is not None:
        q = q.where(ProgressLog.start >= since)
    return sum(s.exec(q))


def week_start(now: datetime, tz: tzinfo) -> datetime:
    local = now.astimezone(tz)
    monday = local.date() - timedelta(days=local.weekday())
    return datetime.combine(monday, time(0), tzinfo=tz)


def remaining_minutes(s: Session, task: Task, now: datetime, tz: tzinfo) -> int:
    if task.effort_mode == EffortMode.WEEKLY:
        return max(0, (task.weekly_minutes or 0) - logged_minutes(s, task.id, week_start(now, tz)))
    return max(0, (task.total_minutes or 0) - logged_minutes(s, task.id))


# ---- events ------------------------------------------------------------------------


def add_title_rule(s: Session, pattern: str, skippable: bool = True, skip_cost: float = 4.0) -> TitleRule:
    if not pattern.strip():
        raise ValueError("pattern is required")
    if skip_cost < 0:
        raise ValueError("skip_cost can't be negative")
    rule = TitleRule(pattern=pattern.strip(), skippable=skippable, skip_cost=skip_cost)
    s.add(rule)
    s.commit()
    s.refresh(rule)
    return rule


def list_title_rules(s: Session) -> list[TitleRule]:
    return list(s.exec(select(TitleRule).order_by(TitleRule.id)))


def delete_title_rule(s: Session, rule_id: int) -> None:
    rule = s.get(TitleRule, rule_id)
    if rule is None:
        raise KeyError(f"no rule with id {rule_id}")
    s.delete(rule)
    s.commit()


def set_event_meta(s: Session, google_event_id: str, skippable: bool, skip_cost: float = 4.0) -> EventMeta:
    meta = s.get(EventMeta, google_event_id) or EventMeta(google_event_id=google_event_id)
    meta.skippable, meta.skip_cost = skippable, skip_cost
    s.add(meta)
    s.commit()
    s.refresh(meta)
    return meta


def clear_event_meta(s: Session, google_event_id: str) -> None:
    """Forget per-event settings so title rules apply again."""
    meta = s.get(EventMeta, google_event_id)
    if meta is not None:
        s.delete(meta)
        s.commit()


def event_metas(s: Session) -> dict[str, EventMeta]:
    return {m.google_event_id: m for m in s.exec(select(EventMeta))}


def apply_event_settings(s: Session, events: list[EventInput]) -> list[EventInput]:
    """Mark events skippable: per-event settings first, then the first matching title rule."""
    rules = list_title_rules(s)
    ids = [e.id for e in events]
    metas = {m.google_event_id: m for m in s.exec(select(EventMeta).where(col(EventMeta.google_event_id).in_(ids)))}
    out = []
    for e in events:
        skippable, cost = False, 0.0
        if (m := metas.get(e.id)) is not None:
            skippable, cost = m.skippable, m.skip_cost
        elif rule := next((r for r in rules if r.pattern.lower() in e.title.lower()), None):
            skippable, cost = rule.skippable, rule.skip_cost
        out.append(EventInput(e.id, e.title, e.start, e.end, skippable=skippable,
                              skip_cost=cost if skippable else 0.0))
    return out


# ---- planning ----------------------------------------------------------------------


def solver_inputs(s: Session, now: datetime) -> tuple[list[TaskInput], list[WeeklyTask]]:
    tz = local_tz(get_prefs(s))
    tasks, weekly = [], []
    for t in s.exec(select(Task).where(Task.status == TaskStatus.ACTIVE)):
        common = dict(priority=t.priority, min_block=t.min_block, ideal_block=t.ideal_block,
                      max_block=t.max_block, time_of_day=t.time_of_day)
        if t.effort_mode == EffortMode.WEEKLY:
            weekly.append(WeeklyTask(str(t.id), t.title, weekly_minutes=t.weekly_minutes or 0,
                                     logged_this_week=logged_minutes(s, t.id, week_start(now, tz)), **common))
        else:
            rem = remaining_minutes(s, t, now, tz)
            if rem > 0:
                tasks.append(TaskInput(str(t.id), t.title, remaining_minutes=rem, deadline=t.deadline,
                                       earliest_start=t.earliest_start, **common))
    return tasks, weekly


def _db_task_id(solver_task_id: str) -> int:
    return int(solver_task_id.split("@", 1)[0])  # weekly instances look like "7@2026-11-16"


def save_proposed_plan(s: Session, plan: Plan) -> list[PlanBlock]:
    s.exec(delete(PlanBlock).where(PlanBlock.status == BlockStatus.PROPOSED))
    rows = [PlanBlock(task_id=_db_task_id(b.task_id), start=b.start, end=b.end, score=b.score) for b in plan.blocks]
    s.add_all(rows)
    s.commit()
    return rows


def plan_now(s: Session, busy_events: list[EventInput], now: datetime) -> Plan:
    """Solve from the database and the given calendar events; store the result as proposed."""
    prefs = get_prefs(s)
    tasks, weekly = solver_inputs(s, now)
    events = apply_event_settings(s, busy_events)
    plan = solve(tasks, events, solver_prefs(prefs), now, weekly=weekly)
    if plan.status in ("OPTIMAL", "FEASIBLE"):
        save_proposed_plan(s, plan)
    return plan


def proposed_blocks(s: Session) -> list[PlanBlock]:
    return list(s.exec(select(PlanBlock).where(PlanBlock.status == BlockStatus.PROPOSED).order_by(PlanBlock.start)))


def upcoming_blocks(s: Session, now: datetime) -> list[PlanBlock]:
    """Blocks to show: the proposed plan if there is one, else approved blocks that haven't ended."""
    proposed = proposed_blocks(s)
    if proposed:
        return [b for b in proposed if b.end > now]
    q = select(PlanBlock).where(PlanBlock.status == BlockStatus.APPROVED, PlanBlock.end > now)
    return list(s.exec(q.order_by(PlanBlock.start)))


def approve_plan(s: Session) -> int:
    """Replace future approved blocks with the proposed ones. Returns how many were approved.

    Writing them to the Optimizer calendar happens in Phase 6.
    """
    proposed = proposed_blocks(s)
    if not proposed:
        return 0
    first = min(b.start for b in proposed)
    s.exec(delete(PlanBlock).where(PlanBlock.status == BlockStatus.APPROVED, PlanBlock.start >= first))
    for b in proposed:
        b.status = BlockStatus.APPROVED
        s.add(b)
    s.commit()
    return len(proposed)
