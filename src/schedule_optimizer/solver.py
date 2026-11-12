"""Place task blocks around calendar events with OR-Tools CP-SAT.

Model summary (see docs/PLAN.md, "Optimizer model in CP-SAT"):
- Each task gets up to K optional chunks: a presence flag, a start, and a length
  between min_block and max_block. Each skippable event gets an optional interval
  whose presence means "attend". Fixed events are mandatory intervals.
- AddNoOverlap over everything, each interval padded at its end by the buffer, so
  any two consecutive items are at least one buffer apart.
- Each chunk is assigned to exactly one day and must fit inside that day's awake
  window. The day assignment also feeds the daily focus cap.
- Objective (maximize), in points: task value at points per hour, minus skip costs,
  minus a fixed cost per chunk, minus deviation from ideal_block, plus a
  time-of-day bonus, plus a small "earlier is better" nudge for deadline tasks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ortools.sat.python import cp_model

from .domain import (
    TIME_OF_DAY_WINDOWS,
    EventDecision,
    EventInput,
    Plan,
    PlanBlock,
    Preferences,
    TaskInput,
    TimeOfDay,
    WeeklyTask,
)
from .timegrid import SLOT_MINUTES, TimeGrid, minutes_to_slots
from .weekly import expand_weekly

# Objective units per point. One slot of a w-points-per-hour task is w/12 points,
# so SCALE must be divisible by 12 to keep every term an integer.
SCALE = 1200
SLOTS_PER_HOUR = 60 // SLOT_MINUTES
# Urgency nudge: units lost per slot a deadline-task chunk starts later. Over a full
# week this is under 2 points, so it breaks ties without overriding real values.
EARLINESS_UNITS_PER_SLOT = 1
MAX_CHUNKS_PER_TASK = 40


def _units_per_slot(points_per_hour: float) -> int:
    return round(points_per_hour * SCALE / SLOTS_PER_HOUR)


@dataclass
class _Chunk:
    task: TaskInput
    present: cp_model.IntVar
    start: cp_model.IntVar
    length: cp_model.IntVar
    placed_len: cp_model.LinearExpr  # length if present, else 0
    deviation: cp_model.IntVar
    tod_len: cp_model.IntVar | None = None
    stay: list = field(default_factory=list)  # overlap with previously approved blocks


@dataclass
class _Model:
    model: cp_model.CpModel
    grid: TimeGrid
    chunks: list[_Chunk] = field(default_factory=list)
    attend: dict[str, cp_model.IntVar] = field(default_factory=dict)
    remaining_slots: dict[str, int] = field(default_factory=dict)


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def build_model(
    tasks: list[TaskInput],
    events: list[EventInput],
    prefs: Preferences,
    now: datetime,
    previous: dict[str, list[tuple[datetime, datetime]]] | None = None,
) -> _Model:
    """`previous` maps a task id (weekly instances share their base id) to the blocks
    the last approved plan gave it. Overlapping them earns a small stability bonus, so
    a re-plan only moves blocks when that gains more than the bonus."""
    grid = TimeGrid.starting(now, prefs.horizon_days)
    m = cp_model.CpModel()
    out = _Model(model=m, grid=grid)
    buf = minutes_to_slots(prefs.buffer_minutes)
    cap = prefs.max_focus_minutes_per_day // SLOT_MINUTES

    dates = grid.local_dates()
    awake = {d: grid.local_window(d, prefs.awake_start, prefs.awake_end) for d in dates}
    awake = {d: w for d, w in awake.items() if w}

    intervals: list[cp_model.IntervalVar] = []
    objective: list = []

    # Events. Overlapping fixed events are merged first so that a double-booked
    # calendar doesn't make NoOverlap infeasible.
    def span(e: EventInput) -> tuple[int, int] | None:
        lo, hi = grid.clamp(grid.to_slot_floor(e.start)), grid.clamp(grid.to_slot_ceil(e.end))
        return (lo, hi) if lo < hi else None

    fixed = [s for e in events if not e.skippable and (s := span(e))]
    for lo, hi in _merge(fixed):
        intervals.append(m.new_fixed_size_interval_var(lo, hi - lo + buf, "fixed"))

    for e in events:
        if not e.skippable or not (s := span(e)):
            continue
        lo, hi = s
        attend = m.new_bool_var(f"attend[{e.id}]")
        out.attend[e.id] = attend
        intervals.append(m.new_optional_fixed_size_interval_var(lo, hi - lo + buf, attend, f"ev[{e.id}]"))
        cost = round(e.skip_cost * SCALE)
        objective.append(cost * attend - cost)  # lose skip_cost points if not attended

    day_loads: dict = {d: [] for d in awake}
    f_units = round(prefs.chunk_cost * SCALE)
    a_units = _units_per_slot(prefs.ideal_deviation_per_hour)
    b_units = _units_per_slot(prefs.time_of_day_bonus_per_hour)
    stay_units = _units_per_slot(prefs.stability_per_hour)

    for task in tasks:
        rem = minutes_to_slots(task.remaining_minutes)
        out.remaining_slots[task.id] = max(rem, 0)
        if rem <= 0:
            continue
        lo_b = grid.clamp(grid.to_slot_ceil(task.earliest_start)) if task.earliest_start else 0
        hi_b = grid.clamp(grid.to_slot_floor(task.deadline)) if task.deadline else grid.num_slots
        days = {d: w for d, w in awake.items() if w[0] < hi_b and w[1] > lo_b}
        if lo_b >= hi_b or not days:
            continue

        min_len = min(minutes_to_slots(task.min_block), rem)
        max_len = max(min_len, min(minutes_to_slots(task.max_block), rem))
        ideal = max(min_len, min(minutes_to_slots(task.ideal_block), max_len))
        k_max = min(-(-rem // min_len), MAX_CHUNKS_PER_TASK)

        tod_windows = {}
        if task.time_of_day != TimeOfDay.ANY:
            t_start, t_end = TIME_OF_DAY_WINDOWS[task.time_of_day]
            tod_windows = {d: grid.local_window(d, t_start, t_end) for d in days}

        w_units = _units_per_slot(task.points_per_hour)
        prev_spans = []
        for ps, pe in (previous or {}).get(task.id.split("@", 1)[0], []):
            plo, phi = max(grid.clamp(grid.to_slot_floor(ps)), lo_b), min(grid.clamp(grid.to_slot_ceil(pe)), hi_b)
            if plo < phi:
                prev_spans.append((plo, phi))
        task_chunks: list[_Chunk] = []
        for k in range(k_max):
            name = f"{task.id}#{k}"
            p = m.new_bool_var(f"p[{name}]")
            s = m.new_int_var(lo_b, hi_b, f"s[{name}]")
            length = m.new_int_var(min_len, max_len, f"len[{name}]")
            m.add(s + length <= hi_b).only_enforce_if(p)
            # Pin absent chunks so the solver doesn't explore meaningless values.
            m.add(s == lo_b).only_enforce_if(~p)
            m.add(length == min_len).only_enforce_if(~p)
            # The interval links start + size == end when present; end must be its own var.
            padded_end = m.new_int_var(lo_b, hi_b + buf, f"e[{name}]")
            intervals.append(m.new_optional_interval_var(s, length + buf, padded_end, p, f"iv[{name}]"))

            # Pick exactly one day (if present) and stay inside its awake window.
            on_day = {}
            day_lens = []
            for d, (dlo, dhi) in days.items():
                x = m.new_bool_var(f"day[{name},{d}]")
                on_day[d] = x
                m.add(s >= dlo).only_enforce_if(x)
                m.add(s + length <= dhi).only_enforce_if(x)
                day_len = m.new_int_var(0, max_len, f"dlen[{name},{d}]")
                m.add(day_len == length).only_enforce_if(x)
                m.add(day_len == 0).only_enforce_if(~x)
                day_loads[d].append(day_len)
                day_lens.append(day_len)
            m.add(sum(on_day.values()) == p)
            placed = sum(day_lens)

            dev = m.new_int_var(0, max_len, f"dev[{name}]")
            m.add(dev >= length - ideal).only_enforce_if(p)
            m.add(dev >= ideal - length).only_enforce_if(p)

            chunk = _Chunk(task, p, s, length, placed, dev)
            if tod_windows:
                t = m.new_bool_var(f"tod[{name}]")
                m.add_implication(t, p)
                for d, x in on_day.items():
                    win = tod_windows.get(d)
                    if win:
                        m.add(s >= win[0]).only_enforce_if([x, t])
                        m.add(s + length <= win[1]).only_enforce_if([x, t])
                    else:
                        m.add_bool_or([~x, ~t])
                tod_len = m.new_int_var(0, max_len, f"todlen[{name}]")
                m.add(tod_len <= length)
                m.add(tod_len <= max_len * t)
                chunk.tod_len = tod_len
                objective.append(b_units * tod_len)

            objective.append(w_units * placed)
            objective.append(-a_units * dev)
            objective.append(-f_units * p)
            if task.deadline:
                objective.append(-EARLINESS_UNITS_PER_SLOT * s)

            # Stability: reward overlap with this task's previously approved blocks.
            # overlap = max(0, min(ends) − max(starts)); lo/hi are bounds the solver
            # pushes tight because it's maximizing, and `gap` may be negative.
            for plo, phi in prev_spans:
                n = grid.num_slots
                lo = m.new_int_var(0, n, "")
                hi = m.new_int_var(0, n, "")
                m.add(lo >= s)
                m.add(lo >= plo)
                m.add(hi <= s + length)
                m.add(hi <= phi)
                gap = m.new_int_var(-n, n, "")
                m.add(gap == hi - lo)
                pos = m.new_int_var(0, n, "")
                m.add_max_equality(pos, [gap, 0])
                ov = m.new_int_var(0, min(max_len, phi - plo), f"stay[{name}]")
                m.add(ov <= pos)
                m.add(ov <= max_len * p)
                objective.append(stay_units * ov)
                chunk.stay.append(ov)
            task_chunks.append(chunk)

        # Symmetry breaking: used chunks come first, in time order.
        for a, b in zip(task_chunks, task_chunks[1:]):
            m.add_implication(b.present, a.present)
            m.add(b.start >= a.start + a.length + buf).only_enforce_if(b.present)
        m.add(sum(c.placed_len for c in task_chunks) <= rem)
        out.chunks.extend(task_chunks)

    for loads in day_loads.values():
        if loads:
            m.add(sum(loads) <= cap)
    m.add_no_overlap(intervals)
    m.maximize(sum(objective))
    return out


def solve(
    tasks: list[TaskInput],
    events: list[EventInput],
    prefs: Preferences,
    now: datetime,
    weekly: list[WeeklyTask] | None = None,
    num_workers: int = 8,
    previous: dict[str, list[tuple[datetime, datetime]]] | None = None,
) -> Plan:
    grid = TimeGrid.starting(now, prefs.horizon_days)
    all_tasks = list(tasks) + [t for w in weekly or [] for t in expand_weekly(w, grid)]
    built = build_model(all_tasks, events, prefs, now, previous)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = prefs.solve_seconds
    solver.parameters.num_workers = num_workers
    status = solver.solve(built.model)
    status_name = solver.status_name(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Plan(status_name, 0.0, [], [], dict(built.remaining_slots), solver.wall_time)

    blocks: list[PlanBlock] = []
    placed: dict[str, int] = {t.id: 0 for t in all_tasks}
    for c in built.chunks:
        if not solver.value(c.present):
            continue
        s, length = solver.value(c.start), solver.value(c.length)
        placed[c.task.id] += length
        hours = length / SLOTS_PER_HOUR
        score = {
            "value": c.task.points_per_hour * hours,
            "block_cost": -prefs.chunk_cost,
            "ideal_deviation": -prefs.ideal_deviation_per_hour * solver.value(c.deviation) / SLOTS_PER_HOUR,
        }
        if c.stay:
            score["stability_bonus"] = prefs.stability_per_hour * sum(solver.value(v) for v in c.stay) / SLOTS_PER_HOUR
        if c.tod_len is not None:
            score["time_of_day_bonus"] = prefs.time_of_day_bonus_per_hour * solver.value(c.tod_len) / SLOTS_PER_HOUR
        blocks.append(PlanBlock(c.task.id, c.task.title, grid.to_datetime(s), grid.to_datetime(s + length), score))
    blocks.sort(key=lambda b: b.start)

    decisions = [
        EventDecision(e.id, e.title, e.start, e.end, bool(solver.value(built.attend[e.id])), e.skip_cost)
        for e in events
        if e.id in built.attend
    ]
    unmet = {
        t.id: min(t.remaining_minutes, (built.remaining_slots.get(t.id, 0) - placed[t.id]) * SLOT_MINUTES)
        for t in all_tasks
    }
    return Plan(
        status=status_name,
        objective_points=solver.objective_value / SCALE,
        blocks=blocks,
        events=decisions,
        unmet_minutes={k: v for k, v in unmet.items() if v > 0},
        wall_seconds=solver.wall_time,
    )
