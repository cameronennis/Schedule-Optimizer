"""Database tables, the store layer, and the CLI, against throwaway SQLite files."""

import json
import sqlite3
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from schedule_optimizer import cli, db, store
from schedule_optimizer.domain import EventInput
from schedule_optimizer.tables import BlockStatus, PlanBlock, ProgressLog, Task, TaskStatus

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 18, 8, 0, tzinfo=TZ)  # Wednesday


@pytest.fixture
def db_file(tmp_path):
    return tmp_path / "schedule.db"


@pytest.fixture
def s(db_file):
    eng = db.make_engine(db_file)
    with Session(eng, expire_on_commit=False) as session:
        store.update_prefs(session, time_zone="America/Los_Angeles")
        yield session
    eng.dispose()


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


# ---- persistence and setup -----------------------------------------------------------

def test_tasks_survive_restart(db_file):
    eng = db.make_engine(db_file)
    with Session(eng) as session:
        store.add_task(session, title="Chem midterm", total_minutes=480, priority=5, deadline=at(19, 9))
    eng.dispose()

    eng = db.make_engine(db_file)  # "restart": brand-new engine on the same file
    with Session(eng) as session:
        [t] = store.list_tasks(session)
        assert (t.title, t.total_minutes, t.priority) == ("Chem midterm", 480, 5)
        assert t.deadline == at(19, 9)  # time zone survives as the same instant
        assert t.deadline.tzinfo is not None
    eng.dispose()


def test_wal_mode_and_schema_version(db_file):
    db.make_engine(db_file).dispose()
    conn = sqlite3.connect(db_file)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"task", "eventmeta", "titlerule", "preferences", "progresslog", "planblock"} <= tables


def test_two_processes_can_share_the_file(db_file):
    """WAL: a reader holding a transaction doesn't block a writer (app + Claude helper)."""
    a, b = db.make_engine(db_file), db.make_engine(db_file)
    with a.connect() as reader:
        reader.execute(text("BEGIN"))
        reader.execute(text("SELECT count(*) FROM task")).scalar_one()
        with Session(b) as writer:
            store.add_task(writer, title="From the helper", total_minutes=30)
        reader.execute(text("COMMIT"))
    with Session(a) as session:
        assert [t.title for t in store.list_tasks(session)] == ["From the helper"]


def test_migrations_run_once_on_old_files(db_file, monkeypatch):
    db.make_engine(db_file).dispose()

    def add_color(conn):
        conn.execute(text("ALTER TABLE task ADD COLUMN color VARCHAR"))

    monkeypatch.setattr(db, "MIGRATIONS", [add_color])
    db.make_engine(db_file).dispose()
    db.make_engine(db_file).dispose()  # second open must not re-run it
    conn = sqlite3.connect(db_file)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert "color" in [r[1] for r in conn.execute("PRAGMA table_info(task)")]


def test_deleting_a_task_removes_its_logs_and_blocks(s):
    t = store.add_task(s, title="X", total_minutes=60)
    store.log_progress(s, t.id, 30, NOW)
    s.add(PlanBlock(task_id=t.id, start=NOW, end=NOW + timedelta(hours=1)))
    s.commit()
    store.delete_task(s, t.id)
    assert s.exec(select(ProgressLog)).all() == []
    assert s.exec(select(PlanBlock)).all() == []


def test_naive_datetimes_are_rejected(s):
    with pytest.raises(ValueError):
        store.add_task(s, title="X", total_minutes=60, deadline=datetime(2026, 11, 19, 9))


# ---- validation ----------------------------------------------------------------------

@pytest.mark.parametrize("fields", [
    {"title": "", "total_minutes": 60},
    {"title": "X"},  # no amount
    {"title": "X", "total_minutes": 60, "priority": 6},
    {"title": "X", "total_minutes": 60, "min_block": 100, "ideal_block": 90},
    {"title": "X", "weekly_minutes": 60, "deadline": at(20, 9)},
    {"title": "X", "total_minutes": 60, "earliest_start": at(20, 9), "deadline": at(19, 9)},
])
def test_invalid_tasks_are_rejected(s, fields):
    with pytest.raises(ValueError):
        store.add_task(s, **fields)


def test_prefs_validation(s):
    with pytest.raises(ValueError):
        store.update_prefs(s, awake_start=time(22), awake_end=time(8))
    with pytest.raises(ValueError):
        store.update_prefs(s, not_a_pref=1)
    store.update_prefs(s, awake_end=time(0))  # midnight is allowed
    assert store.get_prefs(s).awake_end == time(0)


# ---- progress ------------------------------------------------------------------------

def test_remaining_minutes_total_and_weekly(s):
    total = store.add_task(s, title="Essay", total_minutes=240)
    weekly = store.add_task(s, title="Reading", weekly_minutes=180)
    store.log_progress(s, total.id, 60, at(10, 9))
    store.log_progress(s, weekly.id, 50, at(15, 9))  # last Sunday: previous week
    store.log_progress(s, weekly.id, 30, at(17, 9))  # this Tuesday
    assert store.remaining_minutes(s, total, NOW, TZ) == 180
    assert store.remaining_minutes(s, weekly, NOW, TZ) == 150


def test_finished_and_paused_tasks_are_not_planned(s):
    a = store.add_task(s, title="A", total_minutes=60)
    b = store.add_task(s, title="B", total_minutes=60)
    c = store.add_task(s, title="C", total_minutes=60)
    store.update_task(s, b.id, status=TaskStatus.PAUSED)
    store.log_progress(s, c.id, 60, at(17, 9))  # fully done by logging
    tasks, weekly = store.solver_inputs(s, NOW)
    assert [t.id for t in tasks] == [str(a.id)] and weekly == []


# ---- events: title rules and per-event settings --------------------------------------

def test_event_settings_precedence(s):
    events = [
        EventInput("e1", "CHEM Lecture 12", at(18, 9), at(18, 10, 20)),
        EventInput("e2", "Chem lecture 13", at(19, 9), at(19, 10, 20)),
        EventInput("e3", "Dentist", at(19, 14), at(19, 15)),
    ]
    assert not any(e.skippable for e in store.apply_event_settings(s, events))  # no defaults
    store.add_title_rule(s, "lecture", skip_cost=4)
    store.set_event_meta(s, "e2", skippable=False)  # this instance stays fixed
    out = {e.id: e for e in store.apply_event_settings(s, events)}
    assert out["e1"].skippable and out["e1"].skip_cost == 4
    assert not out["e2"].skippable
    assert not out["e3"].skippable


# ---- planning from the database ------------------------------------------------------

def test_plan_now_from_database_and_approve(s):
    store.update_prefs(s, max_focus_minutes_per_day=480)
    mid = store.add_task(s, title="Study for chem midterm", total_minutes=8 * 60, priority=5, deadline=at(19, 9))
    store.add_title_rule(s, "review session", skip_cost=12)
    events = [
        EventInput("lecture", "Chem lecture (review session)", at(18, 9), at(18, 10, 20)),
        EventInput("lab", "Chem lab", at(18, 13), at(18, 16)),
        EventInput("shift", "Library shift", at(18, 17), at(18, 22)),
        EventInput("exam", "Chem midterm", at(19, 9), at(19, 10, 30)),
    ]
    plan = store.plan_now(s, events, NOW)
    assert [e.attend for e in plan.events] == [False]  # same result as the Phase 2 demo
    proposed = store.proposed_blocks(s)
    assert proposed and all(b.task_id == mid.id for b in proposed)
    assert proposed[0].score["value"] > 0

    assert store.approve_plan(s) == len(proposed)
    assert store.proposed_blocks(s) == []
    store.plan_now(s, events, NOW)  # a new solve doesn't touch approved blocks until approved
    approved = s.exec(select(PlanBlock).where(PlanBlock.status == BlockStatus.APPROVED)).all()
    assert len(approved) == len(proposed)


def test_weekly_task_ids_map_back_to_database(s):
    t = store.add_task(s, title="Reading", weekly_minutes=120)
    store.plan_now(s, [], NOW)
    assert {b.task_id for b in store.proposed_blocks(s)} == {t.id}


def test_legacy_settings_json_is_imported(s, tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEDULE_OPTIMIZER_DATA_DIR", str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"busy_calendars": ["primary", "work"]}))
    store.import_legacy_settings(s)
    assert store.get_prefs(s).busy_calendars == ["primary", "work"]


# ---- CLI -----------------------------------------------------------------------------

@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEDULE_OPTIMIZER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "_engine", None)
    cli.main(["prefs", "set", "time_zone=America/Los_Angeles"])
    yield
    if db._engine is not None:
        db._engine.dispose()
    monkeypatch.setattr(db, "_engine", None)


def test_cli_task_lifecycle(cli_db, capsys):
    cli.main(["task", "add", "Chem midterm", "--hours", "8", "--due", "2026-11-19 09:00", "--priority", "5"])
    cli.main(["task", "add", "Reading", "--weekly-hours", "3", "--when", "evening"])
    cli.main(["log", "1", "45"])
    cli.main(["task", "edit", "2", "--priority", "1"])
    capsys.readouterr()
    cli.main(["task", "list"])
    out = capsys.readouterr().out
    assert "Chem midterm" in out and "7h15" in out and "3h00/wk" in out
    cli.main(["task", "done", "1"])
    cli.main(["task", "list"])
    assert "Chem midterm" not in capsys.readouterr().out


def test_cli_due_date_only_means_end_of_day(cli_db):
    cli.main(["task", "add", "Essay", "--hours", "2", "--due", "2026-11-20"])
    with db.session() as session:
        [t] = store.list_tasks(session)
        assert t.deadline == datetime(2026, 11, 21, 0, 0, tzinfo=TZ).astimezone(timezone.utc)


def test_cli_reports_errors_without_traceback(cli_db, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["task", "add", "Bad", "--hours", "1", "--min", "200"])
    assert exc.value.code == 1
    assert "block sizes" in capsys.readouterr().err


def test_cli_offline_plan(cli_db, capsys):
    cli.main(["task", "add", "Essay", "--hours", "2"])
    cli.main(["plan", "--offline"])
    assert "Essay" in capsys.readouterr().out
    cli.main(["approve"])
    assert "Approved" in capsys.readouterr().out
