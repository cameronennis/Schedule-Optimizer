"""The app window, driven by NiceGUI's simulated user (no real window or browser)."""

from datetime import datetime, timedelta

import pytest
from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from schedule_optimizer import db, google_auth, store, ui_app
from schedule_optimizer.tables import Task


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEDULE_OPTIMIZER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(google_auth, "is_signed_in", lambda: False)  # offline: never touch Google
    ui_app.STATE.update(plan=None, events=[], error=None, solved_at=None)
    with db.session() as s:
        store.update_prefs(s, time_zone="America/Los_Angeles")
    yield
    db.engine().dispose()


def finish_setup():
    with db.session() as s:
        store.update_prefs(s, setup_complete=True)


async def test_first_run_setup_saves_preferences(app_db):
    async with user_simulation(root=ui_app.root) as user:
        await user.open("/")
        await user.should_see("Welcome to Schedule Optimizer")
        user.find(marker="pref-focus").clear().type("5")
        user.find(marker="pref-awake-start").clear().type("07:30")
        user.find(marker="setup-next-1").click()
        user.find(marker="setup-next-2").click()
        await user.should_see("Sign in with Google to choose calendars.")
        user.find(marker="setup-finish").click()
        await user.should_see("Nothing planned yet")
    with db.session() as s:
        p = store.get_prefs(s)
        assert p.setup_complete and p.max_focus_minutes_per_day == 300
        assert p.awake_start.strftime("%H:%M") == "07:30"


async def test_add_task_in_window_and_see_it_in_the_plan(app_db):
    """Phase 5 checkpoint: add a task in the window and see it in the plan."""
    finish_setup()
    async with user_simulation(root=ui_app.root) as user:
        await user.open("/")
        user.find(marker="add-task").click()
        user.find(marker="task-title").type("Write lab report")
        user.find(marker="task-hours").clear().type("2")
        user.find(marker="task-save").click()
        await user.should_see("Write lab report")
        await user.should_see("2 h 00 min left of 2 h 00 min")

        user.find(marker="replan").click()
        await user.should_see("Proposed plan", retries=100)  # solving runs in a worker thread
        await user.should_see(marker="approve")
        user.find(marker="approve").click()
        await user.should_see("Approved plan")
    with db.session() as s:
        [t] = store.list_tasks(s)
        assert t.total_minutes == 120
        assert store.upcoming_blocks(s, datetime.now(store.local_tz(store.get_prefs(s))))


async def test_invalid_task_shows_error_and_is_not_saved(app_db):
    finish_setup()
    async with user_simulation(root=ui_app.root) as user:
        await user.open("/")
        user.find(marker="add-task").click()
        user.find(marker="task-save").click()  # no title
        await user.should_see("title is required")
    with db.session() as s:
        assert store.list_tasks(s) == []


async def test_task_with_deadline_date_only(app_db):
    finish_setup()
    async with user_simulation(root=ui_app.root) as user:
        await user.open("/")
        user.find(marker="add-task").click()
        user.find(marker="task-title").type("Essay")
        user.find(marker="task-deadline-date").type("2030-01-15")
        user.find(marker="task-save").click()
        await user.should_see("due Wed Jan 16 00:00")


async def test_log_progress_and_mark_done(app_db):
    finish_setup()
    with db.session() as s:
        t = store.add_task(s, title="Reading", total_minutes=120)
    async with user_simulation(root=ui_app.root) as user:
        await user.open("/")
        user.find(marker="log").click()
        user.find(marker="log-minutes").clear().type("45")
        user.find(marker="log-save").click()
        await user.should_see("1 h 15 min left of 2 h 00 min")
        user.find(marker=f"done-{t.id}").click()
        await user.should_see("No tasks yet")


async def test_delete_asks_first(app_db):
    finish_setup()
    with db.session() as s:
        t = store.add_task(s, title="Old task", total_minutes=60)
    async with user_simulation(root=ui_app.root) as user:
        await user.open("/")
        user.find(marker=f"delete-{t.id}").click()
        await user.should_see("Delete “Old task” and its logged progress?")
        user.find(marker="confirm-delete").click()
        await user.should_see("No tasks yet")


async def test_title_rules_can_be_added_and_removed(app_db):
    finish_setup()
    async with user_simulation(root=ui_app.root) as user:
        await user.open("/")
        user.find(marker="rule-pattern").type("Lecture")
        user.find(marker="rule-cost").clear().type("6")
        user.find(marker="rule-add").click()
        await user.should_see("skippable, cost 6 points")
        with db.session() as s:
            [r] = store.list_title_rules(s)
        user.find(marker=f"rule-delete-{r.id}").click()
        await user.should_not_see("skippable, cost 6 points")


async def test_preferences_tab_rejects_bad_times(app_db):
    finish_setup()
    async with user_simulation(root=ui_app.root) as user:
        await user.open("/")
        user.find(marker="pref-awake-end").clear().type("06:00")
        user.find(marker="prefs-save").click()
        await user.should_see("awake_end must be after awake_start")
