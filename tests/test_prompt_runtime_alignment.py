"""Runtime instruction parity and preferences UI behavior; no external services."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from app.agent import loop, prefilter
from app.agent.deep_search_state import DeepSearchRunState
from app.agent.workflow_router import route_solution
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_runtime(monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    monkeypatch.setattr(loop, "load_history_hint", lambda query: ([], None))
    monkeypatch.setattr(loop, "record_run_trace", lambda *_args: {})
    monkeypatch.setattr(loop, "build_primary_workflow_plan", lambda route: {"calls": []})

    async def initialize(context, messages):
        context.setdefault("_prefilter_state", {
            "enabled": True, "plan_status": "fallback", "phase": "initial",
            "unresolved_constraints": [],
        })

    monkeypatch.setattr(prefilter, "initialize", initialize)
    monkeypatch.setattr(prefilter, "instruction_messages", lambda context: [{
        "role": "system", "content": "CURRENT_PREFILTER_" + context["_prefilter_state"]["phase"],
    }])
    monkeypatch.setattr(prefilter, "summary", lambda context: {
        "enabled": True, "plan_status": context["_prefilter_state"]["plan_status"],
        "recommendation_gate": prefilter.recommendation_gate(context),
    })


def run(client, query="推荐三门课程，周五绝对不能上课"):
    async def collect():
        return [event async for event in loop.run_agent(
            [{"role": "user", "content": query}], client=client, model="fixture",
            user_id="student_fixture", term="2026 Fall", default_term="2026 Fall",
            allowed_query_terms=["2026 Fall"], response_language="zh",
        )]

    return asyncio.run(collect())


def system_text(call):
    return "\n".join(message["content"] for message in call.messages if message["role"] == "system")


def test_normal_requests_rebuild_current_prefilter_evidence(isolated_runtime, monkeypatch):
    def dispatch(name, args, *, context):
        context["_prefilter_state"]["phase"] = "updated_after_tool"
        return {"found": False, "error": "fixture evidence unavailable"}

    monkeypatch.setattr(loop.agent_tools, "dispatch", dispatch)
    client = ScriptedLLMClient(
        tool_response(tool_call("get_policy", {})), text_response("条件仍需确认。"),
    )
    events = run(client)
    assert "CURRENT_PREFILTER_initial" in system_text(client.calls[0])
    assert "CURRENT_PREFILTER_updated_after_tool" in system_text(client.calls[1])
    assert "CURRENT_PREFILTER_initial" not in system_text(client.calls[1])
    assert "not an unconstrained request" in system_text(client.calls[1])
    assert "Public web searches and course GE tags cannot verify" in system_text(client.calls[1])
    assert events[-1]["prefilter"]["recommendation_gate"]["ready"] is False


@pytest.mark.parametrize("reason", ["max_iterations", "max_tool_calls"])
def test_unresolved_fallback_keeps_hints_and_cannot_dispatch_cards(isolated_runtime, monkeypatch, reason):
    dispatched = []

    def dispatch(name, args, *, context):
        dispatched.append(name)
        context["_prefilter_state"]["phase"] = "updated_after_tool"
        return {"found": False}

    monkeypatch.setattr(loop.agent_tools, "dispatch", dispatch)
    monkeypatch.setattr(loop, "MAX_ITERATIONS", 1)
    calls = [tool_call("get_policy", {}, call_id="first")]
    if reason == "max_tool_calls":
        monkeypatch.setattr(loop, "MAX_TOTAL_TOOLS", 1)
        calls.append(tool_call("get_policy", {}, call_id="second", index=1))
    client = ScriptedLLMClient(
        tool_response(*calls),
        # Even a provider emitting an unadvertised tool cannot bypass the gate.
        tool_response(tool_call("propose_recommendation", {"items": [{"course_id": "ICS33"}]})),
    )
    events = run(client)
    fallback = client.calls[-1]
    assert fallback.tools is None
    assert "CURRENT_PREFILTER_updated_after_tool" in system_text(fallback)
    assert "never relaxes or cancels" in system_text(fallback)
    assert "hard conditions remain unresolved" in fallback.messages[-1]["content"]
    assert dispatched == ["get_policy"]
    assert not any(event["type"] == "cards_proposed" for event in events)
    assert next(event for event in events if event["type"] == "limit_reached")["reason"] == reason


def test_continue_preserves_original_request_and_unresolved_gate(isolated_runtime, monkeypatch):
    monkeypatch.setattr(loop, "MAX_ITERATIONS", 1)
    monkeypatch.setattr(loop.agent_tools, "dispatch", lambda *args, **kwargs: {"found": False})
    client = ScriptedLLMClient(
        tool_response(tool_call("get_policy", {})), text_response("还需确认条件。"),
        text_response("请先确认未决条件。"),
    )
    original_query = "只推荐补齐剩余GE的课程"
    initial = run(client, original_query)
    cid = next(event["continuation_id"] for event in initial if event["type"] == "limit_reached")

    async def resume():
        return [event async for event in loop.resume_agent(cid, client=client, model="fixture")]

    resumed = asyncio.run(resume())
    assert "CURRENT_PREFILTER_initial" in system_text(client.calls[-1])
    assert "Continue does not relax or cancel hard conditions" in client.calls[-1].messages[-1]["content"]
    assert any(message.get("content") == original_query for message in client.calls[-1].messages)
    assert resumed[-1]["prefilter"]["recommendation_gate"] == initial[-1]["prefilter"]["recommendation_gate"]
    assert loop.pop_continuation(cid) is None


def test_fallback_includes_workflow_and_history_instructions(isolated_runtime):
    route = route_solution("2026 Fall ICS 专业限制什么时候解除", term="2026 Fall")
    record = {"tool": "get_department_restrictions", "result": {"ok": False, "error_code": "unavailable"}}
    state = DeepSearchRunState(query="2026 Fall ICS 专业限制什么时候解除")
    context = {"_prefilter_state": {
        "enabled": True, "plan_status": "fallback", "phase": "latest", "unresolved_constraints": [],
    }}
    history = {"role": "user", "content": "history data fixture"}
    client = ScriptedLLMClient(text_response("目前无法核实。"))

    async def collect():
        return [event async for event in loop._emit_limit_reached_and_fallback(
            [{"role": "user", "content": state.query}], reason="max_iterations", iterations_used=1,
            tool_calls_used=1, user_id="student_fixture", term="2026 Fall", default_term="2026 Fall",
            allowed_query_terms=["2026 Fall"], query_term_source="explicit", response_language="zh",
            pending_schedule=[], deep_search_state=state, history_hint_message=history,
            client=client, model="fixture", grade_context=context,
            workflow_route=route, primary_results=[record],
        )]

    events = asyncio.run(collect())
    systems = system_text(client.calls[0])
    assert "Developer workflow registry match" in systems
    assert "report structured failures honestly" in systems
    assert "never cached proof" in systems
    assert "CURRENT_PREFILTER_latest" in systems
    assert history in client.calls[0].messages
    assert events[-1]["truncated"] is True
    cid = next(event["continuation_id"] for event in events if event["type"] == "limit_reached")
    loop.pop_continuation(cid)


@pytest.mark.parametrize("query", [
    "只推荐能补齐我剩余 GE 要求的课，给我 3 门建议。",
    "我要修满学分，推荐三门课", "满足毕业要求的课程", "请推荐符合我位置偏好的课",
    "Recommend a full year plan", "I have an open mind about electives",
    "My status is sophomore and I enrolled in ICS31",
])
def test_academic_goals_do_not_trigger_availability_workflow(query):
    assert route_solution(query, term="2026 Fall")["route_type"] == "agentic"


@pytest.mark.parametrize("query", [
    "CS161 现在还有几个位置？", "CS161 还有空位吗？", "CS161 班满了吗？", "CS161满了吗？",
    "Is CS161 full?", "Any open sections for CS161?", "CS161 enrollment status",
    "How many students enrolled in CS161?", "CS161 waitlist",
])
def test_actual_availability_requests_keep_workflow(query):
    assert "websoc_live_availability" in route_solution(query, term="2026 Fall")["workflow_ids"]


@pytest.mark.parametrize("query", [
    "推荐三门ICS课程，同时告诉我院系限制什么时候解除",
    "Recommend ICS electives and explain major restrictions",
    "ICS 专业限制和教授评价怎么样", "ICS 先修要求及专业限制",
])
def test_mixed_tasks_do_not_inherit_two_sentence_restriction_limit(query):
    hint = loop._workflow_instruction_message([{"tool": "get_department_restrictions"}], query)
    assert "at most two short sentences" not in hint["content"]
    assert "report structured failures honestly" in hint["content"]


def test_pure_restriction_keeps_short_verified_fact_explanation():
    hint = loop._workflow_instruction_message(
        [{"tool": "get_department_restrictions"}], "2026 Fall ICS 专业限制什么时候解除？",
    )
    assert "at most two short sentences" in hint["content"]


def test_default_preview_never_overwrites_or_saves_style_extension():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for frontend preferences behavior")
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
const storage = new Map([['testPrompt', 'older saved user preference']]);
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    id, value:'', textContent:'', placeholder:'',
    classList:{add(){},remove(){},contains(){return false;}},
    addEventListener(){},focus(){},
  });
  return elements.get(id);
}
const context = vm.createContext({
  document:{getElementById:element,addEventListener(){}},
  localStorage:{setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  PROMPT_STORAGE_KEY:'testPrompt', getActivePrompt:()=>storage.get('testPrompt') || '',
  defaultSystemPrompt:'SERVER RULES ARE READ ONLY',
  loadDefaultPromptFromAPI:async()=>{},setTimeout:fn=>fn(),
});
vm.runInContext(fs.readFileSync('static/js/profile-memory.js','utf8'),context);
(async()=>{
  await context.openSettings();
  assert.equal(element('promptTextarea').value,'older saved user preference');
  await context.loadDefaultPrompt();
  assert.equal(element('defaultPromptPreview').value,'SERVER RULES ARE READ ONLY');
  assert.equal(element('promptTextarea').value,'older saved user preference');
  assert.equal(storage.get('testPrompt'),'older saved user preference');
  context.savePrompt();
  assert.equal(storage.get('testPrompt'),'older saved user preference');
  context.defaultSystemPrompt='';
  await context.loadDefaultPrompt();
  assert.match(element('defaultPromptPreview').value,/unavailable/);
  assert.equal(element('promptTextarea').value,'older saved user preference');
  element('promptTextarea').value='Use concise Chinese';context.savePrompt();
  assert.equal(storage.get('testPrompt'),'Use concise Chinese');
  context.clearPrompt();context.savePrompt();assert.equal(storage.has('testPrompt'),false);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    index = (ROOT / "static/index.html").read_text()
    assert 'id="defaultPromptPreview"' in index
    assert 'aria-label="Server rules, read only" readonly' in index
    assert 'onclick="loadDefaultPrompt()">Load default' not in index


def _selection_card():
    return {
        "course_id": "STATS 67", "term": "2026 Fall", "primary_code": "36970",
        "requires_secondary": False, "secondary_type": None, "ge_category": "Va",
        "prefilter": {"status": "needs_verification", "warnings": [
            {"code": "secondary_pairing_unknown"}, {"code": "ge_labels_unknown"},
        ]},
        "ranking": {"final_score": None, "features": {
            "grade_a_rate": {"scope": "instructor", "valid": False},
        }},
        # Group order is deliberately different from the actual primary selection.
        "section_groups": [
            {"primary": {"code": "36980", "num": "B", "days": "MW", "time": "11:00–12:20"}},
            {"primary": {"code": "36970", "num": "A", "days": "MW", "time": "09:30–10:50"}, "secondaries": []},
        ],
    }


def test_card_facts_preserve_selection_unknowns_scope_and_latest_term():
    context = {}
    card = _selection_card()
    other_term = {**card, "course_id": "OLD 1", "term": "2025 Fall"}
    loop._record_recommendation_card_facts(context, [card, other_term], "Fall 2026")
    packet = context["_recommendation_card_facts"]
    assert packet["term"] == "2026 Fall"
    assert len(packet["cards"]) == 1
    row = packet["cards"][0]
    assert row["primary_code"] == row["selected_lecture"]["code"] == "36970"
    assert row["selected_lecture"]["time"] == "09:30–10:50"
    assert row["alternative_lectures"][0]["code"] == "36980"
    assert row["required_secondary"] == "unknown"  # Empty filtered list is not no-Dis proof.
    assert "ge_labels_unknown" in row["unresolved"]
    assert row["ranking"]["final_score"] is None
    assert row["ranking"]["feature_scopes"]["grade_a_rate"]["scope"] == "instructor"
    assert len(json.dumps(packet, ensure_ascii=False)) < 5000
    snapshot = loop._grade_snapshot(context)
    context["_recommendation_card_facts"]["cards"].clear()
    assert len(snapshot["_recommendation_card_facts"]["cards"]) == 1
    replacement = {**card, "course_id": "NEW 2"}
    loop._record_recommendation_card_facts(context, [replacement], "2026 Fall")
    assert [item["course_id"] for item in context["_recommendation_card_facts"]["cards"]] == ["NEW 2"]


@pytest.mark.parametrize("budget,late_cards", [(False, False), (True, False), (True, True)])
def test_actual_staged_card_facts_reach_normal_fallback_and_continue(isolated_runtime, monkeypatch, budget, late_cards):
    async def initialize(context, messages):
        context.setdefault("_prefilter_state", {
            "enabled": True, "plan_status": "validated", "phase": "initial", "unresolved_constraints": [],
        })

    monkeypatch.setattr(prefilter, "initialize", initialize)
    monkeypatch.setattr(loop.agent_tools, "prepare_recommendation_grades", lambda *args: None)

    def dispatch(name, args, *, context):
        if name == "get_policy":
            return {"found": False}
        assert name == "propose_recommendation"
        context["_proposed_cards"] = [_selection_card()]
        context["_recommendation_proposed"] = True
        return {"ok": True, "staged_count": 1, "term": "2026 Fall"}

    monkeypatch.setattr(loop.agent_tools, "dispatch", dispatch)
    if budget:
        monkeypatch.setattr(loop, "MAX_ITERATIONS", 1)
    proposal = tool_response(tool_call("propose_recommendation", {"items": [{"course_id": "STATS67"}]}))
    steps = ([tool_response(tool_call("get_policy", {})), proposal] if late_cards else
             [proposal, text_response("当前卡片为A讲；B讲只能作为替代。")])
    if budget:
        steps.append(text_response("配对和GE标签仍未核实。"))
    client = ScriptedLLMClient(*steps)
    events = run(client)
    if budget:
        cid = next(event["continuation_id"] for event in events if event["type"] == "limit_reached")

        async def collect():
            return [event async for event in loop.resume_agent(cid, client=client, model="fixture")]

        asyncio.run(collect())
    for call in client.calls[2 if late_cards else 1:]:
        system = system_text(call)
        assert "different section must be explicitly described as an alternative" in system
        assert "never claim pure lecture" in system
        assert "ge_labels_unknown" in system and "never say those labels are confirmed" in system
        assert "user_confirmed_product_policy" in system
        assert "policy_id=user_confirmed_discussion_presence" in system
        assert "budget finalization and Continue" in system
        messages = [json.loads(message["content"].split("\n", 1)[1]) for message in call.messages
                    if message["role"] == "user" and '"source": "current recommendation card facts"' in message["content"]]
        assert len(messages) == 1
        assert messages[0]["content"]["cards"][0]["selected_lecture"]["code"] == "36970"
        assert "36970" not in system and "09:30" not in system


def test_policy_runtime_hint_keeps_boundaries_when_enabled_and_preserves_disabled_chain(monkeypatch):
    for enabled in ("true", "false"):
        monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", enabled)
        monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", enabled)
        hints = loop._runtime_hint_messages({})
        text = "\n".join(message["content"] for message in hints if message["role"] == "system")
        if enabled == "false":
            assert "user_confirmed_product_policy" not in text
            continue
        for marker in ("user_confirmed_product_policy", "complete unfiltered official target-term",
                       "listed Dis means discussion required", "no listed Dis means no discussion required",
                       "not a university-wide Registrar registration rule", "related target-term timetable",
                       "TBA is unknown", "ON LINE alone does not establish asynchronous", "every required Dis/other component"):
            assert marker in text


def test_related_co_class_timetable_is_a_new_scope_not_a_repeated_completed_primary():
    hint = loop._workflow_instruction_message([{"tool": "get_sections", "args": {"course_id": "SOCIOL1"}}])
    assert "same completed course, term and arguments" in hint["content"]
    assert "related course's target-term timetable" in hint["content"]
    assert "new lookup scope" in hint["content"]


def test_card_facts_use_selected_verified_policy_instead_of_legacy_boolean():
    card = _selection_card()
    # A verified selected rule can resolve the policy; stale course-wide or
    # legacy boolean fields cannot replace its facts.
    card["pairing"] = {"version": 2, "course_id": "STATS67", "term": "2026 Fall",
                       "evidence": {"status": "verified", "source": "registrar_websoc", "course_id": "STATS67", "term": "2026 Fall",
                                    "policy_source": "user_confirmed_product_policy", "policy_id": "user_confirmed_discussion_presence",
                                    "policy_version": 1, "user_confirmed": True, "needs_lookup": True,
                                    "co_class_references": [{"course_id": "COMPSCI1", "term": "2026 Fall", "relation": "co_class"}]},
                       "per_primary": {"36970": {"requirement": "required", "relationship": "unknown",
                                                 "required_components": [{"types": ["Dis"], "min": 1, "max": 1}]}}}
    card["pairing_source_proof"] = dict.fromkeys(("full_unfiltered_request", "response_identity_validated",
                                                "response_document_complete", "source_rows_fully_parsed"), True)
    context = {}
    loop._record_recommendation_card_facts(context, [card], "2026 Fall")
    fact = context["_recommendation_card_facts"]["cards"][0]
    assert fact["required_secondary"] == "required"
    assert fact["pairing"]["policy_source"] == "user_confirmed_product_policy"
    assert fact["pairing"]["policy_id"] == "user_confirmed_discussion_presence"
    assert fact["pairing"]["needs_lookup"] is True and fact["pairing"]["official_table_complete"] is True
    assert fact["pairing"]["co_class_references"][0]["course_id"] == "COMPSCI1"
    card["term"] = "2027 Fall"
    loop._record_recommendation_card_facts(context, [card], "2027 Fall")
    assert context["_recommendation_card_facts"]["cards"][0]["required_secondary"] == "unknown"
