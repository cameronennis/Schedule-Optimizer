from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import pytest

from schedule_optimizer.demo import NOW, at, scenario
from schedule_optimizer.domain import EventInput, Plan, Preferences, TaskInput, TimeOfDay, WeeklyTask
from schedule_optimizer.solver import solve
from schedule_optimizer.timegrid import TimeGrid, ceil_to_slot
from schedule_optimizer.weekly import expand_weekly

TZ = ZoneInfo("America/Los_Angeles")


def check_invariants(plan: Plan, tasks, events, prefs: Preferences):
    """Hard rules every plan must satisfy."""
    assert plan.status in ("OPTIMAL", "FEASIBLE")
    buf = timedelta(minutes=prefs.buffer_minutes)
    by_id = {t.id: t for t in tasks}
    skipped = {e.event_id for e in plan.skipped}

    others = [(e.start, e.end) for e in events if e.id not in skipped]
    spans = [(b.start, b.end) for b in plan.blocks]
    for i, (s1, e1) in enumerate(spans):
        for j, (s2, e2) in enumerate(spans + others):
            if i != j:
                assert e1 + buf <= s2 or e2 + buf <= s1, f"overlap or missing buffer: {s1}-{e1} vs {s2}-{e2}"

    by_task: dict = {}
    for b in plan.blocks:
        by_task.setdefault(b.task_id, []).append(b)
    attended = [(e.start, e.end) for e in events if e.id not in skipped]
    for blocks in by_task.values():
        for a, b in zip(blocks, blocks[1:]):
            event_between = any(a.end <= es and ee <= b.start for es, ee in attended)
            assert event_between or b.start - a.end >= timedelta(minutes=30), \
                f"{a.title} back-to-back at {a.end:%H:%M} with no event between"

    per_day: dict = {}
    for b in plan.blocks:
        assert b.start >= ceil_to_slot(NOW)
        local_s, local_e = b.start.astimezone(TZ), b.end.astimezone(TZ)
        assert local_s.date() == (local_e - timedelta(seconds=1)).date(), "block crosses midnight"
        assert local_s.time() >= prefs.awake_start
        assert local_e.time() <= prefs.awake_end or local_e.time() == time(0)
        task = by_id.get(b.task_id)
        if task is not None:
            assert min(task.min_block, task.remaining_minutes) <= b.minutes  # max_block is soft
            if task.deadline:
                assert b.end <= task.deadline
            if task.earliest_start:
                assert b.start >= task.earliest_start
        per_day[local_s.date()] = per_day.get(local_s.date(), 0) + b.minutes
    for minutes in per_day.values():
        assert minutes <= prefs.max_focus_minutes_per_day


@pytest.mark.parametrize("priority, attends", [(3, True), (1, False)])
def test_midterm_checkpoint(priority, attends):
    tasks, events, prefs, weekly = scenario(midterm_priority=priority)
    plan = solve(tasks, events, prefs, NOW, weekly=weekly)
    check_invariants(plan, tasks, events, prefs)
    lecture = next(e for e in plan.events if e.event_id == "lecture")
    assert lecture.attend is attends


def test_plenty_of_time_keeps_lecture_even_at_top_priority():
    tasks, events, prefs, weekly = scenario(midterm_priority=1)
    tasks[0].remaining_minutes = 60
    plan = solve(tasks, events, prefs, NOW, weekly=weekly)
    check_invariants(plan, tasks, events, prefs)
    assert all(e.attend for e in plan.events)
    assert "midterm" not in plan.unmet_minutes


def test_double_booked_fixed_events_are_still_feasible():
    events = [
        EventInput("a", "Meeting A", at(18, 10), at(18, 11)),
        EventInput("b", "Meeting B", at(18, 10, 30), at(18, 12)),
    ]
    tasks = [TaskInput("t", "Work", remaining_minutes=120)]
    prefs = Preferences()
    plan = solve(tasks, events, prefs, NOW)
    check_invariants(plan, tasks, events, prefs)
    for b in plan.blocks:
        assert b.end + timedelta(minutes=5) <= at(18, 10) or b.start >= at(18, 12, 5)


def test_daily_cap_and_time_of_day():
    tasks = [TaskInput("big", "Thesis", remaining_minutes=20 * 60, time_of_day=TimeOfDay.MORNING)]
    prefs = Preferences(max_focus_minutes_per_day=180, horizon_days=3)
    plan = solve(tasks, [], prefs, NOW)
    check_invariants(plan, tasks, [], prefs)
    assert plan.unmet_minutes["big"] > 0
    assert all(b.end.astimezone(TZ).time() <= time(12) for b in plan.blocks)


def test_one_long_block_beats_many_short_ones():
    tasks = [TaskInput("t", "Essay", remaining_minutes=120)]
    plan = solve(tasks, [], Preferences(horizon_days=1), NOW)
    assert [b.minutes for b in plan.blocks] == [120]


def test_weekly_expands_into_two_weeks():
    grid = TimeGrid.starting(NOW, 7)  # Wed Nov 18 → Wed Nov 25
    parts = expand_weekly(WeeklyTask("r", "Reading", weekly_minutes=180, logged_this_week=60), grid)
    assert [p.remaining_minutes for p in parts] == [120, 180]
    assert parts[0].deadline == datetime(2026, 11, 23, tzinfo=TZ)
    assert parts[1].earliest_start == datetime(2026, 11, 23, tzinfo=TZ)


def test_slots_count_real_time_across_dst():
    # US DST ends Sunday Nov 1, 2026: that local day has 25 hours.
    grid = TimeGrid.starting(datetime(2026, 10, 31, 8, tzinfo=TZ), 7)
    assert grid.to_slot_floor(datetime(2026, 11, 1, 8, tzinfo=TZ)) == 25 * 12
    assert grid.to_datetime(25 * 12) == datetime(2026, 11, 1, 8, tzinfo=TZ)


def test_deadline_beats_longest_block_limit():
    """Regression: a task short on time got 55 + 90 min blocks 5 min apart. It should get
    one longer block instead, since meeting the deadline matters more than max_block."""
    now = datetime(2026, 11, 12, 13, 20, tzinfo=TZ)
    deadline = datetime(2026, 11, 12, 16, tzinfo=TZ)
    tasks = [TaskInput("1", "ORALCOMM Paper", remaining_minutes=180, deadline=deadline)]
    prefs = Preferences(awake_start=time(9), awake_end=time(23, 55))
    plan = solve(tasks, [], prefs, now)
    [block] = plan.blocks
    assert block.end <= deadline and block.minutes == 160  # all the time there is
    assert block.score["over_max_block"] < 0
    assert plan.unmet_minutes["1"] == 20


def test_longest_block_still_holds_when_time_is_plentiful():
    tasks = [TaskInput("t", "Thesis", remaining_minutes=360, deadline=NOW + timedelta(days=3))]
    plan = solve(tasks, [], Preferences(), NOW)
    assert sum(b.minutes for b in plan.blocks) == 360
    assert all(b.minutes <= 120 for b in plan.blocks)


SHORT_DAY = Preferences(awake_start=time(8), awake_end=time(11), horizon_days=1)


def test_short_event_between_blocks_counts_as_the_break():
    """A 10-minute event splits the morning; blocks sit right around it (buffers only)."""
    standup = EventInput("standup", "Standup", at(18, 9, 30), at(18, 9, 40))
    tasks = [TaskInput("t", "Report", remaining_minutes=165, deadline=at(18, 11))]
    plan = solve(tasks, [standup], SHORT_DAY, NOW)
    check_invariants(plan, tasks, [standup], SHORT_DAY)
    assert [(b.start, b.end) for b in plan.blocks] == [(at(18, 8), at(18, 9, 25)), (at(18, 9, 45), at(18, 11))]
    assert plan.unmet_minutes["t"] == 5


def test_short_event_never_overlapped_even_under_pressure():
    events = [EventInput(f"e{i}", "Check-in", at(18, 8 + i, 0), at(18, 8 + i, 15)) for i in range(1, 3)]
    tasks = [TaskInput("t", "Report", remaining_minutes=300, priority=1, deadline=at(18, 11))]
    plan = solve(tasks, events, SHORT_DAY, NOW)
    check_invariants(plan, tasks, events, SHORT_DAY)
    for b in plan.blocks:
        for e in events:
            assert b.end + timedelta(minutes=5) <= e.start or e.end + timedelta(minutes=5) <= b.start


def test_skipped_short_event_is_not_a_break():
    """If the short event is skipped, its time is free, so blocks around it merge."""
    cheap = EventInput("c", "Optional sync", at(18, 9, 30), at(18, 9, 40), skippable=True, skip_cost=0.1)
    tasks = [TaskInput("t", "Report", remaining_minutes=180, priority=1, deadline=at(18, 11))]
    plan = solve(tasks, [cheap], SHORT_DAY, NOW)
    check_invariants(plan, tasks, [cheap], SHORT_DAY)
    assert [e.attend for e in plan.events] == [False]
    assert [b.minutes for b in plan.blocks] == [180]


# ---- symmetry breaking and solve quality ------------------------------------------------

def test_identical_tasks_are_ordered_not_searched_twice():
    """Two interchangeable tasks: the first one listed starts first, and the plan is still optimal."""
    tasks = [TaskInput("a", "Reading A", remaining_minutes=120, deadline=at(19, 23)),
             TaskInput("b", "Reading B", remaining_minutes=120, deadline=at(19, 23))]
    prefs = Preferences(horizon_days=2)
    plan = solve(tasks, [], prefs, NOW)
    check_invariants(plan, tasks, [], prefs)
    assert plan.status == "OPTIMAL" and not plan.unmet_minutes
    first = {t: min(b.start for b in plan.blocks if b.task_id == t) for t in ("a", "b")}
    assert first["a"] <= first["b"]


def test_replan_with_previous_blocks_reaches_optimal():
    """Regression: the stability bonus used helper variables with many equivalent values,
    so re-plans hit the 5 s limit without proving the best plan."""
    tasks, events, prefs, weekly = scenario()
    prefs.solve_seconds = 15  # proves optimal in ~4 s on a laptop; leave headroom for slow CI
    first = solve(tasks, events, prefs, NOW, weekly=weekly)
    previous = {}
    for b in first.blocks:
        previous.setdefault(b.task_id.split("@")[0], []).append((b.start, b.end))
    later = NOW + timedelta(minutes=50)
    replan = solve(tasks, events, prefs, later, weekly=weekly, previous=previous)
    assert replan.status == "OPTIMAL"
    # Blocks that were already in good places stay put.
    kept = sum(1 for b in replan.blocks if (b.start, b.end) in {s for v in previous.values() for s in v})
    assert kept >= len(replan.blocks) // 2
