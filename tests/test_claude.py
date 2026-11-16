"""Phase 7: conflict diagnosis, proposals, the shared Claude tools, and the MCP helper."""

import json
import os
import sys
from datetime import timedelta

import pytest
from sqlmodel import Session

from schedule_optimizer import changes, claude_tools, db, store
from schedule_optimizer.demo import NOW, at
from schedule_optimizer.diagnose import diagnose
from schedule_optimizer.domain import EventInput, Preferences, TaskInput
from schedule_optimizer.solver import solve
from schedule_optimizer.tables import Proposal, ProposalStatus

CTX = claude_tools.Ctx(source="test", now=lambda: NOW)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("SCHEDULE_OPTIMIZER_OFFLINE", "1")  # never touch the real Google account


@pytest.fixture
def s(tmp_path):
    eng = db.make_engine(tmp_path / "schedule.db")
    with Session(eng, expire_on_commit=False) as session:
        store.update_prefs(session, time_zone="America/Los_Angeles")
        yield session
    eng.dispose()


# ---- diagnosis ----------------------------------------------------------------------------

def test_conflict_names_the_deadline_and_the_event_in_the_way():
    seminar = EventInput("sem", "Seminar", at(18, 9), at(18, 11))
    # 200 min by noon: without the seminar there would be time (8:00–12:00), with it there isn't.
    tasks = [TaskInput("t", "Report", remaining_minutes=200, deadline=at(18, 12))]
    prefs = Preferences()
    plan = solve(tasks, [seminar], prefs, NOW)
    [c] = diagnose(plan, tasks, [seminar], prefs, NOW)
    assert c.kind == "rules" and c.unmet_minutes == plan.unmet_minutes["t"] > 0
    assert [(r.kind, r.event_ids) for r in c.rules] == [("deadline", []), ("event", ["sem"])]
    assert c.rules[1].label == "“Seminar” Wed, Nov 18, 9:00–11:00 AM"


def test_deadline_alone_when_nothing_else_would_help():
    tasks = [TaskInput("t", "Report", remaining_minutes=300, deadline=at(18, 12))]  # 5 h in 4 h
    plan = solve(tasks, [], Preferences(), NOW)
    [c] = diagnose(plan, tasks, [], Preferences(), NOW)
    assert [r.kind for r in c.rules] == ["deadline"]


def test_conflict_from_other_tasks_is_competition():
    tasks = [TaskInput("a", "Top", remaining_minutes=120, priority=1, deadline=at(18, 12)),
             TaskInput("b", "Low", remaining_minutes=120, priority=5, deadline=at(18, 12))]
    prefs = Preferences()
    plan = solve(tasks, [], prefs, NOW)
    assert "b" in plan.unmet_minutes
    [c] = diagnose(plan, tasks, [], prefs, NOW)
    assert c.kind == "competition" and c.competing == {"Top": 120}


def test_more_work_than_the_horizon_holds_is_too_big():
    tasks = [TaskInput("t", "Thesis", remaining_minutes=200 * 60, deadline=NOW + timedelta(days=6))]
    prefs = Preferences()
    plan = solve(tasks, [], prefs, NOW)
    [c] = diagnose(plan, tasks, [], prefs, NOW)
    assert c.kind == "too_big"


def test_plan_now_saves_conflicts_and_events(s):
    store.add_task(s, title="Report", total_minutes=200, deadline=at(18, 12))
    seminar = EventInput("sem", "Seminar", at(18, 9), at(18, 11))
    store.plan_now(s, [seminar], NOW)
    row = store.last_solve(s)
    assert [e["id"] for e in row.events] == ["sem"]
    [c] = row.conflicts
    assert c["kind"] == "rules"
    assert "can't all hold" in claude_tools.conflict_text(c) and "Seminar" in claude_tools.conflict_text(c)


# ---- proposals --------------------------------------------------------------------------------

def test_propose_previews_without_changing_anything(s):
    task = store.add_task(s, title="Essay", total_minutes=600, deadline=at(18, 12))
    store.plan_now(s, [], NOW, explain=False)
    before = store.proposed_blocks(s)
    row = changes.propose(s, [{"op": "update_task", "task_id": task.id, "deadline": "2026-11-20"}],
                          "Give the essay until Friday", [], NOW)
    assert row.status == ProposalStatus.PENDING
    assert row.preview["before"]["unmet"]["Essay"] > 0
    assert row.preview["after"]["unmet"] == {}  # fits with the later deadline
    assert s.get(type(task), task.id).deadline == at(18, 12)  # real task unchanged
    assert [(b.start, b.end) for b in store.proposed_blocks(s)] == [(b.start, b.end) for b in before]


def test_apply_proposal_changes_inputs_and_replans(s):
    task = store.add_task(s, title="Essay", total_minutes=600, deadline=at(18, 12))
    row = changes.propose(s, [{"op": "update_task", "task_id": task.id, "deadline": "2026-11-20"},
                              {"op": "add_hold", "title": "Walk", "start": "2026-11-18T12:00", "end": "2026-11-18T12:30"}],
                          "", [], NOW)
    done, plan = changes.apply_proposal(s, row.id, [], NOW)
    assert len(done) == 2 and not plan.unmet_minutes
    assert s.get(type(task), task.id).deadline == at(21, 0)  # end of Friday the 20th
    assert all(b.end + timedelta(minutes=5) <= at(18, 12) or b.start >= at(18, 12, 35) for b in plan.blocks)
    assert s.get(Proposal, row.id).status == ProposalStatus.APPLIED
    with pytest.raises(ValueError, match="already applied"):
        changes.apply_proposal(s, row.id, [], NOW)


def test_bad_change_is_rejected_and_nothing_is_saved(s):
    task = store.add_task(s, title="Essay", total_minutes=60)
    with pytest.raises(ValueError, match="change 2"):
        changes.propose(s, [{"op": "update_task", "task_id": task.id, "priority": 4},
                            {"op": "update_task", "task_id": task.id, "priority": 9}], "", [], NOW)
    assert changes.pending(s) == []
    assert s.get(type(task), task.id).priority == 3


def test_dismiss(s):
    store.add_task(s, title="Essay", total_minutes=60)
    row = changes.propose(s, [{"op": "update_preferences", "max_focus_hours": 4}], "Lighter days", [], NOW)
    changes.dismiss_proposal(s, row.id)
    assert changes.pending(s) == []
    assert store.get_prefs(s).max_focus_minutes_per_day == 360


# ---- shared tools ---------------------------------------------------------------------------------

def test_tools_read_plan_and_explain_a_block(s):
    lecture = EventInput("lec", "Lecture", at(18, 9), at(18, 10))
    store.set_event_meta(s, "lec", skippable=True, skip_for=5)  # any work may take its place
    store.add_task(s, title="Midterm", total_minutes=240, priority=1, deadline=at(18, 12, 30))
    store.plan_now(s, [lecture], NOW)
    plan = claude_tools.run_tool(s, "get_plan", {}, CTX)
    assert plan["plan_state"].startswith("proposed")
    assert plan["events"][0]["event_id"] == "lec" and plan["events"][0]["attend"] is False
    block = plan["blocks"][0]
    why = claude_tools.run_tool(s, "explain_block", {"block_id": block["block_id"]}, CTX)
    assert why["score_terms"][0]["term"].startswith("value")
    assert any("Lecture" in x for x in why.get("uses_time_from_skipped_events", [])) or len(plan["blocks"]) > 1
    tasks = claude_tools.run_tool(s, "get_tasks", {}, CTX)["tasks"]
    assert tasks[0]["remaining_minutes"] == 240


def test_propose_then_apply_through_tools(s):
    out = claude_tools.run_tool(s, "create_task", {"title": "Read ch. 4", "hours": 1.5}, CTX)
    assert "Read ch. 4" in out["created"]
    prop = claude_tools.run_tool(s, "propose_changes", {
        "summary": "Take a break now", "changes": [{"op": "add_hold", "title": "Walk",
                                                     "start": "2026-11-18T08:00", "end": "2026-11-18T08:30"}]}, CTX)
    assert prop["after"]["blocks"][0]["start"] >= "2026-11-18T08:35"
    res = claude_tools.run_tool(s, "apply_changes", {"proposal_id": prop["proposal_id"]}, CTX)
    assert res["applied"] == ["Hold “Walk” Wed, Nov 18, 8:00–8:30 AM"]


# ---- MCP helper over stdio ---------------------------------------------------------------------------

async def test_mcp_server_lists_and_runs_tools(tmp_path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    eng = db.make_engine(tmp_path / "schedule.db")
    with Session(eng) as s:
        store.update_prefs(s, time_zone="America/Los_Angeles")
        store.add_task(s, title="Essay", total_minutes=120)
        code = store.new_chat_code(s).code
    eng.dispose()

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "schedule_optimizer.mcp_server"],
        env={**os.environ, "SCHEDULE_OPTIMIZER_DATA_DIR": str(tmp_path), "SCHEDULE_OPTIMIZER_OFFLINE": "1"},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        names = {t.name for t in (await session.list_tools()).tools}
        assert names == {"get_plan", "get_tasks", "create_task", "propose_changes", "apply_changes",
                         "solve", "explain_block"}
        result = await session.call_tool("solve", {"chat_code": code})
        assert not result.is_error
        body = json.loads(result.content[0].text)
        assert body["planned_minutes"] == 120
        bad = await session.call_tool("apply_changes", {"chat_code": code, "proposal_id": 99})
        assert bad.is_error and "no proposal" in bad.content[0].text


# ---- API-key mode loop (fake client: no network, no billing) ---------------------------------

from types import SimpleNamespace as NS  # noqa: E402

from schedule_optimizer import claude_api  # noqa: E402


def usage(i=1000, o=200):
    return NS(input_tokens=i, output_tokens=o, cache_creation_input_tokens=0, cache_read_input_tokens=0)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        return self.responses.pop(0)


def test_api_chat_proposes_but_cannot_apply(s, tmp_path):
    store.add_task(s, title="Essay", total_minutes=120)
    eng = s.get_bind()
    factory = lambda: Session(eng, expire_on_commit=False)  # noqa: E731
    walk = {"summary": "A walk first", "changes": [
        {"op": "add_hold", "title": "Walk", "start": "2026-11-18T08:00", "end": "2026-11-18T08:20"}]}
    client = FakeClient([
        NS(model="claude-sonnet-5-5", stop_reason="tool_use", usage=usage(), content=[
            NS(type="text", text="Let me look."),
            NS(type="tool_use", id="t1", name="propose_changes", input=walk),
            NS(type="tool_use", id="t2", name="apply_changes", input={"proposal_id": 1})]),
        NS(model="claude-sonnet-5-5", stop_reason="end_turn", usage=usage(), content=[
            NS(type="text", text="I suggested a walk; apply it in the app.")]),
    ])
    chat = claude_api.Chat()
    claude_api.send(chat, "I'm tired", factory, lambda: NOW, client=client)
    assert [r for r, _ in chat.log] == ["user", "claude", "tool", "tool", "claude"]
    assert "apply_changes failed" in chat.log[3][1]  # not offered in the app
    assert chat.proposals == [1] and changes.pending(s)[0].summary == "A walk first"
    assert store.list_holds(s) == []  # nothing applied
    tools = {t["name"] for t in client.requests[0]["tools"]}
    assert "apply_changes" not in tools and "propose_changes" in tools
    assert client.requests[0]["model"] == "claude-sonnet-5-5"
    spent = claude_api.month_usage(s).cost_usd
    assert spent == pytest.approx(2 * (1000 * 2 + 200 * 10) / 1e6)  # Sonnet 5.5 prices


def test_api_chat_stops_at_the_monthly_cap(s):
    store.update_prefs(s, claude_monthly_cap_usd=0)
    eng = s.get_bind()
    client = FakeClient([])
    with pytest.raises(claude_api.CapReached):
        claude_api.send(claude_api.Chat(), "hi", lambda: Session(eng), lambda: NOW, client=client)
    assert client.requests == []


def test_solve_tool_reports_optimality_and_accepts_a_longer_limit(s):
    store.add_task(s, title="Essay", total_minutes=60)
    out = claude_tools.run_tool(s, "solve", {"seconds": 10}, CTX)
    assert out["optimal"] is True and "not_fully_optimized" not in out



async def test_installed_helper_does_nothing_without_a_chat_code(tmp_path):
    """Claude is optional: an installed helper refuses every call unless the chat carries an
    active code, so nothing in an unrelated chat can use it. Listing tools records that
    Claude Desktop connected."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    eng = db.make_engine(tmp_path / "schedule.db")
    with Session(eng) as s:
        store.update_prefs(s, time_zone="America/Los_Angeles")
        store.add_task(s, title="Essay", total_minutes=120)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "schedule_optimizer.mcp_server"],
        env={**os.environ, "SCHEDULE_OPTIMIZER_DATA_DIR": str(tmp_path), "SCHEDULE_OPTIMIZER_OFFLINE": "1"},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        assert len((await session.list_tools()).tools) == 7
        with Session(eng) as s:
            assert store.get_prefs(s).desktop_helper_seen_at is not None
        result = await session.call_tool("solve", {})
        assert result.is_error and "isn't active in this chat" in result.content[0].text
        with Session(eng) as s:
            assert store.last_solve(s) is None  # nothing ran
    eng.dispose()


async def test_mcp_server_works_only_in_an_activated_chat(tmp_path):
    """A chat without the pasted code can't use the tools."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    eng = db.make_engine(tmp_path / "schedule.db")
    with Session(eng) as s:
        store.update_prefs(s, time_zone="America/Los_Angeles")
        code = store.new_chat_code(s).code
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "schedule_optimizer.mcp_server"],
        env={**os.environ, "SCHEDULE_OPTIMIZER_DATA_DIR": str(tmp_path), "SCHEDULE_OPTIMIZER_OFFLINE": "1"},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert "ACTIVATE SCHEDULE OPTIMIZER FOR THIS CHAT" in (init.instructions or "")
        tools = (await session.list_tools()).tools
        assert all("chat_code" in t.input_schema["required"] for t in tools)
        for args in ({}, {"chat_code": "ABC-DEF"}):
            r = await session.call_tool("get_plan", args)
            assert r.is_error and "isn't active in this chat" in r.content[0].text
        assert not (await session.call_tool("get_plan", {"chat_code": code.lower()})).is_error
        with Session(eng) as s:
            assert store.revoke_chat_codes(s) == 1
        assert (await session.call_tool("get_plan", {"chat_code": code})).is_error
    eng.dispose()


def test_chat_codes_last_until_ended_one_at_a_time(s):
    a, b = store.new_chat_code(s, now=NOW), store.new_chat_code(s, now=NOW)
    assert store.activation_message(a.code) == f"ACTIVATE SCHEDULE OPTIMIZER FOR THIS CHAT (code {a.code})"
    assert store.chat_code_is_active(s, a.code)  # no expiry, however old
    assert store.use_chat_code(s, b.code, now=NOW + timedelta(days=30))
    assert s.get(type(b), b.code).last_used_at == NOW + timedelta(days=30)
    store.end_chat_code(s, a.code)
    assert not store.chat_code_is_active(s, a.code) and store.chat_code_is_active(s, b.code)
    assert [c.code for c in store.active_chat_codes(s)] == [b.code]
    with pytest.raises(KeyError):
        store.end_chat_code(s, a.code)



# ---- Claude Desktop config: "Add it for me" with a saved copy -------------------------------

from schedule_optimizer import desktop_setup  # noqa: E402


@pytest.fixture
def claude_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEDULE_OPTIMIZER_DATA_DIR", str(tmp_path / "data"))
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    monkeypatch.setenv("SCHEDULE_OPTIMIZER_CLAUDE_CONFIG", str(path))
    return path


def test_add_keeps_other_settings_and_undo_restores_exactly(claude_config):
    claude_config.parent.mkdir(parents=True)
    original = '{\n  "mcpServers": {"weather": {"command": "w"}},\n  "theme": "dark"\n}\n'
    claude_config.write_text(original)
    desktop_setup.add_entry()
    data = json.loads(claude_config.read_text())
    assert data["theme"] == "dark" and "weather" in data["mcpServers"]
    assert data["mcpServers"]["schedule-optimizer"]["args"][-1] == "schedule-optimizer-mcp"
    assert desktop_setup.is_configured() and desktop_setup.has_backup()
    desktop_setup.undo()
    assert claude_config.read_text() == original  # byte for byte
    assert not desktop_setup.is_configured() and not desktop_setup.has_backup()


def test_add_creates_a_missing_file_and_undo_removes_it(claude_config):
    desktop_setup.add_entry()
    assert desktop_setup.is_configured()
    desktop_setup.undo()
    assert not claude_config.exists()


def test_add_refuses_a_broken_file_without_changing_it(claude_config):
    claude_config.parent.mkdir(parents=True)
    claude_config.write_text('{"mcpServers": {')
    with pytest.raises(desktop_setup.ConfigError, match="isn't valid JSON"):
        desktop_setup.add_entry()
    assert claude_config.read_text() == '{"mcpServers": {'
    assert not desktop_setup.has_backup()


def test_manual_snippets_are_valid_json(claude_config):
    whole = json.loads(desktop_setup.snippet())
    entry = json.loads("{" + desktop_setup.entry_snippet() + "}")
    assert whole["mcpServers"] == entry


# ---- done tasks are deleted after 7 days ---------------------------------------------------

def test_done_tasks_are_deleted_after_seven_days(s):
    from datetime import datetime, timezone

    keep = store.add_task(s, title="Recent", total_minutes=60)
    old = store.add_task(s, title="Old", total_minutes=60)
    active = store.add_task(s, title="Active", total_minutes=60)
    store.log_progress(s, old.id, 30, NOW)
    for t in (keep, old):
        store.update_task(s, t.id, status="done")
    assert s.get(type(old), old.id).completed_at is not None
    now = datetime.now(timezone.utc)
    old.completed_at = now - timedelta(days=8)
    s.add(old)
    s.commit()
    assert store.purge_done_tasks(s, now) == 1
    titles = {t.title for t in store.list_tasks(s, include_done=True)}
    assert titles == {"Recent", "Active"}
    assert store.logged_minutes(s, old.id) == 0  # its progress went with it

    store.update_task(s, keep.id, status="active")  # reopening clears the completion time
    assert s.get(type(keep), keep.id).completed_at is None



def test_clipboard_uses_the_system_tool(monkeypatch):
    import subprocess

    from schedule_optimizer import clipboard

    runs = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, input, check, timeout: runs.append((cmd, input)))
    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    assert clipboard.copy("ACTIVATE (code ABC-DEF)")
    assert runs == [(["pbcopy"], b"ACTIVATE (code ABC-DEF)")]

    def fail(*a, **k):
        raise FileNotFoundError("pbcopy")

    monkeypatch.setattr(subprocess, "run", fail)
    assert clipboard.copy("x") is False  # falls back to the browser clipboard in the app


def test_outdated_helper_refuses_to_run(monkeypatch):
    """Regression: Claude Desktop kept an old helper running after an update, and that copy
    ignored chat codes. A helper whose code changed since it started now refuses every call."""
    import anyio

    from schedule_optimizer import mcp_server

    monkeypatch.setattr(mcp_server, "_source_fingerprint", lambda: ("changed",))
    params = mcp_server.types.CallToolRequestParams(name="get_plan", arguments={"chat_code": "ANY-ONE"})
    result = anyio.run(mcp_server.call_tool, None, params)
    assert result.is_error and "Fully quit Claude Desktop" in result.content[0].text


def test_old_code_refuses_newer_data(tmp_path):
    import sqlite3

    path = tmp_path / "schedule.db"
    db.make_engine(path).dispose()
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    con.commit()
    con.close()
    with pytest.raises(db.NewerDataError, match="newer version"):
        db.make_engine(path)



# ---- AM/PM and MM-DD-YYYY ---------------------------------------------------------------

from schedule_optimizer import fmt  # noqa: E402


def test_times_and_dates_read_the_american_way():
    from datetime import time

    assert fmt.clock(time(0, 5)) == "12:05 AM" and fmt.clock(time(12, 0)) == "12:00 PM"
    assert fmt.clock(time(21, 30)) == "9:30 PM"
    assert fmt.span(at(18, 9), at(18, 10, 30)) == "9:00–10:30 AM"
    assert fmt.span(at(18, 11, 30), at(18, 13, 20)) == "11:30 AM–1:20 PM"
    assert fmt.date_text(at(18, 9)) == "11-18-2026" and fmt.day(at(18, 9)) == "Wed, Nov 18"
    assert fmt.deadline(at(19, 0)) == "11-18-2026, end of day"
    assert fmt.deadline(at(19, 9)) == "11-19-2026 9:00 AM"
    for text in ("9:30 PM", "9:30pm", "9:30 p.m.", "21:30"):
        assert fmt.parse_clock(text) == time(21, 30)
    assert fmt.parse_clock("12 am") == time(0, 0) and fmt.parse_clock("12pm") == time(12, 0)
    assert fmt.parse_date("11-19-2026") == fmt.parse_date("11/19/2026") == fmt.parse_date("2026-11-19")
    with pytest.raises(ValueError):
        fmt.parse_clock("13:00 PM")
    assert fmt.time_options()[:2] == ["12:00 AM", "12:15 AM"] and len(fmt.time_options()) == 96



# ---- skip-for thresholds ----------------------------------------------------------------

def test_skip_threshold_lets_only_allowed_priorities_take_the_time():
    """A lecture skippable for priority 1–2 work is skipped for priority 2 work, never for priority 3."""
    from schedule_optimizer.domain import skip_cost_for

    def plan_with(priority):
        lec = EventInput("lec", "Lecture", at(18, 9), at(18, 10), skippable=True,
                         skip_cost=skip_cost_for(2, 60), skip_for=2)
        tasks = [TaskInput("t", "Paper", remaining_minutes=180, priority=priority, deadline=at(18, 11))]
        return solve(tasks, [lec], Preferences(), NOW)

    assert [e.attend for e in plan_with(2).events] == [False]  # skipped for priority 2
    assert [e.attend for e in plan_with(3).events] == [True]  # kept for priority 3
    assert skip_cost_for(5, 60) < 2 < skip_cost_for(4, 60) < 4  # "any work" costs less than priority 5 earns


def test_old_skip_costs_are_cleared_on_upgrade(tmp_path):
    import sqlite3

    path = tmp_path / "schedule.db"
    eng = db.make_engine(path)
    with Session(eng) as s:
        store.add_title_rule(s, "Lecture", skip_for=2)
        store.set_event_meta(s, "e1", skippable=True)
        store.set_event_meta(s, "e2", skippable=False)
    eng.dispose()
    con = sqlite3.connect(path)
    con.execute("PRAGMA user_version = 8")  # a file from before thresholds
    con.commit()
    con.close()
    eng = db.make_engine(path)
    with Session(eng) as s:
        assert store.list_title_rules(s) == []  # old point costs aren't converted
        assert set(store.event_metas(s)) == {"e2"}  # fixed settings stay
    eng.dispose()
