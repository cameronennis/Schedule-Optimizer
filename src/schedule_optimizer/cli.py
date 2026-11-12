"""Command-line interface to the scheduler's data (Phase 4; the window comes in Phase 5).

    uv run schedule-optimizer task add "Chem midterm" --hours 8 --due "2026-11-19 09:00" --priority 5
    uv run schedule-optimizer task add "Reading" --weekly-hours 3 --when evening
    uv run schedule-optimizer task list
    uv run schedule-optimizer log 1 45                 # 45 minutes on task 1, ending now
    uv run schedule-optimizer rule add Lecture --cost 4
    uv run schedule-optimizer events                   # upcoming events and how they're treated
    uv run schedule-optimizer event set <id> --skippable --cost 8
    uv run schedule-optimizer plan                     # solve with your real calendar
    uv run schedule-optimizer approve
    uv run schedule-optimizer prefs show
    uv run schedule-optimizer prefs set max_focus_minutes_per_day=300 awake_start=07:30
    uv run schedule-optimizer calendars                # choose which calendars block time
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, time, timedelta

from . import db, store
from .calendar_cli import pick_calendars
from .calendar_sync import busy_events
from .demo import print_plan
from .domain import TimeOfDay
from .tables import ClaudeMode, EffortMode, Task, TaskStatus


def parse_when(text: str, tz, end_of_day: bool = False) -> datetime:
    """'2026-11-19 09:00', '2026-11-19T09:00', or a date ('2026-11-19' = start, or end if end_of_day)."""
    text = text.strip()
    try:
        if len(text) == 10:
            d = datetime.fromisoformat(text).date()
            if end_of_day:
                d += timedelta(days=1)
            return datetime.combine(d, time(0), tzinfo=tz)
        dt = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        raise SystemExit(f"Couldn't read the date/time {text!r}; use YYYY-MM-DD or 'YYYY-MM-DD HH:MM'.")
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def fmt_minutes(m: int) -> str:
    h, mm = divmod(m, 60)
    return f"{h}h{mm:02d}" if h else f"{mm}m"


# ---- tasks -----------------------------------------------------------------------


def _task_fields(args, tz) -> dict:
    f = {}
    if args.hours is not None:
        f["total_minutes"], f["effort_mode"] = round(args.hours * 60), EffortMode.TOTAL
    if args.minutes is not None:
        f["total_minutes"], f["effort_mode"] = args.minutes, EffortMode.TOTAL
    if args.weekly_hours is not None:
        f["weekly_minutes"], f["effort_mode"] = round(args.weekly_hours * 60), EffortMode.WEEKLY
    if args.priority is not None:
        f["priority"] = args.priority
    if args.due is not None:
        f["deadline"] = None if args.due == "none" else parse_when(args.due, tz, end_of_day=True)
    if args.start is not None:
        f["earliest_start"] = None if args.start == "none" else parse_when(args.start, tz)
    for name in ("min_block", "ideal_block", "max_block"):
        if getattr(args, name) is not None:
            f[name] = getattr(args, name)
    if args.when is not None:
        f["time_of_day"] = TimeOfDay(args.when)
    if getattr(args, "title", None):
        f["title"] = args.title
    if getattr(args, "notes", None) is not None:
        f["notes"] = args.notes
    return f


def print_tasks(s, tasks: list[Task], tz) -> None:
    if not tasks:
        print("No tasks yet. Add one with: schedule-optimizer task add \"Title\" --hours 2")
        return
    now = datetime.now(tz)
    print(f"{'id':>3}  {'title':<28} {'pri':>3}  {'left':>11}  {'due':<16} {'blocks':<11} {'when':<9} status")
    for t in tasks:
        left = store.remaining_minutes(s, t, now, tz)
        amount = f"{fmt_minutes(left)}/wk" if t.effort_mode == EffortMode.WEEKLY else fmt_minutes(left)
        due = t.deadline.astimezone(tz).strftime("%a %b %d %H:%M") if t.deadline else "-"
        blocks = f"{t.min_block}/{t.ideal_block}/{t.max_block}"
        print(f"{t.id:>3}  {t.title[:28]:<28} {t.priority:>3}  {amount:>11}  {due:<16} {blocks:<11} "
              f"{t.time_of_day.value:<9} {t.status.value}")


def cmd_task(args, s, tz):
    if args.action == "add":
        print_tasks(s, [store.add_task(s, **_task_fields(args, tz))], tz)
    elif args.action == "list":
        print_tasks(s, store.list_tasks(s, include_done=args.all), tz)
    elif args.action == "edit":
        fields = _task_fields(args, tz)
        if args.status:
            fields["status"] = TaskStatus(args.status)
        print_tasks(s, [store.update_task(s, args.id, **fields)], tz)
    elif args.action == "done":
        store.update_task(s, args.id, status=TaskStatus.DONE)
        print(f"Task {args.id} marked done.")
    elif args.action == "rm":
        store.delete_task(s, args.id)
        print(f"Task {args.id} deleted.")


def cmd_log(args, s, tz):
    start = parse_when(args.at, tz) if args.at else datetime.now(tz) - timedelta(minutes=args.minutes)
    store.log_progress(s, args.task_id, args.minutes, start, note=args.note or "")
    t = s.get(Task, args.task_id)
    left = store.remaining_minutes(s, t, datetime.now(tz), tz)
    print(f"Logged {args.minutes} min on “{t.title}”. Remaining: {fmt_minutes(left)}"
          + (" this week." if t.effort_mode == EffortMode.WEEKLY else "."))


# ---- rules and events ----------------------------------------------------------------


def cmd_rule(args, s, tz):
    if args.action == "add":
        r = store.add_title_rule(s, args.pattern, skippable=not args.fixed, skip_cost=args.cost)
        print(f"Rule {r.id}: titles containing “{r.pattern}” → "
              + (f"skippable, cost {r.skip_cost:g}" if r.skippable else "fixed"))
    elif args.action == "list":
        rules = store.list_title_rules(s)
        if not rules:
            print("No title rules. Add one with: schedule-optimizer rule add Lecture --cost 4")
        for r in rules:
            print(f"{r.id:>3}  “{r.pattern}” → " + (f"skippable, cost {r.skip_cost:g}" if r.skippable else "fixed"))
    elif args.action == "rm":
        store.delete_title_rule(s, args.id)
        print(f"Rule {args.id} deleted.")


def cmd_events(args, s, tz):
    now = datetime.now(tz)
    events = store.apply_event_settings(s, busy_events(s, now, choose=pick_calendars))
    day = None
    for e in events:
        st, en = e.start.astimezone(tz), e.end.astimezone(tz)
        if st.date() != day:
            day = st.date()
            print(st.strftime("%A %b %d"))
        how = f"skippable ({e.skip_cost:g})" if e.skippable else "fixed"
        print(f"  {st:%H:%M}–{en:%H:%M}  {e.title[:40]:<40} {how:<16} {e.id}")


def cmd_event(args, s, tz):
    m = store.set_event_meta(s, args.event_id, skippable=args.skippable, skip_cost=args.cost)
    print(f"Event {m.google_event_id}: " + (f"skippable, cost {m.skip_cost:g}" if m.skippable else "fixed"))


# ---- planning ----------------------------------------------------------------------


def cmd_plan(args, s, tz):
    now = datetime.now(tz)
    events = [] if args.offline else busy_events(s, now, choose=pick_calendars)
    plan = store.plan_now(s, events, now)
    print_plan(plan)
    if plan.blocks:
        print("\nThis plan is proposed. Approve it with: schedule-optimizer approve")


def cmd_approve(args, s, tz):
    n = store.approve_plan(s)
    print(f"Approved {n} blocks." if n else "Nothing to approve; run `schedule-optimizer plan` first.")


def cmd_calendars(args, s, tz):
    busy_events(s, datetime.now(tz), choose=pick_calendars, force_choose=True)
    print("Saved.")


# ---- preferences -------------------------------------------------------------------


def _parse_pref(key: str, raw: str):
    field = store.Preferences.model_fields.get(key)
    if field is None or key not in store.PREF_FIELDS:
        raise SystemExit(f"Unknown preference {key!r}. Options: {', '.join(sorted(store.PREF_FIELDS))}")
    t = field.annotation
    if t is time:
        return time.fromisoformat(raw)
    if t is bool:
        return raw.lower() in ("1", "true", "yes", "on")
    if t is int:
        return int(raw)
    if t is float:
        return float(raw)
    if t is ClaudeMode:
        return ClaudeMode(raw)
    if key == "busy_calendars":
        return [c for c in raw.split(",") if c]
    return None if raw.lower() == "none" else raw


def cmd_prefs(args, s, tz):
    if args.action == "set":
        changes = {}
        for pair in args.pairs:
            if "=" not in pair:
                raise SystemExit(f"Use key=value, got {pair!r}")
            k, v = pair.split("=", 1)
            changes[k] = _parse_pref(k, v)
        store.update_prefs(s, **changes)
    p = store.get_prefs(s)
    for k in sorted(store.PREF_FIELDS):
        v = getattr(p, k)
        print(f"  {k:<28} {v.value if hasattr(v, 'value') else v}")
    print(f"\nDatabase: {db.db_path()}")


# ---- argument parsing --------------------------------------------------------------


def _task_options(p, adding: bool):
    if adding:
        p.add_argument("title")
    else:
        p.add_argument("id", type=int)
        p.add_argument("--title")
        p.add_argument("--status", choices=[x.value for x in TaskStatus])
    amount = p.add_mutually_exclusive_group(required=adding)
    amount.add_argument("--hours", type=float, help="total hours of work")
    amount.add_argument("--minutes", type=int, help="total minutes of work")
    amount.add_argument("--weekly-hours", type=float, help="hours per week (resets Monday)")
    p.add_argument("--priority", type=int, choices=range(1, 6))
    p.add_argument("--due", help="deadline: 'YYYY-MM-DD HH:MM' or a date (end of that day); 'none' clears")
    p.add_argument("--start", help="earliest start, same format; 'none' clears")
    p.add_argument("--min", dest="min_block", type=int, help="shortest block, minutes")
    p.add_argument("--ideal", dest="ideal_block", type=int, help="ideal block, minutes")
    p.add_argument("--max", dest="max_block", type=int, help="longest block, minutes")
    p.add_argument("--when", choices=[x.value for x in TimeOfDay], help="preferred time of day")
    p.add_argument("--notes")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="schedule-optimizer", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    task = sub.add_parser("task", help="add, list, edit, finish, or delete tasks")
    tsub = task.add_subparsers(dest="action", required=True)
    _task_options(tsub.add_parser("add"), adding=True)
    tsub.add_parser("list").add_argument("--all", action="store_true", help="include done tasks")
    _task_options(tsub.add_parser("edit"), adding=False)
    tsub.add_parser("done").add_argument("id", type=int)
    tsub.add_parser("rm").add_argument("id", type=int)

    log = sub.add_parser("log", help="log minutes worked on a task")
    log.add_argument("task_id", type=int)
    log.add_argument("minutes", type=int)
    log.add_argument("--at", help="start time (default: minutes ago)")
    log.add_argument("--note")

    rule = sub.add_parser("rule", help="title rules that make matching events skippable")
    rsub = rule.add_subparsers(dest="action", required=True)
    radd = rsub.add_parser("add")
    radd.add_argument("pattern", help="text the title contains (case-insensitive)")
    radd.add_argument("--cost", type=float, default=4.0, help="skip cost in points")
    radd.add_argument("--fixed", action="store_true", help="mark matching events not skippable")
    rsub.add_parser("list")
    rsub.add_parser("rm").add_argument("id", type=int)

    sub.add_parser("events", help="show upcoming busy events and their settings")
    ev = sub.add_parser("event", help="settings for one event instance")
    esub = ev.add_subparsers(dest="action", required=True)
    eset = esub.add_parser("set")
    eset.add_argument("event_id")
    group = eset.add_mutually_exclusive_group(required=True)
    group.add_argument("--skippable", action="store_true")
    group.add_argument("--fixed", action="store_true")
    eset.add_argument("--cost", type=float, default=4.0)

    plan = sub.add_parser("plan", help="solve and show a proposed plan")
    plan.add_argument("--offline", action="store_true", help="ignore Google Calendar")
    sub.add_parser("approve", help="approve the proposed plan")
    sub.add_parser("calendars", help="choose which calendars block time")

    prefs = sub.add_parser("prefs", help="show or change preferences")
    psub = prefs.add_subparsers(dest="action", required=True)
    psub.add_parser("show")
    psub.add_parser("set").add_argument("pairs", nargs="+", metavar="key=value")
    return parser


COMMANDS = {"task": cmd_task, "log": cmd_log, "rule": cmd_rule, "events": cmd_events, "event": cmd_event,
            "plan": cmd_plan, "approve": cmd_approve, "calendars": cmd_calendars, "prefs": cmd_prefs}


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    with db.session() as s:
        store.import_legacy_settings(s)
        tz = store.local_tz(store.get_prefs(s))
        try:
            COMMANDS[args.command](args, s, tz)
        except (ValueError, KeyError) as e:
            print(f"Error: {e.args[0] if e.args else e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
