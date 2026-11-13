"""Explain unmet work: which rules conflict, found with CP-SAT assumption literals.

The normal model never fails (blocks are optional, so it just leaves work unmet).
To explain a shortfall, we build a second model that *requires* the task's full
remaining work, with each relaxable rule (deadline, awake hours, a day's focus cap,
an event, the shortest block) behind an on/off literal. Solving with every literal
assumed true is infeasible, and `sufficient_assumptions_for_infeasibility()` names a
set of literals responsible. We then shrink that set by trying to drop each one, so
what's reported is minimal: relaxing any single rule in it would help.
"""

from __future__ import annotations

import time as _time
from datetime import datetime

from ortools.sat.python import cp_model

from .domain import Conflict, EventInput, Plan, Preferences, TaskInput, WeeklyTask
from .solver import _Diag, build_model
from .timegrid import TimeGrid
from .weekly import expand_weekly

MAX_TASKS = 3  # diagnose at most this many unmet tasks per plan
SHRINK_ORDER = ["awake", "min_block", "focus_cap", "earliest_start", "event", "attend", "deadline"]


def _solver(seconds: float, workers: int) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(seconds, 0.05)
    solver.parameters.num_workers = workers
    return solver


def _core(model: cp_model.CpModel, lits: list, seconds: float) -> tuple[int, list[int]]:
    """Solve requiring everything; on infeasibility return the responsible assumptions."""
    model.clear_assumptions()
    model.add_assumptions(lits)
    solver = _solver(seconds, workers=1)  # a single worker reports the core reliably
    status = solver.solve(model)
    core = solver.sufficient_assumptions_for_infeasibility() if status == cp_model.INFEASIBLE else []
    return status, list(core)


def _fits(model: cp_model.CpModel, lits: list, need_slots: int, seconds: float) -> bool | None:
    """With only `lits` enforced, can all the work be placed? None if undecided in time."""
    model.clear_assumptions()
    model.add_assumptions(lits)
    solver = _solver(seconds, workers=8)
    status = solver.solve(model)
    if status == cp_model.INFEASIBLE:
        return False
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        if solver.objective_value >= need_slots:
            return True
        if solver.best_objective_bound < need_slots:
            return False
    return None


def diagnose_task(
    task: TaskInput,
    events: list[EventInput],
    prefs: Preferences,
    now: datetime,
    plan: Plan,
    seconds: float = 2.0,
) -> Conflict:
    deadline_at = _time.monotonic() + seconds
    unmet = plan.unmet_minutes.get(task.id, 0)
    if task.remaining_minutes > prefs.horizon_days * 24 * 60:
        return Conflict(task.id, task.title, unmet, "too_big")  # more hours than the window has
    attended = {e.event_id for e in plan.events if e.attend}
    diag = _Diag(required=task.id, attended=attended)
    built = build_model([task], events, prefs, now, diag=diag)
    model = built.model
    by_index = {lit.index: key for key, (lit, _) in diag.lits.items()}
    all_lits = [lit for lit, _ in diag.lits.values()]

    def left() -> float:
        return deadline_at - _time.monotonic()

    status, core = _core(model, all_lits + [diag.need], left())
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        competing: dict[str, int] = {}
        for b in plan.blocks:
            if b.task_id != task.id and (task.deadline is None or b.start < task.deadline):
                competing[b.title] = competing.get(b.title, 0) + b.minutes
        return Conflict(task.id, task.title, unmet, "competition", competing=competing)
    if status != cp_model.INFEASIBLE:
        return Conflict(task.id, task.title, unmet, "unknown")
    if not core or _fits(model, [], diag.required_slots, min(left(), 1.0)) is False:
        return Conflict(task.id, task.title, unmet, "too_big")  # doesn't fit even with every rule off

    # Shrink: drop each literal in turn; keep the drop if it's still infeasible. Rules
    # that are least useful to change are tried first, so the set that remains leans
    # toward actionable ones (deadlines, events) over awake hours or block sizes.
    keep = [diag.lits[by_index[i]][0] for i in core if i in by_index]  # drops `need` itself
    keep.sort(key=lambda lit: SHRINK_ORDER.index(diag.lits[by_index[lit.index]][1].kind))
    i = 0
    while i < len(keep) and left() > 0:
        trial = keep[:i] + keep[i + 1:]
        if _fits(model, trial, diag.required_slots, min(left(), 0.5)) is False:
            keep = trial  # still impossible without this rule, so it isn't needed in the set
        else:
            i += 1  # needed (or undecided: keep it, so the set stays a true conflict)
    rules = [diag.lits[by_index[lit.index]][1] for lit in keep]
    if not rules:
        return Conflict(task.id, task.title, unmet, "too_big")
    order = ["deadline", "earliest_start", "attend", "event", "focus_cap", "awake", "min_block"]
    rules.sort(key=lambda r: (order.index(r.kind), r.day or "", r.label))
    return Conflict(task.id, task.title, unmet, "rules", rules=rules)


def diagnose(
    plan: Plan,
    tasks: list[TaskInput],
    events: list[EventInput],
    prefs: Preferences,
    now: datetime,
    weekly: list[WeeklyTask] | None = None,
    seconds: float = 4.0,
) -> list[Conflict]:
    """Explain each unmet task that has a deadline (others just continue after the horizon)."""
    grid = TimeGrid.starting(now, prefs.horizon_days)
    all_tasks = {t.id: t for t in list(tasks) + [t for w in weekly or [] for t in expand_weekly(w, grid)]}
    unmet = [all_tasks[i] for i in plan.unmet_minutes if i in all_tasks and all_tasks[i].deadline]
    unmet.sort(key=lambda t: t.deadline)
    unmet = unmet[:MAX_TASKS]
    out = []
    for n, task in enumerate(unmet):
        out.append(diagnose_task(task, events, prefs, now, plan, seconds=seconds / len(unmet)))
    return out
