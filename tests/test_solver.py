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

    per_day: dict = {}
    for b in plan.blocks:
        assert b.start >= ceil_to_slot(NOW)
        local_s, local_e = b.start.astimezone(TZ), b.end.astimezone(TZ)
        assert local_s.date() == (local_e - timedelta(seconds=1)).date(), "block crosses midnight"
        assert local_s.time() >= prefs.awake_start
        assert local_e.time() <= prefs.awake_end or local_e.time() == time(0)
        task = by_id.get(b.task_id)
        if task is not None:
            assert min(task.min_block, task.remaining_minutes) <= b.minutes <= task.max_block
            if task.deadline:
                assert b.end <= task.deadline
            if task.earliest_start:
                assert b.start >= task.earliest_start
        per_day[local_s.date()] = per_day.get(local_s.date(), 0) + b.minutes
    for minutes in per_day.values():
        assert minutes <= prefs.max_focus_minutes_per_day


@pytest.mark.parametrize("priority, attends", [(3, True), (5, False)])
def test_midterm_checkpoint(priority, attends):
    tasks, events, prefs, weekly = scenario(midterm_priority=priority)
    plan = solve(tasks, events, prefs, NOW, weekly=weekly)
    check_invariants(plan, tasks, events, prefs)
    lecture = next(e for e in plan.events if e.event_id == "lecture")
    assert lecture.attend is attends


def test_plenty_of_time_keeps_lecture_even_at_top_priority():
    tasks, events, prefs, weekly = scenario(midterm_priority=5)
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
