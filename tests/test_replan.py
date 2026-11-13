"""Phase 6: writing to the Optimizer calendar, and re-planning without a reshuffle."""

import itertools
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httplib2
import pytest
from googleapiclient.errors import HttpError
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from schedule_optimizer import calendar_sync, db, optimizer_cal, store
from schedule_optimizer.domain import Preferences, TaskInput
from schedule_optimizer.solver import solve

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 18, 8, 0, tzinfo=TZ)


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


@pytest.fixture
def s(tmp_path):
    eng = db.make_engine(tmp_path / "schedule.db")
    with Session(eng, expire_on_commit=False) as session:
        store.update_prefs(session, time_zone="America/Los_Angeles")
        yield session
    eng.dispose()


# ---- a fake Google Calendar --------------------------------------------------------------

def http_error(status):
    return HttpError(httplib2.Response({"status": status}), b"{}")


class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeBatch:
    def __init__(self, service):
        self.service, self.items = service, []

    def add(self, req, callback, request_id):
        self.items.append((req, callback, request_id))

    def execute(self):
        self.service.batches += 1
        for req, cb, rid in self.items:
            try:
                cb(rid, req.execute(), None)
            except Exception as e:  # noqa: BLE001
                cb(rid, None, e)


class FakeCalendar:
    """Just enough of calendars() and events() for optimizer_cal and calendar_sync."""

    def __init__(self):
        self.calendars_by_id: dict[str, dict] = {}
        self.events_by_cal: dict[str, dict[str, dict]] = {}
        self.ids = itertools.count(1)
        self.batches = 0
        self.log: list[str] = []

    # service.calendars()
    def calendars(self):
        svc = self

        class C:
            def get(self, calendarId):
                def run():
                    if calendarId not in svc.calendars_by_id:
                        raise http_error(404)
                    return svc.calendars_by_id[calendarId]
                return Req(run)

            def insert(self, body):
                def run():
                    cid = f"cal{next(svc.ids)}@group.calendar.google.com"
                    svc.calendars_by_id[cid] = {"id": cid, **body}
                    svc.events_by_cal[cid] = {}
                    svc.log.append(f"create calendar {body['summary']}")
                    return svc.calendars_by_id[cid]
                return Req(run)
        return C()

    # service.calendarList() (for calendar_sync)
    def calendarList(self):
        svc = self

        class L:
            def list(self, pageToken=None, **kw):
                items = [{"id": "primary@x", "summary": "Me", "primary": True, "accessRole": "owner",
                          "timeZone": "America/Los_Angeles"}]
                items += [{"id": c, "summary": v["summary"], "accessRole": "owner"} for c, v in svc.calendars_by_id.items()]
                return Req(lambda: {"items": items})
        return L()

    # service.events()
    def events(self):
        svc = self

        class E:
            def list(self, calendarId, pageToken=None, timeMin=None, privateExtendedProperty=None, **kw):
                def run():
                    items = list(svc.events_by_cal.get(calendarId, {}).values())
                    if timeMin:
                        t = datetime.fromisoformat(timeMin)
                        items = [e for e in items if datetime.fromisoformat(e["end"]["dateTime"]) > t]
                    return {"items": items}
                return Req(run)

            def insert(self, calendarId, body):
                def run():
                    eid = f"ev{next(svc.ids)}"
                    svc.events_by_cal[calendarId][eid] = {"id": eid, **body}
                    svc.log.append(f"insert {body['summary']}")
                    return svc.events_by_cal[calendarId][eid]
                return Req(run)

            def delete(self, calendarId, eventId):
                def run():
                    if eventId not in svc.events_by_cal[calendarId]:
                        raise http_error(410)
                    del svc.events_by_cal[calendarId][eventId]
                    svc.log.append(f"delete {eventId}")
                return Req(run)

            def patch(self, calendarId, eventId, body):
                def run():
                    svc.events_by_cal[calendarId][eventId].update(body)
                    svc.log.append(f"trim {eventId}")
                    return svc.events_by_cal[calendarId][eventId]
                return Req(run)
        return E()

    def new_batch_http_request(self):
        return FakeBatch(self)

    def optimizer_events(self, cal_id):
        return sorted(self.events_by_cal[cal_id].values(), key=lambda e: e["start"]["dateTime"])


# ---- stability in the solver ---------------------------------------------------------

def overlap_minutes(a, b):
    total = 0
    for x in a:
        for y in b:
            if x.task_id.split("@")[0] == y.task_id.split("@")[0]:
                total += max(0, (min(x.end, y.end) - max(x.start, y.start)).total_seconds() / 60)
    return total


def test_replan_keeps_blocks_in_place_after_logging_progress():
    """Phase 6 checkpoint, in the solver: log 30 min, re-plan, and most blocks stay put."""
    prefs = Preferences(horizon_days=4)
    tasks = [TaskInput("1", "Essay", remaining_minutes=6 * 60, priority=3, deadline=at(21, 23)),
             TaskInput("2", "Problem set", remaining_minutes=4 * 60, priority=4, deadline=at(20, 17))]
    first = solve(tasks, [], prefs, NOW)
    previous = {}
    for b in first.blocks:
        previous.setdefault(b.task_id, []).append((b.start, b.end))

    tasks[0].remaining_minutes -= 30  # logged 30 minutes of essay
    later = NOW + timedelta(minutes=30)
    second = solve(tasks, [], prefs, later, previous=previous)
    still_ahead = [b for b in first.blocks if b.start >= later]
    kept = overlap_minutes(second.blocks, still_ahead)
    planned = sum(b.minutes for b in still_ahead)
    assert kept >= 0.8 * planned - 30, f"only {kept:.0f} of {planned} minutes stayed in place"
    assert any("stability_bonus" in b.score for b in second.blocks)


def test_stability_never_overrides_real_value():
    """A block worth more elsewhere still moves: e.g., a new fixed event lands on it."""
    from schedule_optimizer.domain import EventInput

    prefs = Preferences(horizon_days=1)
    tasks = [TaskInput("1", "Essay", remaining_minutes=90)]
    first = solve(tasks, [], prefs, NOW)
    [b] = first.blocks
    meeting = EventInput("m", "Meeting", b.start, b.end)
    second = solve(tasks, [meeting], prefs, NOW, previous={"1": [(b.start, b.end)]})
    [moved] = second.blocks
    assert moved.start >= b.end or moved.end <= b.start


# ---- schema migration ----------------------------------------------------------------

def test_phase4_database_upgrades_to_latest(tmp_path):
    path = tmp_path / "old.db"
    eng = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(eng)
    with eng.begin() as conn:  # make it look like a Phase 4 (v1) file
        conn.execute(text("ALTER TABLE preferences DROP COLUMN stability_per_hour"))
        conn.execute(text("ALTER TABLE preferences DROP COLUMN optimizer_calendar_id"))
        conn.execute(text("ALTER TABLE preferences DROP COLUMN claude_monthly_cap_usd"))  # v3 additions
        for table in ("proposal", "hold", "lastsolve", "apiusage"):
            conn.execute(text(f"DROP TABLE {table}"))
        conn.execute(text("INSERT INTO preferences (id, awake_start, awake_end, max_focus_minutes_per_day, "
                          "buffer_minutes, horizon_days, busy_calendars, claude_mode, setup_complete, chunk_cost, "
                          "ideal_deviation_per_hour, time_of_day_bonus_per_hour) VALUES (1, '08:00:00', '23:00:00', "
                          "300, 5, 7, '[\"primary\"]', 'OFF', 1, 1.0, 1.0, 1.0)"))
        conn.execute(text("PRAGMA user_version = 1"))
    eng.dispose()

    eng = db.make_engine(path)
    with Session(eng) as session:
        p = store.get_prefs(session)
        assert (p.max_focus_minutes_per_day, p.busy_calendars) == (300, ["primary"])  # data kept
        assert p.stability_per_hour == 1.0 and p.optimizer_calendar_id is None
        assert p.claude_monthly_cap_usd == 5.0 and store.list_holds(session) == []
    eng.dispose()
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 4


# ---- Optimizer calendar sync ---------------------------------------------------------

def plan_and_approve(s, now=NOW):
    store.plan_now(s, [], now)
    return store.approve_plan(s)


def test_first_approve_creates_calendar_and_writes_blocks(s):
    store.add_task(s, title="Essay", total_minutes=180, deadline=at(19, 23))
    fake = FakeCalendar()
    store.plan_now(s, [], NOW)
    count, result = optimizer_cal.approve_and_sync(s, fake, NOW)
    cal_id = store.get_prefs(s).optimizer_calendar_id
    assert fake.calendars_by_id[cal_id]["summary"] == "Optimizer"
    events = fake.optimizer_events(cal_id)
    blocks = store.approved_blocks(s, NOW)
    assert result.inserted == count == len(blocks) == len(events) > 0
    assert all(e["summary"] == "Essay" for e in events)
    assert all(e["extendedProperties"]["private"]["app"] == "schedule-optimizer" for e in events)
    assert all(b.google_event_id for b in store.approved_blocks(s, NOW))
    assert fake.batches == 1  # one HTTP round trip for all inserts


def test_sync_is_idempotent(s):
    store.add_task(s, title="Essay", total_minutes=180)
    plan_and_approve(s)
    fake = FakeCalendar()
    optimizer_cal.sync_approved(fake, s, NOW)
    fake.log.clear()
    again = optimizer_cal.sync_approved(fake, s, NOW)
    assert (again.inserted, again.deleted, again.kept) == (0, 0, len(store.approved_blocks(s, NOW)))
    assert fake.log == []


def test_replan_only_touches_blocks_that_moved(s):
    t = store.add_task(s, title="Essay", total_minutes=240, deadline=at(20, 23))
    plan_and_approve(s)
    fake = FakeCalendar()
    optimizer_cal.sync_approved(fake, s, NOW)
    before = {(e["start"]["dateTime"], e["end"]["dateTime"]) for e in fake.optimizer_events(store.get_prefs(s).optimizer_calendar_id)}

    store.log_progress(s, t.id, 30, NOW)
    later = NOW + timedelta(minutes=30)
    store.plan_now(s, [], later)
    count, result = optimizer_cal.approve_and_sync(s, fake, later)
    after = {(e["start"]["dateTime"], e["end"]["dateTime"]) for e in fake.optimizer_events(store.get_prefs(s).optimizer_calendar_id)}
    assert result.kept >= 1, "re-plan reshuffled every block"
    assert len(before & after) >= result.kept


def test_in_progress_block_is_trimmed_not_deleted(s):
    """Re-planning partway through a block (with the task now done) keeps the part that happened."""
    t = store.add_task(s, title="Essay", total_minutes=60)
    plan_and_approve(s)
    fake = FakeCalendar()
    optimizer_cal.sync_approved(fake, s, NOW)
    cal_id = store.get_prefs(s).optimizer_calendar_id
    [ev] = fake.optimizer_events(cal_id)

    start = datetime.fromisoformat(ev["start"]["dateTime"])
    midway = start + timedelta(minutes=20)
    store.update_task(s, t.id, status="done")
    store.plan_now(s, [], midway)  # nothing left, so the new plan is empty
    _, result = optimizer_cal.approve_and_sync(s, fake, midway)
    assert result.trimmed == 1 and result.deleted == 0
    assert datetime.fromisoformat(fake.events_by_cal[cal_id][ev["id"]]["end"]["dateTime"]) == midway


def test_calendar_is_recreated_if_user_deleted_it(s):
    store.add_task(s, title="Essay", total_minutes=60)
    plan_and_approve(s)
    fake = FakeCalendar()
    optimizer_cal.sync_approved(fake, s, NOW)
    old = store.get_prefs(s).optimizer_calendar_id
    del fake.calendars_by_id[old]
    optimizer_cal.sync_approved(fake, s, NOW)
    new = store.get_prefs(s).optimizer_calendar_id
    assert new != old and fake.optimizer_events(new)


def test_optimizer_calendar_never_counts_as_busy(s):
    store.add_task(s, title="Essay", total_minutes=60)
    plan_and_approve(s)
    fake = FakeCalendar()
    optimizer_cal.sync_approved(fake, s, NOW)
    cal_id = store.get_prefs(s).optimizer_calendar_id
    store.update_prefs(s, busy_calendars=["primary@x", cal_id])  # even if chosen by mistake
    events = calendar_sync.busy_events(s, NOW, service=fake)
    assert events == []  # the plan's own blocks don't block time


def test_breaks_go_on_the_optimizer_calendar(s):
    """A hold (e.g. a break) is written on approve, even with no work planned, and removed when deleted."""
    fake = FakeCalendar()
    hold = store.add_hold(s, "Break: short walk", at(18, 9), at(18, 9, 30))
    store.plan_now(s, [], NOW)  # no tasks, so no blocks
    count, result = optimizer_cal.approve_and_sync(s, fake, NOW)
    cal_id = store.get_prefs(s).optimizer_calendar_id
    [ev] = fake.optimizer_events(cal_id)
    assert count == 0 and result.inserted == 1
    assert ev["summary"] == "Break: short walk" and ev["colorId"] == optimizer_cal.HOLD_COLOR

    count, result = optimizer_cal.approve_and_sync(s, fake, NOW)  # idempotent
    assert (result.inserted, result.deleted, result.kept) == (0, 0, 1)

    store.delete_hold(s, hold.id)
    _, result = optimizer_cal.approve_and_sync(s, fake, NOW)
    assert result.deleted == 1 and fake.optimizer_events(cal_id) == []


def test_breaks_and_blocks_together(s):
    store.add_task(s, title="Essay", total_minutes=120, deadline=at(18, 12))
    store.add_hold(s, "Break", at(18, 9), at(18, 9, 30))
    store.plan_now(s, [], NOW)
    fake = FakeCalendar()
    optimizer_cal.approve_and_sync(s, fake, NOW)
    events = fake.optimizer_events(store.get_prefs(s).optimizer_calendar_id)
    assert {e["summary"] for e in events} == {"Essay", "Break"}
    brk = datetime.fromisoformat(next(e for e in events if e["summary"] == "Break")["start"]["dateTime"])
    for e in events:
        if e["summary"] == "Essay":  # work stays clear of the break and its buffer
            start, end = (datetime.fromisoformat(e[k]["dateTime"]) for k in ("start", "end"))
            assert end + timedelta(minutes=5) <= brk or start >= brk + timedelta(minutes=35)


# ---- approve replaces everything from the solve time on ------------------------------------

def test_block_under_way_at_replan_is_cut_off_not_left_behind(s):
    """Regression: a block that had started when you re-planned stayed on the Optimizer
    calendar after approving, overlapping the new plan."""
    fake = FakeCalendar()
    store.add_task(s, title="PSET", total_minutes=120, deadline=at(19, 23))
    store.plan_now(s, [], NOW)
    optimizer_cal.approve_and_sync(s, fake, NOW)
    [old] = store.approved_blocks(s, NOW)
    cal_id = store.get_prefs(s).optimizer_calendar_id

    mid = old.start + timedelta(minutes=40)  # re-plan 40 minutes into the block
    # Something urgent has to go first, so the PSET block doesn't continue right away.
    store.add_task(s, title="Catchup", total_minutes=60, priority=5, deadline=mid + timedelta(minutes=70))
    store.plan_now(s, [], mid)
    optimizer_cal.approve_and_sync(s, fake, mid)

    events = fake.optimizer_events(cal_id)
    spans = [(datetime.fromisoformat(e["start"]["dateTime"]), datetime.fromisoformat(e["end"]["dateTime"]))
             for e in events]
    for i, (s1, e1) in enumerate(spans):  # nothing on the calendar overlaps
        for s2, e2 in spans[i + 1:]:
            assert e1 <= s2 or e2 <= s1, f"overlap: {s1}-{e1} and {s2}-{e2}"
    trimmed = [e for e in events if e["summary"] == "PSET" and datetime.fromisoformat(e["start"]["dateTime"]) == old.start]
    assert [datetime.fromisoformat(e["end"]["dateTime"]) for e in trimmed] == [mid]  # what happened stays
    # The database agrees: the old block now ends at the re-plan.
    assert [(b.start, b.end) for b in store.approved_blocks(s, mid - timedelta(minutes=1))
            if b.start == old.start] == [(old.start, mid)]


def test_approving_an_empty_replan_clears_the_calendar(s):
    fake = FakeCalendar()
    t = store.add_task(s, title="Essay", total_minutes=120, deadline=at(19, 23))
    store.plan_now(s, [], NOW)
    optimizer_cal.approve_and_sync(s, fake, NOW)
    cal_id = store.get_prefs(s).optimizer_calendar_id
    assert fake.optimizer_events(cal_id)

    store.update_task(s, t.id, status="done")
    store.plan_now(s, [], NOW)  # nothing left to plan
    assert store.has_unapproved_solve(s)
    count, result = optimizer_cal.approve_and_sync(s, fake, NOW)
    assert count == 0 and result.deleted >= 1
    assert fake.optimizer_events(cal_id) == [] and store.approved_blocks(s, NOW) == []
    assert not store.has_unapproved_solve(s)


def test_replan_after_replan_leaves_only_the_latest(s):
    fake = FakeCalendar()
    store.add_task(s, title="A", total_minutes=90, deadline=at(19, 23))
    for minutes in (0, 50, 130):  # three re-plans, each approved
        now = NOW + timedelta(minutes=minutes)
        store.add_task(s, title=f"New {minutes}", total_minutes=60, priority=5, deadline=at(18, 23))
        store.plan_now(s, [], now)
        optimizer_cal.approve_and_sync(s, fake, now)
    cal_id = store.get_prefs(s).optimizer_calendar_id
    future = {(e["summary"], e["start"]["dateTime"], e["end"]["dateTime"]) for e in fake.optimizer_events(cal_id)
              if datetime.fromisoformat(e["end"]["dateTime"]) > now}
    approved = {(t, b.start.isoformat(), b.end.isoformat()) for b in store.approved_blocks(s, now)
                for t in [{x.id: x.title for x in store.list_tasks(s)}[b.task_id]]}
    assert len(future) == len(approved)


def test_same_task_continuing_merges_into_one_block(s):
    """Regression: re-planning 4 minutes into a block left a 5-minute sliver next to the
    same task's new block. If the plan continues the same task, it's one block."""
    fake = FakeCalendar()
    store.add_task(s, title="Catchup", total_minutes=60, priority=5, deadline=at(18, 12))
    store.plan_now(s, [], NOW)
    optimizer_cal.approve_and_sync(s, fake, NOW)
    [old] = store.approved_blocks(s, NOW)
    mid = old.start + timedelta(minutes=4)
    store.plan_now(s, [], mid)
    optimizer_cal.approve_and_sync(s, fake, mid)
    [ev] = fake.optimizer_events(store.get_prefs(s).optimizer_calendar_id)
    [blk] = store.approved_blocks(s, mid)
    assert datetime.fromisoformat(ev["start"]["dateTime"]) == blk.start == old.start
    assert datetime.fromisoformat(ev["end"]["dateTime"]) == blk.end > old.end - timedelta(minutes=1)


def test_short_sliver_of_a_replaced_block_is_dropped(s):
    fake = FakeCalendar()
    store.add_task(s, title="PSET", total_minutes=120, deadline=at(19, 23))
    store.plan_now(s, [], NOW)
    optimizer_cal.approve_and_sync(s, fake, NOW)
    [old] = store.approved_blocks(s, NOW)
    mid = old.start + timedelta(minutes=4)
    store.add_task(s, title="Urgent", total_minutes=60, priority=5, deadline=mid + timedelta(minutes=70))
    store.plan_now(s, [], mid)
    optimizer_cal.approve_and_sync(s, fake, mid)
    events = fake.optimizer_events(store.get_prefs(s).optimizer_calendar_id)
    assert all(datetime.fromisoformat(e["end"]["dateTime"]) - datetime.fromisoformat(e["start"]["dateTime"])
               >= timedelta(minutes=30) for e in events)
    assert old.start not in [b.start for b in store.approved_blocks(s, NOW)]
