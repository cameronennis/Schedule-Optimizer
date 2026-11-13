"""The app window, driven by NiceGUI's simulated user (no real window or browser)."""

import asyncio
from datetime import datetime, timedelta

import pytest
from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from schedule_optimizer import changes, claude_api, db, google_auth, store, ui_app
from schedule_optimizer.tables import ClaudeMode, Task


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEDULE_OPTIMIZER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(google_auth, "is_signed_in", lambda: False)  # offline: never touch Google
    monkeypatch.setenv("SCHEDULE_OPTIMIZER_OFFLINE", "1")
    ui_app.STATE.update(plan=None, events=[], error=None, solved_at=None)
    ui_app.CHAT.__init__()
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


# ---- Phase 7 ---------------------------------------------------------------------------

def local_now():
    with db.session() as s:
        return datetime.now(store.local_tz(store.get_prefs(s)))


async def test_unmet_work_explains_why(app_db):
    finish_setup()
    now = local_now()
    with db.session() as s:
        store.add_task(s, title="Paper", total_minutes=24 * 60, deadline=now + timedelta(hours=26))
    async with user_simulation(root=ui_app.root) as user:
        await user.open("/")
        user.find(marker="replan").click()
        await user.should_see("Unmet: Paper", retries=200)
        [task] = store_tasks()
        await user.should_see(marker=f"why-{task.id}")


def store_tasks():
    with db.session() as s:
        return store.list_tasks(s)


async def test_proposal_can_be_applied_from_the_now_tab(app_db):
    finish_setup()
    now = local_now()
    with db.session() as s:
        t = store.add_task(s, title="Essay", total_minutes=60)
        row = changes.propose(s, [{"op": "update_task", "task_id": t.id, "priority": 5}],
                              "Make the essay top priority", [], now)
    async with user_simulation(root=ui_app.root) as user:
        await user.open("/")
        await user.should_see("Make the essay top priority")
        user.find(marker=f"apply-{row.id}").click()
        await user.should_see("Applied", retries=100)
    with db.session() as s:
        assert store.list_tasks(s)[0].priority == 5
        assert changes.pending(s) == []


async def test_desktop_mode_shows_config_snippet(app_db):
    finish_setup()
    with db.session() as s:
        store.update_prefs(s, claude_mode=ClaudeMode.DESKTOP)
    async with user_simulation(root=ui_app.root) as user:
        await user.open("/")
        user.find("Preferences").click()
        await user.should_see("Settings › Developer › Edit Config", retries=50)
    assert ui_app.desktop_config()["mcpServers"]["schedule-optimizer"]["args"][-1] == "schedule-optimizer-mcp"


async def test_api_mode_chat_tab(app_db, monkeypatch):
    finish_setup()
    with db.session() as s:
        store.update_prefs(s, claude_mode=ClaudeMode.API_KEY)
    monkeypatch.setattr(claude_api, "has_key", lambda: True)

    def fake_send(text):
        ui_app.CHAT.log += [("user", text), ("claude", "Take a 20-minute walk first.")]

    monkeypatch.setattr(ui_app, "_send_chat", fake_send)
    async with user_simulation(root=ui_app.root) as user:
        await user.open("/")
        user.find("Claude").click()
        user.find(marker="chat-input").type("I'm tired")
        user.find(marker="chat-send").click()
        await user.should_see("Take a 20-minute walk first.", retries=100)


async def test_not_fully_optimized_is_flagged_with_a_longer_option(app_db, monkeypatch):
    from schedule_optimizer.domain import Plan
    finish_setup()
    with db.session() as s:
        store.save_last_solve(s, Plan("FEASIBLE", 12.0, [], [], {}, 5.0), [], [], "app")
    calls = []

    async def fake_replan(seconds=None):
        calls.append(seconds)

    monkeypatch.setattr(ui_app, "replan", fake_replan)
    async with user_simulation(root=ui_app.root) as user:
        await user.open("/")
        await user.should_see("not fully optimized")
        user.find(marker="optimize-longer").click()
        for _ in range(50):  # the click handler runs as a background task
            if calls:
                break
            await asyncio.sleep(0.02)
    assert calls == [store.LONG_SOLVE_SECONDS]


async def test_optimal_plan_says_so(app_db):
    from schedule_optimizer.domain import Plan
    finish_setup()
    with db.session() as s:
        store.save_last_solve(s, Plan("OPTIMAL", 12.0, [], [], {}, 0.4), [], [], "app")
    async with user_simulation(root=ui_app.root) as user:
        await user.open("/")
        await user.should_see("optimal")
        await user.should_not_see("not fully optimized")
