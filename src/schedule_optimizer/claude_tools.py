"""The tools Claude can call, shared by the Claude Desktop helper and API-key mode.

Each tool is a JSON-schema spec plus a handler `(session, args, ctx) -> dict`.
Read tools only read. `propose_changes` writes a pending proposal (and nothing
else). `apply_changes` and `create_task` change real inputs; Claude Desktop asks
your permission before running them, and API-key mode doesn't offer them at all:
there you apply proposals with the button in the app.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session

from . import changes, store
from .calendar_sync import busy_events_if_connected
from .tables import EffortMode, PlanBlock, Task


INSTRUCTIONS = """\
Schedule Optimizer plans the user's tasks around their Google Calendar with a solver.
You never place blocks yourself: you change the solver's inputs (tasks, event skip
costs, preferences, holds such as breaks), the user approves, and it re-plans.

- Start with get_plan (and get_tasks) to see the current state. get_plan's "now" is
  the current local date and time.
- To change anything, call propose_changes. It previews the result without changing
  anything.
- All weights are points per hour: priority p is worth 2p points per hour of work;
  an event's skip cost is the points lost by skipping it (cost 4 ≈ 24 min of
  top-priority work).
- When work doesn't fit, get_plan lists the conflicting rules. Propose relaxing one
  of them, and say what it costs.
- When the user is tired or stressed, consider a break (add_hold) and lighter
  blocks, and suggest something restful and good for them, like a short walk.
- New plans are only proposed. The user writes them, with any breaks (holds), to
  their Optimizer calendar with "Approve plan" in the app window.
- Keep replies short. Times are local.
"""

DESKTOP_INSTRUCTIONS = """\
- After propose_changes, summarize the before/after for the user, and call
  apply_changes only after they agree.
"""

API_INSTRUCTIONS = """\
- You're chatting inside the Schedule Optimizer app. After propose_changes, the
  proposal appears in the app with Apply and Dismiss buttons; tell the user briefly
  what it does and that they can apply it. You can't apply it yourself.
"""


@dataclass
class Ctx:
    source: str  # "claude-desktop" or "api"
    now: Callable[[], datetime]


def fmt_local(dt: datetime, tz) -> str:
    return dt.astimezone(tz).isoformat(timespec="minutes")


def conflict_text(c: dict) -> str:
    """One readable paragraph for a saved Conflict (as stored in LastSolve.conflicts)."""
    head = f"{c['title']}: {c['unmet_minutes']} min don't fit."
    if c["kind"] == "rules":
        if len(c["rules"]) == 1:
            return f"{head} Relaxing this one rule would make room: {c['rules'][0]['label']}."
        rules = "; ".join(r["label"] for r in c["rules"])
        return f"{head} These rules can't all hold, so at least one has to give: {rules}."
    if c["kind"] == "competition":
        others = ", ".join(f"{t} ({m} min)" for t, m in c["competing"].items())
        return f"{head} It would fit on its own; other tasks use the time first: {others}."
    if c["kind"] == "too_big":
        return f"{head} That's more than the whole planning window holds, even with rules relaxed."
    return f"{head} (The diagnosis ran out of time.)"


# ---- read tools ------------------------------------------------------------------------


def _task_dict(s: Session, t: Task, now: datetime, tz) -> dict:
    d = {
        "id": t.id, "title": t.title, "priority": t.priority, "status": t.status.value,
        "kind": "weekly" if t.effort_mode == EffortMode.WEEKLY else "total",
        "remaining_minutes": store.remaining_minutes(s, t, now, tz),
        "min_block": t.min_block, "ideal_block": t.ideal_block, "max_block": t.max_block,
        "time_of_day": t.time_of_day.value,
    }
    if t.effort_mode == EffortMode.WEEKLY:
        d["weekly_minutes"] = t.weekly_minutes
    else:
        d["total_minutes"] = t.total_minutes
    if t.deadline:
        d["deadline"] = fmt_local(t.deadline, tz)
    if t.earliest_start:
        d["earliest_start"] = fmt_local(t.earliest_start, tz)
    if t.notes:
        d["notes"] = t.notes
    return d


def get_tasks(s: Session, args: dict, ctx: Ctx) -> dict:
    now = ctx.now()
    tz = store.local_tz(store.get_prefs(s))
    return {"tasks": [_task_dict(s, t, now, tz) for t in store.list_tasks(s, include_done=bool(args.get("include_done")))]}


def get_plan(s: Session, args: dict, ctx: Ctx) -> dict:
    now = ctx.now()
    prefs = store.get_prefs(s)
    tz = store.local_tz(prefs)
    blocks = store.upcoming_blocks(s, now)
    titles = {t.id: t.title for t in store.list_tasks(s, include_done=True)}
    row = store.last_solve(s)
    out = {
        "now": fmt_local(now, tz),
        "plan_state": "proposed (not approved yet)" if store.proposed_blocks(s) else (
            "approved" if blocks else "empty"),
        "blocks": [{"block_id": b.id, "task_id": b.task_id, "task": titles.get(b.task_id, "?"),
                    "start": fmt_local(b.start, tz), "end": fmt_local(b.end, tz),
                    "minutes": int((b.end - b.start).total_seconds() // 60)} for b in blocks],
        "preferences": {"awake": f"{prefs.awake_start:%H:%M}–{prefs.awake_end:%H:%M}",
                        "max_focus_hours_per_day": prefs.max_focus_minutes_per_day / 60,
                        "buffer_minutes": prefs.buffer_minutes, "horizon_days": prefs.horizon_days},
        "holds": [{"hold_id": h.id, "title": h.title, "start": fmt_local(h.start, tz), "end": fmt_local(h.end, tz)}
                  for h in store.list_holds(s, now)],
        "pending_proposals": [{"proposal_id": p.id, "summary": p.summary} for p in changes.pending(s)],
    }
    if row:
        out["last_solve"] = {
            "solved_at": fmt_local(row.solved_at, tz), "status": row.status, "by": row.source,
            "unmet_minutes": {titles.get(int(k.split("@")[0]), k): v for k, v in row.unmet.items()},
            "conflicts": [conflict_text(c) for c in row.conflicts],
        }
        out["events"] = [
            {"event_id": e["id"], "title": e["title"],
             "start": fmt_local(datetime.fromisoformat(e["start"]), tz),
             "end": fmt_local(datetime.fromisoformat(e["end"]), tz),
             **({"skippable": True, "skip_cost": e["skip_cost"], "attend": e["attend"]} if e["skippable"] else {})}
            for e in row.events if datetime.fromisoformat(e["end"]) > now
        ]
    return out


def explain_block(s: Session, args: dict, ctx: Ctx) -> dict:
    block = s.get(PlanBlock, int(args.get("block_id", 0)))
    if block is None:
        raise KeyError(f"no block with id {args.get('block_id')}")
    tz = store.local_tz(store.get_prefs(s))
    task = s.get(Task, block.task_id)
    minutes = int((block.end - block.start).total_seconds() // 60)
    names = {
        "value": f"value: {minutes / 60:g} h × {2 * task.priority} points/hour (priority {task.priority})",
        "block_cost": "fixed cost per block (prefers fewer, longer blocks)",
        "ideal_deviation": f"differs from the ideal block length ({task.ideal_block} min)",
        "time_of_day_bonus": f"inside the preferred time of day ({task.time_of_day.value})",
        "over_max_block": f"runs past the longest block ({task.max_block} min), allowed when time is short",
        "stability_bonus": "stays where the last approved plan put it",
    }
    terms = [{"term": names.get(k, k), "points": round(v, 2)} for k, v in (block.score or {}).items()]
    out = {
        "block": {"task": task.title, "start": fmt_local(block.start, tz), "end": fmt_local(block.end, tz),
                  "minutes": minutes, "status": block.status.value},
        "score_terms": terms,
        "total_points": round(sum((block.score or {}).values()), 2),
        "task": _task_dict(s, task, ctx.now(), tz),
    }
    row = store.last_solve(s)
    if row:
        skipped = [e for e in row.events if e["skippable"] and not e["attend"]
                   and datetime.fromisoformat(e["start"]) < block.end and datetime.fromisoformat(e["end"]) > block.start]
        if skipped:
            out["uses_time_from_skipped_events"] = [f"{e['title']} (skip cost {e['skip_cost']:g})" for e in skipped]
        mine = [conflict_text(c) for c in row.conflicts if c["task_id"].split("@")[0] == str(task.id)]
        if mine:
            out["conflicts"] = mine
    return out


# ---- tools that plan or change things ----------------------------------------------------


def _busy(s: Session, now: datetime) -> tuple[list, str | None]:
    return busy_events_if_connected(s, now)


def solve(s: Session, args: dict, ctx: Ctx) -> dict:
    now = ctx.now()
    events, note = _busy(s, now)
    plan = store.plan_now(s, events, now, source=ctx.source)
    out = changes.plan_summary(s, plan, now)
    row = store.last_solve(s)
    out["conflicts"] = [conflict_text(c) for c in row.conflicts] if row else []
    out["next_step"] = "This plan is proposed. Approve it in the Schedule Optimizer window to write it to the Optimizer calendar."
    if note:
        out["note"] = note
    return out


def propose_changes(s: Session, args: dict, ctx: Ctx) -> dict:
    now = ctx.now()
    events, note = _busy(s, now)
    row = changes.propose(s, list(args.get("changes") or []), args.get("summary", ""), events, now, ctx.source)
    out = {"proposal_id": row.id, "summary": row.summary, **row.preview}
    if ctx.source == "api":
        out["next_step"] = "Shown to the user in the app with Apply and Dismiss buttons."
    else:
        out["next_step"] = "Show the user what changes and the before/after, then call apply_changes if they agree."
    if note:
        out["note"] = note
    return out


def apply_changes(s: Session, args: dict, ctx: Ctx) -> dict:
    now = ctx.now()
    events, note = _busy(s, now)
    done, plan = changes.apply_proposal(s, int(args.get("proposal_id", 0)), events, now, source=ctx.source)
    out = {"applied": done, "new_plan": changes.plan_summary(s, plan, now),
           "next_step": "The re-planned schedule is proposed. Approve it in the Schedule Optimizer window."}
    if note:
        out["note"] = note
    return out


def create_task(s: Session, args: dict, ctx: Ctx) -> dict:
    desc = changes.apply_change(s, {**args, "op": "create_task"}, ctx.now())
    return {"created": desc, "next_step": "Call solve to re-plan with it."}


# ---- specs -------------------------------------------------------------------------------

_EMPTY = {"type": "object", "properties": {}}
TOOLS: dict[str, dict] = {
    "get_plan": {
        "description": "Current plan (proposed or approved blocks, with block ids), calendar events with "
                       "event ids and skip settings, unmet work and why it doesn't fit, holds, preferences, "
                       "and pending proposals. Times are local ISO.",
        "input_schema": _EMPTY, "handler": get_plan, "read_only": True,
    },
    "get_tasks": {
        "description": "All tasks with ids, remaining minutes, priority, deadline and block sizes.",
        "input_schema": {"type": "object", "properties": {"include_done": {"type": "boolean"}}},
        "handler": get_tasks, "read_only": True,
    },
    "explain_block": {
        "description": "Why a block is where it is: its score terms in points, the task's details, any "
                       "skipped events it uses the time of, and conflicts for the task.",
        "input_schema": {"type": "object", "properties": {"block_id": {"type": "integer"}}, "required": ["block_id"]},
        "handler": explain_block, "read_only": True,
    },
    "solve": {
        "description": "Re-plan from now with the current inputs. The result is a proposed plan; the user "
                       "approves it in the app before anything is written to their calendar.",
        "input_schema": _EMPTY, "handler": solve, "read_only": False,
    },
    "propose_changes": {
        "description": "Propose changes to the planner's inputs (never to blocks directly) and preview the "
                       "re-planned result on a scratch copy. Nothing real changes. Weights are points per hour: "
                       "priority p is worth 2p points per hour of work, and an event's skip cost is points lost "
                       "by skipping it. For 'I'm tired', a break is an add_hold.",
        "input_schema": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "One sentence the user will see."},
            "changes": {"type": "array", "items": changes.CHANGE_SCHEMA}}, "required": ["summary", "changes"]},
        "handler": propose_changes, "read_only": False,
    },
    "apply_changes": {
        "description": "Apply a pending proposal the user agreed to, then re-plan. The new plan still needs "
                       "Approve plan in the app.",
        "input_schema": {"type": "object", "properties": {"proposal_id": {"type": "integer"}}, "required": ["proposal_id"]},
        "handler": apply_changes, "read_only": False,
    },
    "create_task": {
        "description": "Add a task directly. Fill sensible defaults: priority 3, blocks 30/90/120 min.",
        "input_schema": {"type": "object", "properties": changes.TASK_FIELDS, "required": ["title"]},
        "handler": create_task, "read_only": False,
    },
}

API_TOOLS = ["get_plan", "get_tasks", "explain_block", "solve", "propose_changes"]


def run_tool(s: Session, name: str, args: dict, ctx: Ctx) -> dict:
    if name not in TOOLS:
        raise KeyError(f"unknown tool {name}")
    return TOOLS[name]["handler"](s, args or {}, ctx)
