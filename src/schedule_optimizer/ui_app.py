"""The app window (NiceGUI). Pages: Now, Tasks, Events, Preferences, plus first-run setup.

NiceGUI runs a small local web server (FastAPI) and draws these pages with Vue;
pywebview shows them in a native window. Each button handler is a Python function
that reads or writes the database through `store`, then refreshes the affected list.
Slow work (solving, Google calls) runs in a worker thread with `run.io_bound` so
the window stays responsive.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from nicegui import run, ui

from . import db, google_auth, optimizer_cal, store
from .calendar_sync import CalendarsNotChosen, busy_events, connect
from .domain import EventInput, Plan, TimeOfDay
from .gcal import list_calendars
from .tables import ClaudeMode, EffortMode, Task, TaskStatus

# Single-user desktop app: one shared in-memory cache of the last solve.
STATE: dict = {"plan": None, "events": [], "error": None, "solved_at": None}

PRIORITY_LABELS = {1: "1 · low", 2: "2", 3: "3 · normal", 4: "4", 5: "5 · top"}
WHEN_LABELS = {t.value: t.value.capitalize() for t in TimeOfDay}


def tz():
    with db.session() as s:
        return store.local_tz(store.get_prefs(s))


def fmt_minutes(m: int) -> str:
    h, mm = divmod(int(m), 60)
    return f"{h} h {mm:02d} min" if h else f"{mm} min"


def notify_error(e: Exception) -> None:
    msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
    ui.notify(msg, type="negative", multi_line=True)


# ---- small reusable inputs -------------------------------------------------------------


class DateTimeInput:
    """A date picker + time picker pair that yields an aware datetime (or None).

    With no time entered, a deadline means the end of that day and a start means its beginning.
    """

    def __init__(self, label: str, value: datetime | None, zone, end_of_day: bool, marker: str):
        self.zone, self.end_of_day = zone, end_of_day
        default_time = time(0)
        local = value.astimezone(zone) if value else None
        with ui.row().classes("items-center gap-2 no-wrap"):
            with ui.input(f"{label} date", value=local.strftime("%Y-%m-%d") if local else "",
                          placeholder="YYYY-MM-DD").classes("w-40").mark(f"{marker}-date") as self.date:
                with ui.menu().props("no-parent-event") as menu:
                    ui.date(mask="YYYY-MM-DD").bind_value(self.date)
                with self.date.add_slot("append"):
                    ui.icon("edit_calendar").on("click", menu.open).classes("cursor-pointer")
            with ui.input("Time", value=local.strftime("%H:%M") if local else "",
                          placeholder="end of day" if end_of_day else "00:00").classes("w-28").mark(f"{marker}-time") as self.time:
                with ui.menu().props("no-parent-event") as tmenu:
                    ui.time(mask="HH:mm").bind_value(self.time)
                with self.time.add_slot("append"):
                    ui.icon("schedule").on("click", tmenu.open).classes("cursor-pointer")
            ui.button(icon="close", on_click=lambda: (self.date.set_value(""), self.time.set_value(""))) \
                .props("flat dense round").tooltip("Clear")

    def get(self) -> datetime | None:
        d = (self.date.value or "").strip()
        if not d:
            return None
        try:
            day = datetime.strptime(d, "%Y-%m-%d").date()
            t = (self.time.value or "").strip()
            clock = datetime.strptime(t, "%H:%M").time() if t else time(0)
        except ValueError:
            raise ValueError("Dates look like 2026-11-19 and times like 09:00.")
        if not t and self.end_of_day:
            day += timedelta(days=1)  # midnight at the end of that day
        return datetime.combine(day, clock, tzinfo=self.zone)


def time_input(label: str, value: time, marker: str):
    with ui.input(label, value=value.strftime("%H:%M")).classes("w-32").mark(marker) as inp:
        with ui.menu().props("no-parent-event") as menu:
            ui.time(mask="HH:mm").bind_value(inp)
        with inp.add_slot("append"):
            ui.icon("schedule").on("click", menu.open).classes("cursor-pointer")
    return inp


def parse_clock(text: str) -> time:
    try:
        return time.fromisoformat((text or "").strip())
    except ValueError:
        raise ValueError(f"{text!r} isn't a time; use HH:MM, e.g. 08:00.")


# ---- preferences form (used by setup and the Preferences tab) --------------------------


class PrefsForm:
    def __init__(self, advanced: bool = True):
        with db.session() as s:
            p = store.get_prefs(s)
        with ui.row().classes("gap-4 items-end"):
            self.awake_start = time_input("Wake up", p.awake_start, "pref-awake-start")
            self.awake_end = time_input("Stop working by", p.awake_end, "pref-awake-end")
        with ui.row().classes("gap-4 items-end"):
            self.focus = ui.number("Max focus per day", value=p.max_focus_minutes_per_day / 60, min=0.5, max=16,
                                   step=0.5, suffix="h").classes("w-44").mark("pref-focus")
            self.buffer = ui.number("Buffer between blocks", value=p.buffer_minutes, min=0, max=60, step=5,
                                    suffix="min").classes("w-44").mark("pref-buffer")
            self.horizon = ui.number("Plan ahead", value=p.horizon_days, min=1, max=28, step=1,
                                     suffix="days").classes("w-36").mark("pref-horizon")
        self.weights = None
        if advanced:
            with ui.expansion("Advanced: scoring weights (points)").classes("w-full"):
                ui.label("Task value is 2 points per hour per priority level (priority 5 = 10 points/hour).") \
                    .classes("text-sm text-gray-500")
                with ui.row().classes("gap-4"):
                    self.weights = {
                        "chunk_cost": ui.number("Cost per block", value=p.chunk_cost, step=0.5, min=0),
                        "ideal_deviation_per_hour": ui.number("Per hour off ideal length",
                                                              value=p.ideal_deviation_per_hour, step=0.5, min=0),
                        "time_of_day_bonus_per_hour": ui.number("Preferred time bonus per hour",
                                                                value=p.time_of_day_bonus_per_hour, step=0.5, min=0),
                        "stability_per_hour": ui.number("Re-plan: per hour a block stays put",
                                                        value=p.stability_per_hour, step=0.5, min=0),
                    }

    def save(self) -> bool:
        try:
            changes = dict(
                awake_start=parse_clock(self.awake_start.value),
                awake_end=parse_clock(self.awake_end.value),
                max_focus_minutes_per_day=round(float(self.focus.value or 0) * 60),
                buffer_minutes=int(self.buffer.value or 0),
                horizon_days=int(self.horizon.value or 7),
            )
            if self.weights:
                changes |= {k: float(v.value or 0) for k, v in self.weights.items()}
            with db.session() as s:
                store.update_prefs(s, **changes)
        except ValueError as e:
            notify_error(e)
            return False
        return True


# ---- Google sign-in and calendar choice -----------------------------------------------


async def sign_in(after=None) -> None:
    n = ui.notification("Finish signing in with Google in your browser…", spinner=True, timeout=None)
    try:
        await run.io_bound(google_auth.get_credentials, True)
        n.message, n.type, n.spinner, n.timeout = "Signed in with Google.", "positive", False, 3
    except Exception as e:  # noqa: BLE001  (show any sign-in failure to the user)
        n.dismiss()
        notify_error(e)
        return
    if after:
        after()


async def load_calendars():
    service = await run.io_bound(connect, False)
    return await run.io_bound(list_calendars, service)


def load_later(build) -> None:
    """Run an async builder right after the page renders, so slow calls don't block it."""
    container = ui.column().classes("w-full gap-1")

    async def fill() -> None:
        with container:
            await build()

    ui.timer(0, fill, once=True)


@ui.refreshable
def calendar_picker() -> None:
    load_later(_calendar_picker)


async def _calendar_picker() -> None:
    """Checkboxes for which calendars block time. Saves on every change."""
    if not google_auth.is_signed_in():
        ui.label("Sign in with Google to choose calendars.").classes("text-gray-500")
        ui.button("Sign in with Google", icon="login",
                  on_click=lambda: sign_in(calendar_picker.refresh)).mark("sign-in")
        return
    spinner = ui.spinner()
    try:
        calendars = await load_calendars()
    except Exception as e:  # noqa: BLE001
        spinner.delete()
        ui.label(f"Couldn't load calendars: {e}").classes("text-negative")
        return
    spinner.delete()
    with db.session() as s:
        prefs = store.get_prefs(s)
        chosen = set(prefs.busy_calendars)
    calendars = [c for c in calendars if c.id != prefs.optimizer_calendar_id]
    if not chosen:  # suggest calendars you own or can edit
        chosen = {c.id for c in calendars if c.access_role in ("owner", "writer")}
        with db.session() as s:
            store.update_prefs(s, busy_calendars=sorted(chosen))

    def toggle(cal_id: str, on: bool) -> None:
        with db.session() as s:
            current = set(store.get_prefs(s).busy_calendars)
            current = current | {cal_id} if on else current - {cal_id}
            store.update_prefs(s, busy_calendars=sorted(current))

    ui.label("Events on checked calendars block time; others are ignored.").classes("text-sm text-gray-500")
    for c in calendars:
        label = c.name + (" (primary)" if c.primary else "")
        ui.checkbox(label, value=c.id in chosen, on_change=lambda e, cid=c.id: toggle(cid, e.value))


# ---- Now -------------------------------------------------------------------------------


def _use_google() -> bool:
    if not google_auth.is_signed_in():
        return False
    with db.session() as s:
        return bool(store.get_prefs(s).busy_calendars)


def _solve(use_google: bool) -> tuple[Plan, list[EventInput]]:
    with db.session() as s:
        now = datetime.now(store.local_tz(store.get_prefs(s)))
        events = busy_events(s, now) if use_google else []
        events = store.apply_event_settings(s, events)
        return store.plan_now(s, events, now), events


async def replan() -> None:
    STATE["error"] = None
    n = ui.notification("Planning…", spinner=True, timeout=None)
    try:
        plan, events = await run.io_bound(_solve, _use_google())
        STATE.update(plan=plan, events=events, solved_at=datetime.now())
        n.dismiss()
        if plan.status not in ("OPTIMAL", "FEASIBLE"):
            ui.notify(f"No plan found ({plan.status}).", type="warning")
    except CalendarsNotChosen:
        n.dismiss()
        STATE["error"] = "Choose which calendars block time in Preferences, or sign out to plan offline."
    except Exception as e:  # noqa: BLE001
        n.dismiss()
        STATE["error"] = f"Planning failed: {e}"
    now_view.refresh()


def _approve(write_to_google: bool):
    with db.session() as s:
        service = connect(interactive=False) if write_to_google else None
        return optimizer_cal.approve_and_sync(s, service)


async def approve() -> None:
    to_google = google_auth.is_signed_in()
    n = ui.notification("Approving…" + (" and writing to your Optimizer calendar" if to_google else ""),
                        spinner=True, timeout=None)
    try:
        count, result = await run.io_bound(_approve, to_google)
    except Exception as e:  # noqa: BLE001
        n.dismiss()
        notify_error(e)
        now_view.refresh()
        return
    n.dismiss()
    if not count:
        ui.notify("Nothing to approve.")
    elif result:
        ui.notify(f"Approved {count} blocks. Optimizer calendar: {result.inserted} added, "
                  f"{result.deleted} removed, {result.kept} unchanged.", type="positive")
    else:
        ui.notify(f"Approved {count} blocks (offline, so nothing was written to Google).", type="positive")
    now_view.refresh()


def open_log_dialog(task_id: int | None = None) -> None:
    with db.session() as s:
        tasks = store.list_tasks(s)
    if not tasks:
        ui.notify("Add a task first.")
        return
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Log progress").classes("text-lg font-medium")
        task = ui.select({t.id: t.title for t in tasks}, label="Task",
                         value=task_id or tasks[0].id).classes("w-full").mark("log-task")
        minutes = ui.number("Minutes worked", value=30, min=5, step=5).classes("w-full").mark("log-minutes")
        note = ui.input("Note (optional)").classes("w-full")

        def save() -> None:
            try:
                with db.session() as s:
                    zone = store.local_tz(store.get_prefs(s))
                    m = int(minutes.value or 0)
                    store.log_progress(s, int(task.value), m, datetime.now(zone) - timedelta(minutes=m), note.value)
            except (ValueError, KeyError) as e:
                notify_error(e)
                return
            dialog.close()
            ui.notify(f"Logged {m} min.", type="positive")
            tasks_view.refresh()
            now_view.refresh()

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Log", on_click=save).mark("log-save")
    dialog.open()


@ui.refreshable
def now_view() -> None:
    zone = tz()
    now = datetime.now(zone)
    with db.session() as s:
        blocks = store.upcoming_blocks(s, now)
        has_proposed = bool(store.proposed_blocks(s))
        titles = {t.id: t.title for t in store.list_tasks(s, include_done=True)}

    with ui.row().classes("items-center gap-2"):
        ui.button("Re-plan", icon="auto_awesome", on_click=replan).mark("replan")
        ui.button("Approve plan", icon="check", on_click=approve).mark("approve") \
            .props("" if has_proposed else "disable").tooltip("Saves this plan and writes it to your Optimizer calendar")
        ui.button("Log progress", icon="timer", on_click=lambda: open_log_dialog()).props("outline").mark("log")
        ui.label("Using Google Calendar" if _use_google() else "Offline: Google Calendar not used") \
            .classes("text-sm text-gray-500 ml-2")

    if STATE["error"]:
        ui.label(STATE["error"]).classes("text-negative")

    current = next((b for b in blocks if b.start <= now < b.end), None)
    upcoming = next((b for b in blocks if b.start > now), None)
    with ui.card().classes("w-full"):
        if current:
            ui.label("Right now").classes("text-sm text-gray-500")
            ui.label(titles.get(current.task_id, "?")).classes("text-xl font-medium")
            ui.label(f"until {current.end.astimezone(zone):%H:%M}")
        elif upcoming:
            ui.label("Next up").classes("text-sm text-gray-500")
            ui.label(titles.get(upcoming.task_id, "?")).classes("text-xl font-medium")
            ui.label(f"{upcoming.start.astimezone(zone):%a %H:%M}–{upcoming.end.astimezone(zone):%H:%M}")
        else:
            ui.label("Nothing planned yet. Add tasks, then press Re-plan.").classes("text-gray-500")

    plan: Plan | None = STATE["plan"]
    if plan:
        with ui.row().classes("gap-4 text-sm text-gray-600"):
            ui.label(f"{plan.status.title()} · {plan.objective_points:.1f} points · {plan.wall_seconds:.1f} s")
            for e in plan.skipped:
                ui.label(f"Skips “{e.title}” ({e.start.astimezone(zone):%a %H:%M}, cost {e.skip_cost:g})") \
                    .classes("text-warning")
            for task_id, mins in plan.unmet_minutes.items():
                ui.label(f"Unmet: {titles.get(int(task_id.split('@')[0]), task_id)} · {fmt_minutes(mins)}") \
                    .classes("text-negative")

    items = [("block", b.start, b.end, b) for b in blocks]
    items += [("event", e.start, e.end, e) for e in STATE["events"] if e.end > now]
    items.sort(key=lambda x: x[1])
    status = "Proposed plan" if has_proposed else ("Approved plan" if blocks else "")
    if status:
        ui.label(status).classes("text-lg font-medium mt-2")
    day = None
    skipped = {e.event_id for e in plan.skipped} if plan else set()
    for kind, start, end, obj in items:
        s_local, e_local = start.astimezone(zone), end.astimezone(zone)
        if s_local.date() != day:
            day = s_local.date()
            ui.label(s_local.strftime("%A, %B %d")).classes("font-medium mt-3")
        with ui.row().classes("items-center gap-3 w-full pl-2"):
            ui.label(f"{s_local:%H:%M}–{e_local:%H:%M}").classes("w-28 font-mono text-sm")
            if kind == "block":
                ui.icon("task_alt", color="primary")
                ui.label(titles.get(obj.task_id, "?")).classes("font-medium")
                pts = sum(obj.score.values()) if obj.score else 0
                ui.label(f"{(end - start).seconds // 60} min · {pts:+.1f} pts").classes("text-sm text-gray-500")
            else:
                ui.icon("event_busy" if obj.id in skipped else "event", color="grey")
                ui.label(obj.title).classes("text-gray-500" + (" line-through" if obj.id in skipped else ""))
                if obj.skippable:
                    ui.badge("skipped" if obj.id in skipped else f"skippable · {obj.skip_cost:g}",
                             color="orange" if obj.id in skipped else "grey")


# ---- Tasks -----------------------------------------------------------------------------


def open_task_dialog(task: Task | None = None) -> None:
    zone = tz()
    t = task or Task(title="", total_minutes=60)
    with ui.dialog() as dialog, ui.card().classes("w-[36rem]"):
        ui.label("Edit task" if task else "New task").classes("text-lg font-medium")
        title = ui.input("Title", value=t.title).classes("w-full").mark("task-title")
        mode = ui.toggle({"total": "Total amount", "weekly": "Per week"}, value=t.effort_mode.value) \
            .mark("task-mode")
        # Editing shows the full amount, not what's left.
        minutes = (t.weekly_minutes if t.effort_mode == EffortMode.WEEKLY else t.total_minutes) or 60
        hours = ui.number("Hours", value=round(minutes / 60, 2), min=0.25, step=0.25, suffix="h") \
            .classes("w-40").mark("task-hours")
        priority = ui.select(PRIORITY_LABELS, label="Priority", value=t.priority).classes("w-40").mark("task-priority")
        with ui.column().classes("gap-0").bind_visibility_from(mode, "value", value="total"):
            deadline = DateTimeInput("Deadline", t.deadline, zone, True, "task-deadline")
        when = ui.select(WHEN_LABELS, label="Preferred time of day", value=t.time_of_day.value).classes("w-56")
        with ui.expansion("More options").classes("w-full"):
            start = DateTimeInput("Can't start before", t.earliest_start, zone, False, "task-start")
            with ui.row():
                min_b = ui.number("Shortest block", value=t.min_block, min=5, step=5, suffix="min").classes("w-36")
                ideal_b = ui.number("Ideal block", value=t.ideal_block, min=5, step=5, suffix="min").classes("w-36")
                max_b = ui.number("Longest block", value=t.max_block, min=5, step=5, suffix="min").classes("w-36")
            notes = ui.textarea("Notes", value=t.notes).classes("w-full")

        def save() -> None:
            try:
                start_dt = start.get()
                weekly = mode.value == "weekly"
                fields = dict(
                    title=(title.value or "").strip(),
                    effort_mode=EffortMode(mode.value),
                    total_minutes=None if weekly else round(float(hours.value or 0) * 60),
                    weekly_minutes=round(float(hours.value or 0) * 60) if weekly else None,
                    priority=int(priority.value),
                    deadline=None if weekly else deadline.get(),
                    earliest_start=start_dt,
                    time_of_day=TimeOfDay(when.value),
                    min_block=int(min_b.value or 30), ideal_block=int(ideal_b.value or 90),
                    max_block=int(max_b.value or 120), notes=notes.value or "",
                )
                with db.session() as s:
                    if task:
                        store.update_task(s, task.id, **fields)
                    else:
                        store.add_task(s, **fields)
            except (ValueError, KeyError) as e:
                notify_error(e)
                return
            dialog.close()
            ui.notify("Task saved. Press Re-plan on the Now tab to update the plan.", type="positive")
            tasks_view.refresh()

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=save).mark("task-save")
    dialog.open()


def _task_action(task_id: int, action: str) -> None:
    with db.session() as s:
        if action == "done":
            store.update_task(s, task_id, status=TaskStatus.DONE)
        elif action == "pause":
            store.update_task(s, task_id, status=TaskStatus.PAUSED)
        elif action == "resume":
            store.update_task(s, task_id, status=TaskStatus.ACTIVE)
        elif action == "delete":
            store.delete_task(s, task_id)
    tasks_view.refresh()


def confirm_delete(t: Task) -> None:
    with ui.dialog() as dialog, ui.card():
        ui.label(f"Delete “{t.title}” and its logged progress?")
        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Delete", color="negative",
                      on_click=lambda: (dialog.close(), _task_action(t.id, "delete"))).mark("confirm-delete")
    dialog.open()


@ui.refreshable
def tasks_view() -> None:
    zone = tz()
    now = datetime.now(zone)
    with ui.row().classes("items-center gap-2"):
        ui.button("Add task", icon="add", on_click=lambda: open_task_dialog()).mark("add-task")
        show_done = ui.switch("Show done", value=STATE.get("show_done", False),
                              on_change=lambda e: (STATE.update(show_done=e.value), tasks_view.refresh()))
    with db.session() as s:
        tasks = store.list_tasks(s, include_done=show_done.value)
        left = {t.id: store.remaining_minutes(s, t, now, zone) for t in tasks}
    if not tasks:
        ui.label("No tasks yet. Add one to get started.").classes("text-gray-500 mt-4")
        return
    for t in tasks:
        with ui.card().classes("w-full").mark(f"task-{t.id}"):
            with ui.row().classes("items-center w-full no-wrap"):
                with ui.column().classes("gap-0 grow"):
                    ui.label(t.title).classes("text-base font-medium" + (" line-through" if t.status == TaskStatus.DONE else ""))
                    bits = [f"Priority {t.priority}"]
                    if t.effort_mode == EffortMode.WEEKLY:
                        bits.append(f"{fmt_minutes(left[t.id])} left this week of {fmt_minutes(t.weekly_minutes or 0)}")
                    else:
                        bits.append(f"{fmt_minutes(left[t.id])} left of {fmt_minutes(t.total_minutes or 0)}")
                    if t.deadline:
                        bits.append(f"due {t.deadline.astimezone(zone):%a %b %d %H:%M}")
                    if t.time_of_day != TimeOfDay.ANY:
                        bits.append(t.time_of_day.value)
                    if t.status != TaskStatus.ACTIVE:
                        bits.append(t.status.value)
                    ui.label(" · ".join(bits)).classes("text-sm text-gray-500")
                ui.button(icon="timer", on_click=lambda tid=t.id: open_log_dialog(tid)).props("flat round") \
                    .tooltip("Log progress")
                ui.button(icon="edit", on_click=lambda task=t: open_task_dialog(task)).props("flat round") \
                    .tooltip("Edit").mark(f"edit-{t.id}")
                if t.status == TaskStatus.ACTIVE:
                    ui.button(icon="pause", on_click=lambda tid=t.id: _task_action(tid, "pause")) \
                        .props("flat round").tooltip("Pause")
                    ui.button(icon="check", on_click=lambda tid=t.id: _task_action(tid, "done")) \
                        .props("flat round").tooltip("Mark done").mark(f"done-{t.id}")
                else:
                    ui.button(icon="play_arrow", on_click=lambda tid=t.id: _task_action(tid, "resume")) \
                        .props("flat round").tooltip("Make active again")
                ui.button(icon="delete", on_click=lambda task=t: confirm_delete(task)).props("flat round") \
                    .tooltip("Delete").mark(f"delete-{t.id}")


# ---- Events ----------------------------------------------------------------------------


@ui.refreshable
def rules_view() -> None:
    with db.session() as s:
        rules = store.list_title_rules(s)
    ui.label("Title rules").classes("text-lg font-medium")
    ui.label("Events whose title contains the text start out with these settings. "
             "Settings on a single event override its rule.").classes("text-sm text-gray-500")
    for r in rules:
        with ui.row().classes("items-center gap-3"):
            ui.label(f"“{r.pattern}”").classes("font-medium")
            ui.label(f"skippable, cost {r.skip_cost:g} points" if r.skippable else "fixed").classes("text-gray-600")

            def remove(rid=r.id):
                with db.session() as s:
                    store.delete_title_rule(s, rid)
                rules_view.refresh()
                events_view.refresh()

            ui.button(icon="delete", on_click=remove).props("flat round dense").mark(f"rule-delete-{r.id}")
    with ui.row().classes("items-end gap-3"):
        pattern = ui.input("Title contains", placeholder="Lecture").classes("w-48").mark("rule-pattern")
        skippable = ui.switch("Skippable", value=True)
        cost = ui.number("Skip cost", value=4, min=0, step=1, suffix="pts").classes("w-32").mark("rule-cost") \
            .bind_visibility_from(skippable, "value")

        def add() -> None:
            try:
                with db.session() as s:
                    store.add_title_rule(s, pattern.value or "", skippable=skippable.value,
                                         skip_cost=float(cost.value or 0))
            except ValueError as e:
                notify_error(e)
                return
            rules_view.refresh()
            events_view.refresh()

        ui.button("Add rule", icon="add", on_click=add).mark("rule-add")
    ui.label("A skip cost is in the same points as task value: priority 5 work earns 10 points per hour, "
             "so a cost of 4 means “worth about 24 minutes of top-priority work.”").classes("text-sm text-gray-500")


def _fetch_events() -> list[EventInput]:
    with db.session() as s:
        now = datetime.now(store.local_tz(store.get_prefs(s)))
        return store.apply_event_settings(s, busy_events(s, now))


@ui.refreshable
def events_view() -> None:
    ui.label("Upcoming events").classes("text-lg font-medium mt-4")
    load_later(_events_view)


async def _events_view() -> None:
    if not google_auth.is_signed_in():
        ui.label("Sign in with Google to see your events.").classes("text-gray-500")
        ui.button("Sign in with Google", icon="login", on_click=lambda: sign_in(events_view.refresh))
        return
    spinner = ui.spinner()
    try:
        events = await run.io_bound(_fetch_events)
    except CalendarsNotChosen:
        spinner.delete()
        ui.label("Choose which calendars block time in Preferences first.").classes("text-gray-500")
        return
    except Exception as e:  # noqa: BLE001
        spinner.delete()
        ui.label(f"Couldn't load events: {e}").classes("text-negative")
        return
    spinner.delete()
    with db.session() as s:
        metas = store.event_metas(s)
    zone = tz()
    day = None
    for e in events:
        st, en = e.start.astimezone(zone), e.end.astimezone(zone)
        if st.date() != day:
            day = st.date()
            ui.label(st.strftime("%A, %B %d")).classes("font-medium mt-3")
        with ui.row().classes("items-center gap-3 pl-2"):
            ui.label(f"{st:%H:%M}–{en:%H:%M}").classes("w-28 font-mono text-sm")
            ui.label(e.title).classes("w-72 truncate")

            def save(ev_id=e.id, sw=None, num=None):
                with db.session() as s:
                    store.set_event_meta(s, ev_id, skippable=sw.value, skip_cost=float(num.value or 0))
                ui.notify("Saved for this event.", type="positive")

            sw = ui.switch("Skippable", value=e.skippable)
            num = ui.number("Cost", value=e.skip_cost or 4, min=0, step=1).classes("w-24") \
                .bind_visibility_from(sw, "value")
            sw.on_value_change(lambda _, f=save, a=sw, b=num: f(sw=a, num=b))
            num.on("blur", lambda _, f=save, a=sw, b=num: f(sw=a, num=b))
            if e.id in metas:
                def reset(ev_id=e.id):
                    with db.session() as s:
                        store.clear_event_meta(s, ev_id)
                    events_view.refresh()

                ui.button("Use rule", on_click=reset).props("flat dense").tooltip("Forget settings for this event")


# ---- Preferences -----------------------------------------------------------------------


def account_view() -> None:
    @ui.refreshable
    def body() -> None:
        if google_auth.is_signed_in():
            ui.label("Signed in with Google. The app reads your calendars and writes only to its own "
                     "“Optimizer” calendar.").classes("text-sm")

            def sign_out() -> None:
                google_auth.sign_out()
                ui.notify("Signed out.")
                body.refresh()
                calendar_picker.refresh()

            ui.button("Sign out", icon="logout", on_click=sign_out).props("outline")
        else:
            ui.label("Not signed in. Plans ignore your calendar until you sign in.").classes("text-sm")
            ui.button("Sign in with Google", icon="login",
                      on_click=lambda: sign_in(lambda: (body.refresh(), calendar_picker.refresh())))

    body()


def prefs_view() -> None:
    ui.label("Your day").classes("text-lg font-medium")
    form = PrefsForm()

    def save() -> None:
        if form.save():
            ui.notify("Preferences saved. Re-plan to apply them.", type="positive")

    ui.button("Save preferences", icon="save", on_click=save).mark("prefs-save")
    ui.separator().classes("my-4")
    ui.label("Google Calendar").classes("text-lg font-medium")
    account_view()
    ui.label("Calendars that block time").classes("font-medium mt-2")
    calendar_picker()
    ui.separator().classes("my-4")
    ui.label("Claude").classes("text-lg font-medium")
    with db.session() as s:
        mode = store.get_prefs(s).claude_mode
    ui.select({ClaudeMode.OFF.value: "Off (works offline)",
               ClaudeMode.DESKTOP.value: "Claude Desktop (coming in Phase 7)",
               ClaudeMode.API_KEY.value: "API key (coming in Phase 7)"},
              label="Claude mode", value=mode.value).classes("w-80").props("disable")


# ---- First-run setup -------------------------------------------------------------------


def setup_view() -> None:
    with ui.card().classes("w-full max-w-3xl mx-auto"):
        ui.label("Welcome to Schedule Optimizer").classes("text-2xl font-medium")
        ui.label("Three quick steps. You can change everything later in Preferences.").classes("text-gray-500")
        with ui.stepper().props("vertical flat").classes("w-full") as stepper:
            with ui.step("Your day"):
                form = PrefsForm(advanced=False)
                with ui.stepper_navigation():
                    ui.button("Next", on_click=lambda: form.save() and stepper.next()).mark("setup-next-1")
            with ui.step("Google Calendar"):
                ui.label("Sign in so plans work around your events. The app only reads your calendars, "
                         "and writes to a separate “Optimizer” calendar it creates.").classes("text-sm")
                account_view()
                with ui.stepper_navigation():
                    ui.button("Next", on_click=stepper.next).mark("setup-next-2")
                    ui.button("Back", on_click=stepper.previous).props("flat")
            with ui.step("Busy calendars"):
                calendar_picker()
                with ui.stepper_navigation():
                    def finish() -> None:
                        with db.session() as s:
                            store.update_prefs(s, setup_complete=True)
                        root_view.refresh()

                    ui.button("Finish", icon="check", on_click=finish).mark("setup-finish")
                    ui.button("Back", on_click=stepper.previous).props("flat")


# ---- Root --------------------------------------------------------------------------------


@ui.refreshable
def root_view() -> None:
    with db.session() as s:
        store.import_legacy_settings(s)
        done = store.get_prefs(s).setup_complete
    if not done:
        setup_view()
        return
    with ui.tabs().classes("w-full") as tabs:
        now_tab = ui.tab("Now", icon="today")
        tasks_tab = ui.tab("Tasks", icon="checklist")
        events_tab = ui.tab("Events", icon="event")
        prefs_tab = ui.tab("Preferences", icon="tune")
    with ui.tab_panels(tabs, value=now_tab).classes("w-full"):
        with ui.tab_panel(now_tab):
            now_view()
        with ui.tab_panel(tasks_tab):
            tasks_view()
        with ui.tab_panel(events_tab):
            rules_view()
            ui.button("Reload events", icon="refresh", on_click=events_view.refresh).props("flat").classes("mt-4")
            events_view()
        with ui.tab_panel(prefs_tab):
            prefs_view()


def root() -> None:
    """Builds the page for each window (NiceGUI calls this per client)."""
    ui.page_title("Schedule Optimizer")
    with ui.header().classes("items-center"):
        ui.icon("event_available").classes("text-2xl")
        ui.label("Schedule Optimizer").classes("text-lg font-medium")
    with ui.column().classes("w-full max-w-5xl mx-auto p-4"):
        root_view()
