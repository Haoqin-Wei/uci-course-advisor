"""A term plan the model asks for after reading the conversation (plan_term)."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import BackgroundTasks

from app.agent import graduation, loop, prefilter, tools
from app.data import sessions
from app.llm import adapter
from app.llm.safety import student_turns, untrusted_context_message
from app.recommendations.filter_plan import empty_filter_plan, validate_filter_plan
from app.recommendations.presentation import plan_review_text
from app.routers import chat
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response

TERM = "2027 Winter"
PROFILE = {"program_id": "BS-193", "completed_courses": ["I&C SCI 33"], "in_progress_courses": []}
FRIDAY = "周五绝对不能上课"


def test_student_turns_are_what_the_student_typed_not_context_data_in_the_user_role():
    messages = [{"role": "system", "content": "rules"},
                untrusted_context_message("persistent student profile", "优先保 GPA，周五绝对不能上课"),
                {"role": "user", "content": "推荐一下下学期的选课"}, {"role": "assistant", "content": "…"},
                untrusted_context_message("retrieved data for this turn", "x"),
                {"role": "user", "content": "重新排"}]
    assert student_turns(messages) == ["推荐一下下学期的选课", "重新排"]


@pytest.fixture
def planning(monkeypatch):
    """Profile, pool building and the condition parser replaced by recorders."""
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    seen = {"built": 0, "parsed": [], "profile": dict(PROFILE), "tray": True, "parser_fails": False}
    monkeypatch.setattr(prefilter.db, "get_student_profile",
                        lambda _user: {"found": bool(seen["profile"]), "profile": seen["profile"]})

    def build(context):
        seen["built"] += 1
        state = context["_prefilter_state"]
        if seen["tray"]:
            term = state["term_plan"]["term"]
            state["candidates"][(term, "I&C SCI 46")] = {"course_id": "I&C SCI 46", "sections": [
                {"section_code": "35660", "section_type": "Lec", "days": "MW", "start_time": "15:30", "end_time": "16:50"}]}
            state["graduation_path"] = {
                "term": state["term_plan"]["term"], "required_now": [{"course_id": "I&C SCI 46"}],
                "suggested_plan": {"courses": [{"course_id": "I&C SCI 46", "tier": "required"}]},
                "reasons": {"I&C SCI 46": {"tier": "required"}}}

    async def parse(text, _context=None, profile=None):
        seen["parsed"].append(text)
        if seen["parser_fails"]:
            return {"plan": empty_filter_plan(), "status": "fallback", "issues": ["model_timeout"], "metrics": {}}
        raw = {"hard_filters": {"excluded_weekdays": ["Fri"]} if "周五" in text else
               {"earliest_start": "10:00", "earliest_start_is_hard": True},
               "explicit_constraints": [{"field": "excluded_weekdays" if "周五" in text else "earliest_start",
                                         "quote": next(line for line in text.split("\n") if "周五" in line or "10" in line),
                                         "source": "user_message"}]}
        return {**validate_filter_plan(raw, text, profile), "metrics": {}}

    monkeypatch.setattr(prefilter, "_build_pools", build)
    monkeypatch.setattr("app.llm.filter_plan.parse_filter_plan", parse)
    return seen


def turn(message, *earlier):
    return {"user_id": "student", "term": TERM, "allowed_query_terms": [TERM], "response_language": "zh",
            "user_query": message, "query_term_source": "followup", "_student_turns": [*earlier, message]}


def start(context, conditions=None, term=TERM):
    outcome = asyncio.run(prefilter.start_term_plan(context, term, conditions))
    return outcome["not_found"], outcome["ignored"]


@pytest.mark.parametrize("message", ["重新排", "把 SOC SCI 4A 换成别的 GE", "那少修一门呢", "推荐几门 COMPSCI 的课"])
def test_a_follow_up_gets_a_tray_whatever_its_wording_and_without_the_parser(planning, message):
    context = turn(message, "推荐一下下学期的选课", "转的是物理7D和7LD")
    assert start(context) == ([], [])
    state = context["_prefilter_state"]
    assert planning["parsed"] == [] and planning["built"] == 1
    assert state["plan"] == empty_filter_plan() and state["term_plan"] == {"term": TERM, "carried": []}
    assert prefilter.recommendation_gate(context)["ready"] is True
    assert graduation.tray(context, TERM)["required_now"] == [{"course_id": "I&C SCI 46"}]
    # A GE word or a named course in the follow-up does not turn the plan into a GE list.
    assert state["ge_scope_requested"] is False and state["requested_plan"]["course_scope"] == "any"


def test_a_rule_from_an_earlier_message_is_carried_only_in_the_students_own_words(planning):
    earlier = "帮我排下学期的课，" + FRIDAY
    context = turn("重新排", earlier)
    assert start(context, [FRIDAY]) == ([], [])
    state = context["_prefilter_state"]
    assert planning["parsed"] == [FRIDAY]                       # the quote alone; "重新排" states no rule
    assert state["plan"]["hard_filters"]["excluded_weekdays"] == ["Fri"]
    assert state["term_plan"]["carried"] == [FRIDAY] and prefilter.recommendation_gate(context)["ready"] is True

    paraphrased = turn("重新排", earlier)
    assert start(paraphrased, ["周五不上课", "帮我排下学期的课"]) == (["周五不上课"], ["帮我排下学期的课"])
    assert paraphrased["_prefilter_state"]["plan"] == empty_filter_plan()
    assert planning["parsed"] == [FRIDAY]                       # neither reached the parser

    # Words that only appear in profile or memory data are not the student's.
    assert start(turn("重新排", "推荐一下下学期的选课"), [FRIDAY])[0] == [FRIDAY]


def test_a_rule_in_the_current_message_is_read_by_the_server_without_being_quoted(planning):
    message = "重新排，10点之前绝对不能有课"
    context = turn(message, "推荐一下下学期的选课")
    assert start(context, ["10点之前绝对不能有课"]) == ([], [])     # quoting the current message changes nothing
    assert planning["parsed"] == [message]
    hard = context["_prefilter_state"]["plan"]["hard_filters"]
    assert (hard["earliest_start"], hard["earliest_start_is_hard"]) == ("10:00", True)


def test_a_rule_the_parser_cannot_read_stops_the_plan_instead_of_dropping_the_rule(planning):
    planning["parser_fails"] = True
    context = turn("重新排，" + FRIDAY)
    start(context)
    gate = prefilter.recommendation_gate(context)
    assert gate["ready"] is False and "filter_plan_unavailable" in gate["reasons"]
    assert planning["built"] == 0 and graduation.tray(context, TERM) is None


def test_the_tray_is_reused_within_a_turn_and_rebuilt_for_a_new_rule_or_term(planning):
    context = turn("重新排", "帮我排课，" + FRIDAY)
    start(context)
    start(context)
    assert planning["built"] == 1
    start(context, FRIDAY)                                         # one quote given as a bare string
    assert planning["built"] == 2 and context["_prefilter_state"]["term_plan"]["carried"] == [FRIDAY]
    again = call(context, conditions=[FRIDAY])
    assert planning["built"] == 2 and again["ok"] is True and again["tray"].startswith("unchanged")
    assert again["time_rules_applied"]["excluded_weekdays"] == ["Fri"]
    context["allowed_query_terms"].append("2027 Spring")
    start(context, [FRIDAY], term="2027 Spring")
    assert planning["built"] == 3 and graduation.tray(context, "2027 Spring")["term"] == "2027 Spring"
    # A state the server opened itself for a request it recognised, with no tray, is replaced.
    recognised = turn("推荐几门 COMPSCI 的课")
    recognised["_prefilter_state"] = {"enabled": True, "plan": empty_filter_plan(), "ge_scope_requested": True}
    start(recognised)
    assert recognised["_prefilter_state"]["term_plan"]["term"] == TERM and planning["built"] == 4


def test_the_model_asking_for_a_plan_overrides_the_wording_check_but_not_a_missing_record(monkeypatch):
    monkeypatch.setattr(graduation, "compute", lambda state, term, *, program_id: {"term": term, "by": program_id})
    state = {"profile": dict(PROFILE), "plan": empty_filter_plan()}
    context = {"user_query": "把 SOC SCI 4A 换成别的 GE", "_prefilter_state": state}
    assert graduation.build(context, TERM) is None                              # recognised request: a course is named
    state["term_plan"] = {"term": TERM, "carried": []}
    assert graduation.build(context, TERM) == {"term": TERM, "by": "BS-193"}
    state["profile"] = {"program_id": "BS-193", "completed_courses": []}
    assert graduation.build(context, TERM) is None


def call(context, **args):
    """The tool as the loop runs it: argument guard, dispatch, then the awaited handler."""
    result = tools.dispatch("plan_term", args, context=context)
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


def test_the_tool_returns_the_tray_and_leaves_the_answer_to_the_model_until_it_proposes(planning):
    context = turn("重新排", "帮我排课，" + FRIDAY)
    result = call(context, conditions=[FRIDAY, "周五不上"])
    assert result["ok"] is True and result["term"] == TERM
    assert result["tray"]["required_now"] == [{"course_id": "I&C SCI 46"}] and "reasons" not in result["tray"]
    assert result["time_rules_applied"] == {"earliest_start": None, "excluded_weekdays": ["Fri"]}
    assert result["conditions_not_found"] == ["周五不上"]
    # Asking about the plan stays a conversation: nothing is forced until courses are proposed.
    assert "_controlled_recommendation" not in context
    assert prefilter.guard_call("search_courses", {"term": TERM}, context)[1]["error_code"] == "tray_is_the_candidate_set"

    outside = {"course_id": "EARTHSS 3", "category": "elective", "reason": "x"}
    # In a follow-up a pick outside the tray is not refilled from the server's plan,
    # which may hold the very course the student asked to swap out.
    refused = graduation.review_proposal(context, {"items": [outside]}, TERM)[1]
    assert refused["error_code"] == "no_pick_in_tray" and refused["removed_outside_tray"] == ["EARTHSS 3"]
    assert "_controlled_recommendation" not in context
    args, refused = graduation.review_proposal(
        context, {"items": [outside, {"course_id": "I&C SCI 46", "category": "core", "reason": "x"}]}, TERM)
    assert refused is None and [item["course_id"] for item in args["items"]] == ["I&C SCI 46"]
    assert context["_plan_review"]["follow_up"] is True and context["_plan_review"]["substituted"] == []
    assert context["_plan_review"]["removed_outside_tray"] == ["EARTHSS 3"]
    assert context["_controlled_recommendation"] is True and context["_controlled_recommendation_cards"] == []
    assert context["_plan_review"]["time_rules"] == {"earliest_start": None, "excluded_weekdays": ["Fri"]}


def test_without_a_major_or_recorded_courses_the_turn_goes_on_as_before(planning):
    planning["profile"] = {"program_id": None, "completed_courses": ["I&C SCI 33"]}
    context = turn("重新排")
    before = dict(context)
    result = call(context)
    assert (result["ok"], result["available"], result["reason_code"]) == (False, False, "no_program")
    assert context == before and planning["built"] == 0
    planning["profile"] = {"program_id": "BS-193", "completed_courses": []}
    assert call(turn("重新排"))["reason_code"] == "no_recorded_courses"


def test_no_tray_leaves_no_state_and_an_unreadable_rule_keeps_the_turn_from_proposing(planning):
    planning["tray"] = False
    context = turn("重新排")
    assert call(context)["reason_code"] == "tray_not_built" and "_prefilter_state" not in context

    planning.update(tray=True, parser_fails=True)
    blocked = turn("重新排，" + FRIDAY)
    result = call(blocked)
    assert result["error_code"] == "conditions_unresolved" and result["recommendation_gate"]["ready"] is False
    refused = prefilter.guard_call("propose_recommendation", {"items": [{"course_id": "I&C SCI 46"}], "term": TERM}, blocked)[1]
    assert refused["error"] == "recommendation conditions remain unresolved"
    assert "_controlled_recommendation" not in blocked


def test_the_tool_is_offered_scoped_to_the_turns_term_and_never_replayed_from_cache(planning):
    schema = next(item["function"] for item in tools.TOOL_SCHEMAS if item["function"]["name"] == "plan_term")
    assert set(schema["parameters"]["properties"]) == {"term", "conditions"} and "required" not in schema["parameters"]
    assert "plan_term" in tools.TERM_SCOPED_TOOLS and "plan_term" not in loop.IDEMPOTENT_TERM_READ_TOOLS
    assert "conditions_unresolved" in loop._FAILURES_NOT_WORTH_REPEATING
    context = turn("重新排")
    assert call(context)["term"] == TERM                          # the guard fills in the turn's term
    assert "outside the allowed query-term scope" in call(turn("重新排"), term="2026 Fall")["error"]
    assert tools.humanize_tool_call("plan_term", {"term": TERM}, "zh") == "核对毕业进度 · 2027年冬季"
    assert tools.humanize_tool_call("plan_term", {"term": TERM}, "en").startswith("Checking your degree progress")


def test_the_answer_names_the_time_rules_in_force():
    review = {"courses": ["I&C SCI 46"], "changed": False,
              "time_rules": {"earliest_start": "10:00", "excluded_weekdays": ["Fri", "Mon"]}}
    assert plan_review_text(review, "zh") == "这份计划按你提的时间条件排：周五、周一不排课；10:00 之前不排课。"
    assert plan_review_text(review, "en") == ("This plan keeps to the time conditions you gave: "
                                              "no classes on Friday, Monday; nothing before 10:00.")
    assert plan_review_text({"courses": ["X"], "time_rules": {"earliest_start": None, "excluded_weekdays": []}}) is None


# ── the loop: the answer becomes the server's once a plan is proposed ────────────────

PROSE = "好的，我按最新档案重新排。"
CARD = {"course_id": "I&C SCI 46", "term": TERM, "title": "Data Structures", "units": 4, "primary_code": "35660",
        "graduation_path": {"tier": "required", "unlock_count": 9, "unlocks": ["COMPSCI 141"]},
        "section_groups": [{"letter": "A", "secondaries": [], "primary": {
            "code": "35660", "num": "A", "type": "Lec", "days": "MW", "time": "15:30–16:50", "instructors": ["YEH, T."]}}]}
REVIEW = {"courses": ["I&C SCI 46"], "changed": False, "note": "先拿下数据结构。", "left_out_required": [],
          "time_rules": {"earliest_start": None, "excluded_weekdays": []}}


@pytest.fixture
def scripted_turn(monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "false")
    monkeypatch.setattr(loop, "load_history_hint", lambda *_: ([], None))
    monkeypatch.setattr(loop, "record_run_trace", lambda *_: {})
    monkeypatch.setattr(loop, "build_primary_workflow_plan", lambda *_: {"calls": []})
    monkeypatch.setattr(loop.agent_tools, "prepare_recommendation_grades", lambda *_: [])
    monkeypatch.setattr(loop.agent_tools, "prepare_professor_grades", lambda *_: [])
    calls = []

    def dispatch(name, args, *, context):
        calls.append((name, list(context.get("_student_turns") or [])))
        if name == "plan_term":
            return {"ok": True, "term": TERM, "tray": {"required_now": [{"course_id": "I&C SCI 46"}]}}
        if name == "propose_recommendation":
            # What the tray check does when it accepts a proposal.
            context.update(_plan_review=dict(REVIEW), _controlled_recommendation=True, _proposed_cards=[dict(CARD)])
            context.setdefault("_controlled_recommendation_cards", [])
            return {"ok": True, "staged_count": 1, "term": TERM}
        return {"found": False}

    monkeypatch.setattr(loop.agent_tools, "dispatch", dispatch)
    return calls


def run(client, query="重新排", history=()):
    async def collect():
        return [event async for event in loop.run_agent(
            [*history, {"role": "user", "content": query}], client=client, model="fixture", user_id="demo_001",
            term=TERM, default_term=TERM, allowed_query_terms=[TERM], query_term_source="followup",
            response_language="zh")]
    return asyncio.run(collect())


def proposal():
    return tool_response(tool_call("propose_recommendation", {
        "term": TERM, "note": "先拿下数据结构。",
        "items": [{"course_id": "I&C SCI 46", "category": "core", "reason": "数据结构"}]}))


def test_a_follow_up_the_server_did_not_recognise_ends_as_a_server_written_plan_in_two_model_calls(scripted_turn):
    history = [untrusted_context_message("persistent student profile", "优先保 GPA"),
               {"role": "user", "content": "推荐一下下学期的选课"}, {"role": "assistant", "content": "…"}]
    client = ScriptedLLMClient(tool_response(tool_call("plan_term", {}), leading_tokens=[PROSE]), proposal())
    events = run(client, history=history)
    kinds = [event["type"] for event in events]
    assert kinds[0] != "response_mode"                              # not recognised up front: an ordinary turn
    assert events[kinds.index("token")]["text"] == PROSE           # the model's own words stream until it proposes
    switch = kinds.index("response_mode")
    assert kinds.index("tool_call_done") < switch < kinds.index("cards_proposed")
    assert events[switch]["controlled_recommendation"] is True
    cards = events[kinds.index("cards_proposed")]
    assert cards["controlled_recommendation"] is True and cards["cards"][0]["course_id"] == "I&C SCI 46"
    final = events[-1]
    assert final["type"] == "final" and final["controlled_recommendation"] is True and final["incomplete"] is False
    assert PROSE not in final["text"] and final["text"].startswith("以下按毕业路径推荐")
    assert "先拿下数据结构。" in final["text"] and "**I&C SCI 46**" in final["text"]
    assert len(client.calls) == 2                                   # nothing is asked of the model after the cards
    client.assert_exhausted()
    assert [name for name, _ in scripted_turn] == ["plan_term", "propose_recommendation"]
    assert scripted_turn[0][1] == ["推荐一下下学期的选课", "重新排"]  # the tool sees the student's words only
    offered = {tool["function"]["name"] for tool in client.calls[0].tools}
    assert {"plan_term", "propose_recommendation", "search_courses"} <= offered


def test_a_pick_that_fell_out_gets_the_model_one_more_proposal_and_no_more(scripted_turn, monkeypatch):
    proposals = []

    def dispatch(name, args, *, context):
        if name == "plan_term":
            return {"ok": True, "term": TERM, "tray": {}}
        proposals.append(args)
        fell_out = len(proposals) < 3                    # even a third time, only the first is handed back
        context.update(_plan_review={**REVIEW, "not_staged": [{"course_id": "HISTORY 10", "code": "full"}] if fell_out else []},
                       _controlled_recommendation=True, _proposed_cards=[dict(CARD)],
                       _plan_repick=fell_out and not context.get("_plan_repick_used"))
        context["_plan_repick_used"] = True
        context.setdefault("_controlled_recommendation_cards", [])
        return {"ok": True, "staged_count": 1, "skipped_count": int(fell_out), "term": TERM}

    monkeypatch.setattr(loop.agent_tools, "dispatch", dispatch)
    client = ScriptedLLMClient(tool_response(tool_call("plan_term", {})), proposal(), proposal())
    events = run(client)
    assert len(client.calls) == 3 and len(proposals) == 2          # asked again once, then the server finishes
    client.assert_exhausted()
    final = events[-1]
    assert final["controlled_recommendation"] is True
    assert "**HISTORY 10** 没有排入" in final["text"]               # still unplaced the second time: said, not hidden

    # The model may also answer the hand-back in words; the server's text stands and names what fell out.
    proposals.clear()
    client = ScriptedLLMClient(tool_response(tool_call("plan_term", {})), proposal(), text_response("换不了。"))
    final = run(client)[-1]
    assert "换不了" not in final["text"] and "**HISTORY 10** 没有排入" in final["text"]
    client.assert_exhausted()


def test_asking_about_the_plan_after_plan_term_keeps_the_models_own_answer(scripted_turn):
    answer = "ICS 6B 没排进去，是因为它和 PHYSICS 7D 的讲座时间冲突。"
    client = ScriptedLLMClient(tool_response(tool_call("plan_term", {})), text_response(answer))
    events = run(client, "为什么没有排 ICS 6B？")
    assert all(event["type"] != "response_mode" for event in events)
    assert events[-1]["text"] == answer and "controlled_recommendation" not in events[-1]
    client.assert_exhausted()


def test_the_stored_answer_is_the_servers_text_without_the_words_streamed_before_the_plan(app_client, scripted_turn, monkeypatch):
    client = ScriptedLLMClient(tool_response(tool_call("plan_term", {}), leading_tokens=[PROSE]), proposal())

    async def stream(message, session_state, *, user_id, term, **_kwargs):
        async for event in loop.run_agent(
            [{"role": "user", "content": message}], client=client, model="fixture", user_id=user_id,
            term=TERM, default_term=TERM, allowed_query_terms=[TERM], query_term_source="followup",
            response_language="zh",
        ):
            yield event

    monkeypatch.setattr(adapter, "stream_agent_response", stream)
    sid = sessions.create_session("demo_001", title="Follow-up plan", term_scope=TERM)

    async def collect():
        return [json.loads(raw.removeprefix("data: ").strip()) async for raw in chat._stream_chat(
            chat.ChatRequest(message="重新排", session_id=sid), BackgroundTasks(), user_id="demo_001")]

    events = asyncio.run(collect())
    meta = next(event for event in events if event["type"] == "meta")
    tokens = [event["text"] for event in events if event["type"] == "token"]
    assert tokens[0] == PROSE and tokens[-1] == meta["final_answer"]
    assert PROSE not in meta["final_answer"] and meta["final_answer"].startswith("以下按毕业路径推荐")
    assert meta["controlled_recommendation"] is True and meta["cards"][0]["course_id"] == "I&C SCI 46"
    stored = sessions.read_turns("demo_001", sid)[-1]
    assert stored["content"] == meta["final_answer"] and stored["cards"] == meta["cards"]
    client.assert_exhausted()
