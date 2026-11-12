"""Turn weekly intentions into ordinary deadline tasks, one per week in the horizon.

A week runs Monday 00:00 to the next Monday 00:00 (local time). The current week's
instance gets what's left after logged minutes; later weeks get the full amount.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from .domain import TaskInput, WeeklyTask
from .timegrid import TimeGrid


def expand_weekly(task: WeeklyTask, grid: TimeGrid) -> list[TaskInput]:
    start_local = grid.origin.astimezone(grid.tz)
    monday = start_local.date() - timedelta(days=start_local.weekday())
    out: list[TaskInput] = []
    first = True
    while True:
        week_start = datetime.combine(monday, time(0), tzinfo=grid.tz)
        if week_start >= grid.end:
            break
        week_end = datetime.combine(monday + timedelta(days=7), time(0), tzinfo=grid.tz)
        remaining = task.weekly_minutes - (task.logged_this_week if first else 0)
        if remaining > 0:
            out.append(
                TaskInput(
                    id=f"{task.id}@{monday.isoformat()}",
                    title=task.title,
                    remaining_minutes=remaining,
                    priority=task.priority,
                    deadline=week_end,
                    earliest_start=None if first else week_start,
                    min_block=task.min_block,
                    ideal_block=task.ideal_block,
                    max_block=task.max_block,
                    time_of_day=task.time_of_day,
                )
            )
        first = False
        monday += timedelta(days=7)
    return out
