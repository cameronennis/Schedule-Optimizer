"""Claude's way of changing things: proposals you review, then apply.

Claude never places blocks. It proposes changes to the solver's *inputs* (tasks,
event skip costs, preferences, holds such as breaks); `propose()` previews their effect on a
scratch copy of the database, and nothing real changes until `apply_proposal()`.
Applying re-plans, and the new plan still waits for "Approve plan" before anything
is written to Google.

Each change is a dict with an "op" key; CHANGE_SCHEMA describes them for Claude.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from sqlmodel import Session

from . import db, store
from .domain import EventInput, Plan
from .tables import EffortMode, Proposal, ProposalStatus, Task, TaskStatus

TASK_FIELDS = {
    "title": {"type": "string"},
    "hours": {"type": "number", "description": "Total work for a one-off task."},
    "weekly_hours": {"type": "number", "description": "Hours per week for a recurring intention (no deadline)."},
    "priority": {"type": "integer", "minimum": 1, "maximum": 5, "description": "1 low … 5 top (2–10 points/hour)."},
    "deadline": {"type": "string", "description": "ISO date or datetime, local time if no offset. A bare date means end of that day."},
    "earliest_start": {"type": "string", "description": "ISO date or datetime."},
    "min_block": {"type": "integer", "description": "Shortest block, minutes."},
    "ideal_block": {"type": "integer", "description": "Ideal block, minutes."},
    "max_block": {"type": "integer", "description": "Longest block, minutes (soft)."},
    "time_of_day": {"type": "string", "enum": ["any", "morning", "afternoon", "evening"]},
    "notes": {"type": "string"},
}

CHANGE_SCHEMA = {
    "type": "object",
    "description": "One change to the planner's inputs. Fields besides `op` depend on the op.",
    "properties": {
        "op": {
            "type": "string",
            "enum": ["create_task", "update_task", "delete_task", "log_progress", "set_event",
                     "add_title_rule", "delete_title_rule", "update_preferences", "add_hold", "delete_hold"],
            "description": (
                "create_task: TASK fields (title and hours or weekly_hours required). "
                "update_task: task_id + any TASK fields, or status (active/paused/done). "
                "delete_task: task_id. log_progress: task_id, minutes. "
                "set_event: event_id, skippable, skip_cost (points; one top-priority hour = 10). "
                "add_title_rule: pattern, skippable, skip_cost. delete_title_rule: rule_id. "
                "update_preferences: awake_start/awake_end (HH:MM), max_focus_hours, buffer_minutes, horizon_days. "
                "add_hold: title, start, end (time set aside, e.g. a break; goes on the Optimizer calendar when approved). delete_hold: hold_id."
            ),
        },
        "task_id": {"type": "integer"},
        "status": {"type": "string", "enum": ["active", "paused", "done"]},
        "minutes": {"type": "integer"},
        "event_id": {"type": "string"},
        "skippable": {"type": "boolean"},
        "skip_cost": {"type": "number"},
        "pattern": {"type": "string"},
        "rule_id": {"type": "integer"},
        "hold_id": {"type": "integer"},
        "start": {"type": "string"},
        "end": {"type": "string"},
        "awake_start": {"type": "string"},
        "awake_end": {"type": "string"},
        "max_focus_hours": {"type": "number"},
        "buffer_minutes": {"type": "integer"},
        "horizon_days": {"type": "integer"},
        **{k: v for k, v in TASK_FIELDS.items()},
    },
    "required": ["op"],
}


# ---- parsing ---------------------------------------------------------------------------


def parse_dt(value: str, tz, end_of_day: bool = False) -> datetime:
    """ISO date or datetime → aware datetime. Naive values are local; a bare date is
    the start of that day, or (with end_of_day) the following midnight."""
    value = value.strip()
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    if end_of_day and len(value) == 10:
        dt += timedelta(days=1)
    return dt


def parse_time(value: str) -> time:
    return time.fromisoformat(value.strip())


def _task_fields(change: dict, tz) -> dict:
    out = {}
    for k in ("title", "priority", "min_block", "ideal_block", "max_block", "time_of_day", "notes"):
        if change.get(k) is not None:
            out[k] = change[k]
    if change.get("hours") is not None:
        out["total_minutes"] = round(float(change["hours"]) * 60)
        out["effort_mode"] = EffortMode.TOTAL
    if change.get("weekly_hours") is not None:
        out["weekly_minutes"] = round(float(change["weekly_hours"]) * 60)
        out["effort_mode"] = EffortMode.WEEKLY
    if "deadline" in change:
        out["deadline"] = parse_dt(change["deadline"], tz, end_of_day=True) if change["deadline"] else None
    if "earliest_start" in change:
        out["earliest_start"] = parse_dt(change["earliest_start"], tz) if change["earliest_start"] else None
    return out


def _task(s: Session, task_id) -> Task:
    task = s.get(Task, int(task_id))
    if task is None:
        raise KeyError(f"no task with id {task_id}")
    return task


def apply_change(s: Session, change: dict, now: datetime) -> str:
    """Apply one change for real. Returns a one-line description of what changed."""
    tz = store.local_tz(store.get_prefs(s))
    op = change.get("op")
    if op == "create_task":
        fields = _task_fields(change, tz)
        if not fields.get("title"):
            raise ValueError("create_task needs a title")
        t = store.add_task(s, **fields)
        return f"Added task “{t.title}” (id {t.id})"
    if op == "update_task":
        task = _task(s, change.get("task_id"))
        fields = _task_fields(change, tz)
        if change.get("status"):
            fields["status"] = TaskStatus(change["status"])
        if not fields:
            raise ValueError("update_task needs at least one field to change")
        store.update_task(s, task.id, **fields)
        return f"Updated “{task.title}”: {', '.join(sorted(fields))}"
    if op == "delete_task":
        task = _task(s, change.get("task_id"))
        store.delete_task(s, task.id)
        return f"Deleted task “{task.title}”"
    if op == "log_progress":
        task = _task(s, change.get("task_id"))
        minutes = int(change.get("minutes") or 0)
        store.log_progress(s, task.id, minutes, now - timedelta(minutes=minutes))
        return f"Logged {minutes} min on “{task.title}”"
    if op == "set_event":
        event_id = change.get("event_id")
        if not event_id:
            raise ValueError("set_event needs event_id")
        skippable = bool(change.get("skippable"))
        cost = float(change.get("skip_cost", 4.0))
        if cost < 0:
            raise ValueError("skip_cost can't be negative")
        store.set_event_meta(s, event_id, skippable, cost)
        return f"Event {event_id}: " + (f"skippable, cost {cost:g}" if skippable else "fixed")
    if op == "add_title_rule":
        rule = store.add_title_rule(s, change.get("pattern", ""), bool(change.get("skippable", True)),
                                    float(change.get("skip_cost", 4.0)))
        return f"Rule: titles containing “{rule.pattern}” are " + (
            f"skippable, cost {rule.skip_cost:g}" if rule.skippable else "fixed")
    if op == "delete_title_rule":
        store.delete_title_rule(s, int(change.get("rule_id")))
        return f"Deleted title rule {change.get('rule_id')}"
    if op == "update_preferences":
        fields = {}
        for k in ("awake_start", "awake_end"):
            if change.get(k):
                fields[k] = parse_time(change[k])
        if change.get("max_focus_hours") is not None:
            fields["max_focus_minutes_per_day"] = round(float(change["max_focus_hours"]) * 60)
        for k in ("buffer_minutes", "horizon_days"):
            if change.get(k) is not None:
                fields[k] = int(change[k])
        if not fields:
            raise ValueError("update_preferences needs at least one field to change")
        store.update_prefs(s, **fields)
        return f"Preferences: {', '.join(sorted(fields))}"
    if op == "add_hold":
        start, end = parse_dt(change.get("start", ""), tz), parse_dt(change.get("end", ""), tz)
        hold = store.add_hold(s, change.get("title") or "Hold", start, end)
        st, en = hold.start.astimezone(tz), hold.end.astimezone(tz)
        return f"Hold “{hold.title}” {st:%a %H:%M}–{en:%H:%M}"
    if op == "delete_hold":
        store.delete_hold(s, int(change.get("hold_id")))
        return f"Removed hold {change.get('hold_id')}"
    raise ValueError(f"unknown op {op!r}")


# ---- previews on a scratch copy -------------------------------------------------------


@contextmanager
def scratch_session(s: Session) -> Iterator[Session]:
    """A session on a throwaway copy of the database file."""
    src_path = s.get_bind().url.database
    with tempfile.TemporaryDirectory() as d:
        dst_path = Path(d) / "preview.db"
        src = sqlite3.connect(src_path)
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        eng = db.make_engine(dst_path)
        try:
            with Session(eng, expire_on_commit=False) as copy:
                yield copy
        finally:
            eng.dispose()


def plan_summary(s: Session, plan: Plan, now: datetime, max_blocks: int = 40) -> dict:
    """A compact, JSON-friendly view of a plan for Claude and the review card."""
    tz = store.local_tz(store.get_prefs(s))
    titles = {str(t.id): t.title for t in store.list_tasks(s, include_done=True)}

    def title(task_id: str) -> str:
        return titles.get(task_id.split("@", 1)[0], task_id)

    return {
        "status": plan.status,
        "unmet": {title(k): v for k, v in plan.unmet_minutes.items()},
        "skipped_events": [f"{e.title} ({e.start.astimezone(tz):%a %H:%M}, cost {e.skip_cost:g})"
                           for e in plan.skipped],
        "planned_minutes": sum(b.minutes for b in plan.blocks),
        "blocks": [{"task": b.title, "start": b.start.astimezone(tz).isoformat(timespec="minutes"),
                    "end": b.end.astimezone(tz).isoformat(timespec="minutes"), "minutes": b.minutes}
                   for b in plan.blocks[:max_blocks]],
    }


def preview(s: Session, changes: list[dict], busy_events: list[EventInput], now: datetime) -> dict:
    """Apply `changes` to a scratch copy and compare plans. Raises ValueError/KeyError on a bad change."""
    if not changes:
        raise ValueError("no changes given")
    with scratch_session(s) as copy:
        before = store.plan_now(copy, busy_events, now, explain=False)
        before_summary = plan_summary(copy, before, now)
        descriptions = []
        for i, change in enumerate(changes, 1):
            try:
                descriptions.append(apply_change(copy, change, now))
            except (ValueError, KeyError) as e:
                msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
                raise ValueError(f"change {i} ({change.get('op')}): {msg}") from e
        after = store.plan_now(copy, busy_events, now, explain=False)
        after_summary = plan_summary(copy, after, now)
    before_spans = {(b["task"], b["start"], b["end"]) for b in before_summary["blocks"]}
    after_spans = {(b["task"], b["start"], b["end"]) for b in after_summary["blocks"]}
    return {
        "changes": descriptions,
        "before": {k: before_summary[k] for k in ("unmet", "skipped_events", "planned_minutes")},
        "after": after_summary,
        "blocks_changed": len(before_spans ^ after_spans),
    }


# ---- proposals -----------------------------------------------------------------------


def propose(s: Session, changes: list[dict], summary: str, busy_events: list[EventInput],
            now: datetime, source: str = "claude") -> Proposal:
    prev = preview(s, changes, busy_events, now)
    row = Proposal(source=source, summary=summary.strip() or "; ".join(prev["changes"]),
                   changes=changes, preview=prev)
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def pending(s: Session) -> list[Proposal]:
    from sqlmodel import select

    q = select(Proposal).where(Proposal.status == ProposalStatus.PENDING).order_by(Proposal.id)
    return list(s.exec(q))


def _pending_proposal(s: Session, proposal_id: int) -> Proposal:
    row = s.get(Proposal, int(proposal_id))
    if row is None:
        raise KeyError(f"no proposal with id {proposal_id}")
    if row.status != ProposalStatus.PENDING:
        raise ValueError(f"proposal {proposal_id} was already {row.status.value}")
    return row


def apply_proposal(s: Session, proposal_id: int, busy_events: list[EventInput], now: datetime,
                   source: str = "app") -> tuple[list[str], Plan]:
    """Apply a pending proposal for real, then re-plan. Returns (what changed, new plan)."""
    row = _pending_proposal(s, proposal_id)
    with scratch_session(s) as copy:  # validate everything first, so a bad change applies nothing
        for change in row.changes:
            apply_change(copy, change, now)
    done = [apply_change(s, change, now) for change in row.changes]
    row.status, row.decided_at = ProposalStatus.APPLIED, datetime.now(timezone.utc)
    s.add(row)
    s.commit()
    plan = store.plan_now(s, busy_events, now, source=source)
    return done, plan


def dismiss_proposal(s: Session, proposal_id: int) -> None:
    row = _pending_proposal(s, proposal_id)
    row.status, row.decided_at = ProposalStatus.DISMISSED, datetime.now(timezone.utc)
    s.add(row)
    s.commit()
