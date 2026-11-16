"""Phase 3 checkpoint: sign in with Google and print the next 7 days of busy events.

    uv run python -m schedule_optimizer.calendar_cli            # sign in (first run), show your week
    uv run python -m schedule_optimizer.calendar_cli --pick     # choose busy calendars again
    uv run python -m schedule_optimizer.calendar_cli --solve    # plan sample tasks around your week
    uv run python -m schedule_optimizer.calendar_cli --sign-out # revoke and forget the saved sign-in
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from . import fmt
from . import db, google_auth, store
from .calendar_sync import busy_events
from .demo import print_plan
from .domain import TaskInput, TimeOfDay, WeeklyTask
from .gcal import CalendarInfo, calendar_service, list_calendars
from .solver import solve


def pick_calendars(calendars: list[CalendarInfo]) -> list[str]:
    defaults = [i for i, c in enumerate(calendars, 1) if c.access_role in ("owner", "writer")]
    print("\nWhich calendars should block time? (events on others are ignored)\n")
    for i, c in enumerate(calendars, 1):
        tag = " (primary)" if c.primary else ""
        mark = "*" if i in defaults else " "
        print(f" {mark}{i:>2}. {c.name}{tag}   [{c.access_role}]")
    print("\n * = default (calendars you own or can edit)")
    while True:
        raw = input("Numbers separated by commas, or Enter for the defaults: ").strip()
        try:
            chosen = defaults if not raw else sorted({int(x) for x in raw.replace(" ", "").split(",") if x})
            if chosen and all(1 <= n <= len(calendars) for n in chosen):
                return [calendars[n - 1].id for n in chosen]
        except ValueError:
            pass
        print(f"Please enter numbers between 1 and {len(calendars)}.")


def sample_tasks(now: datetime):
    return (
        [
            TaskInput("sample-study", "Sample: exam study", remaining_minutes=6 * 60, priority=2,
                      deadline=now + timedelta(days=3)),
            TaskInput("sample-project", "Sample: project work", remaining_minutes=4 * 60, priority=3),
        ],
        [WeeklyTask("sample-reading", "Sample: reading", weekly_minutes=2 * 60, priority=4,
                    time_of_day=TimeOfDay.EVENING)],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pick", action="store_true", help="choose busy calendars again")
    parser.add_argument("--solve", action="store_true", help="plan sample tasks around your real events")
    parser.add_argument("--sign-out", action="store_true", help="revoke and forget the saved Google sign-in")
    args = parser.parse_args()

    if args.sign_out:
        google_auth.sign_out()
        print("Signed out. The next run will open the browser again.")
        return

    first_time = not google_auth.is_signed_in()
    creds = google_auth.get_credentials(interactive=True)
    print("Signed in (new sign-in)." if first_time else "Signed in from saved Keychain token — no browser needed.")

    service = calendar_service(creds)
    with db.session() as s:
        store.import_legacy_settings(s)
        now = datetime.now(store.local_tz(store.get_prefs(s)))
        events = busy_events(s, now, service=service, choose=pick_calendars, force_choose=args.pick)
        db_prefs = store.get_prefs(s)
        tz = store.local_tz(db_prefs)
        prefs = store.solver_prefs(db_prefs)
        names = {c.id: c.name for c in list_calendars(service)}
        print("Busy calendars: " + ", ".join(names.get(b, b) for b in db_prefs.busy_calendars))

    print(f"\nNext {prefs.horizon_days} days ({tz}): {len(events)} busy events\n")
    day = None
    for e in events:
        s, t = e.start.astimezone(tz), e.end.astimezone(tz)
        if s.date() != day:
            day = s.date()
            print(fmt.day(s))
        print(f"  {fmt.span(s, t):<18}  {e.title}")

    if args.solve:
        tasks, weekly = sample_tasks(now)
        print("\nPlanning sample tasks around these events…\n")
        print_plan(solve(tasks, events, prefs, now, weekly=weekly))


if __name__ == "__main__":
    main()
