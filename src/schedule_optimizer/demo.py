"""Phase 2 demo: solve a hard-coded exam-crunch week and print the plan.

    uv run python -m schedule_optimizer.demo                      # midterm priority 3
    uv run python -m schedule_optimizer.demo --midterm-priority 1 # top priority: lecture gets skipped
"""

from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from . import fmt
from .domain import EventInput, Plan, Preferences, TaskInput, TimeOfDay, WeeklyTask, skip_cost_for, skip_for_short
from .solver import solve

TZ = ZoneInfo("America/Los_Angeles")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """Local time on November `day`, 2026 (the 18th is a Wednesday)."""
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


NOW = at(18, 8)  # Wednesday 8:00


def scenario(midterm_priority: int = 3, lecture_skip_for: int = 1):
    tasks = [
        TaskInput("midterm", "Study for chem midterm", remaining_minutes=8 * 60,
                  priority=midterm_priority, deadline=at(19, 9)),
        TaskInput("pset", "Problem set 6", remaining_minutes=4 * 60, priority=3, deadline=at(20, 17)),
    ]
    weekly = [
        WeeklyTask("reading", "Seminar reading", weekly_minutes=3 * 60, priority=4,
                   time_of_day=TimeOfDay.EVENING),
    ]
    events = [
        EventInput("lecture", "Chem lecture (review session)", at(18, 9), at(18, 10, 20),
                   skippable=True, skip_cost=skip_cost_for(lecture_skip_for, 80), skip_for=lecture_skip_for),
        EventInput("lab", "Chem lab", at(18, 13), at(18, 16)),
        EventInput("shift", "Library shift", at(18, 17), at(18, 22)),
        EventInput("exam", "Chem midterm", at(19, 9), at(19, 10, 30)),
    ]
    # Exam week: allow up to 8 hours of focus per day.
    prefs = Preferences(max_focus_minutes_per_day=8 * 60)
    return tasks, events, prefs, weekly


def print_plan(plan: Plan) -> None:
    print(f"Status: {plan.status}   score: {plan.objective_points:.1f} points   ({plan.wall_seconds:.1f} s)\n")
    day = None
    for b in plan.blocks:
        if b.start.date() != day:
            day = b.start.date()
            print(fmt.day(b.start))
        pts = sum(b.score.values())
        print(f"  {fmt.span(b.start, b.end):<18}  {b.minutes:>3} min  {b.title:<24} {pts:+.1f} pts")
    print()
    for e in plan.events:
        verdict = "attend" if e.attend else f"SKIP (skippable for {skip_for_short(e.skip_for or 5)} work)"
        print(f"{e.title} {fmt.when(e.start)}: {verdict}")
    for task_id, minutes in plan.unmet_minutes.items():
        print(f"Unmet: {task_id} — {minutes} min")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--midterm-priority", type=int, default=3, choices=range(1, 6))
    parser.add_argument("--lecture-skip-for", type=int, default=1, choices=range(1, 6),
                        help="the lecture may be skipped for priority 1 through this")
    args = parser.parse_args()
    tasks, events, prefs, weekly = scenario(args.midterm_priority, args.lecture_skip_for)
    print_plan(solve(tasks, events, prefs, NOW, weekly=weekly))


if __name__ == "__main__":
    main()
