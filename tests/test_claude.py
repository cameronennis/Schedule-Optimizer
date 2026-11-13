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
    assert c.rules[1].label == "“Seminar” Wed Nov 18 09:00–11:00"


def test_deadline_alone_when_nothing_else_would_help():
    tasks = [TaskInput("t", "Report", remaining_minutes=300, deadline=at(18, 12))]  # 5 h in 4 h
    plan = solve(tasks, [], Preferences(), NOW)
    [c] = diagnose(plan, tasks, [], Preferences(), NOW)
    assert [r.kind for r in c.rules] == ["deadline"]


def test_conflict_from_other_tasks_is_competition():
    tasks = [TaskInput("a", "Top", remaining_minutes=120, priority=5, deadline=at(18, 12)),
             TaskInput("b", "Low", remaining_minutes=120, priority=1, deadline=at(18, 12))]
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
    store.set_event_meta(s, "lec", skippable=True, skip_cost=0.5)
    store.add_task(s, title="Midterm", total_minutes=240, priority=5, deadline=at(18, 12, 30))
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
    assert res["applied"] == ["Hold “Walk” Wed 08:00–08:30"]


# ---- MCP helper over stdio ---------------------------------------------------------------------------

async def test_mcp_server_lists_and_runs_tools(tmp_path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    eng = db.make_engine(tmp_path / "schedule.db")
    with Session(eng) as s:
        store.update_prefs(s, time_zone="America/Los_Angeles")
        store.add_task(s, title="Essay", total_minutes=120)
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
        result = await session.call_tool("solve", {})
        assert not result.is_error
        body = json.loads(result.content[0].text)
        assert body["planned_minutes"] == 120
        bad = await session.call_tool("apply_changes", {"proposal_id": 99})
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
        NS(model="claude-opus-5-5", stop_reason="tool_use", usage=usage(), content=[
            NS(type="text", text="Let me look."),
            NS(type="tool_use", id="t1", name="propose_changes", input=walk),
            NS(type="tool_use", id="t2", name="apply_changes", input={"proposal_id": 1})]),
        NS(model="claude-opus-5-5", stop_reason="end_turn", usage=usage(), content=[
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
    assert client.requests[0]["model"] == "claude-opus-5-5"
    spent = claude_api.month_usage(s).cost_usd
    assert spent == pytest.approx(2 * (1000 * 4 + 200 * 20) / 1e6)


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
