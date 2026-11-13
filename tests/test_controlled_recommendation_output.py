"""Adversarial provider prose never becomes controlled recommendation output."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest
from fastapi import BackgroundTasks

from app.agent import loop, prefilter
from app.data import sessions
from app.llm import adapter
from app.routers import chat
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response


BAD = "BAD_PROVIDER_已保证3门GE_纯Lec只输一个code_B班11点已选_同课差40个百分点"
QUERY = "推荐2026 Fall的简单GE课程"


def _card(course="ICS33", code="36200", num="A", time="09:30–10:50"):
    primary = {"code": code, "num": num, "type": "Lec", "days": "MW", "time": time,
               "instructors": ["LECTURER, A."]}
    return {"course_id": course, "term": "2026 Fall", "title": "Synthetic course", "units": 4,
            "primary_code": code, "reason": BAD, "model_reason": BAD,
            "ge_category": "GE-III", "requires_secondary": None,
            "prefilter": {"enabled": True, "status": "needs_verification", "warnings": [
                {"code": "secondary_pairing_unknown"}, {"code": "ge_labels_unknown"}]},
            "section_groups": [{"letter": num, "primary": primary, "secondaries": []}],
            "sections": [{"section_code": code, "section_num": num, "section_type": "Lec", "days": "MW",
                          "start_time": "09:30" if num == "A" else "11:00", "end_time": "10:50" if num == "A" else "12:20"}]}


@pytest.fixture
def isolated_agent(monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "false")
    monkeypatch.setattr(loop, "load_history_hint", lambda *_: ([], None))
    monkeypatch.setattr(loop, "record_run_trace", lambda *_: {})
    monkeypatch.setattr(loop, "build_primary_workflow_plan", lambda *_: {"calls": []})
    monkeypatch.setattr(loop.agent_tools, "prepare_recommendation_grades", lambda *_: [])
    monkeypatch.setattr(loop.agent_tools, "prepare_professor_grades", lambda *_: [])

    async def no_prefilter(*_):
        return None

    monkeypatch.setattr(prefilter, "initialize", no_prefilter)
    staged = [_card()]

    def dispatch(name, args, *, context):
        if name == "propose_recommendation":
            context["_proposed_cards"] = deepcopy(staged)
            return {"ok": True, "staged_count": len(staged), "term": "2026 Fall"}
        return {"found": False}

    monkeypatch.setattr(loop.agent_tools, "dispatch", dispatch)
    return staged


def _proposal():
    return tool_response(tool_call("propose_recommendation", {
        "items": [{"course_id": "ICS33", "category": "elective", "reason": BAD}], "term": "2026 Fall",
    }), leading_tokens=[BAD])


def _run(client, query=QUERY):
    async def collect():
        return [event async for event in loop.run_agent(
            [{"role": "user", "content": query}], client=client, model="fixture", user_id="demo_001",
            term="2026 Fall", default_term="2027 Winter", allowed_query_terms=["2026 Fall"],
            query_term_source="explicit", response_language="zh",
        )]
    return asyncio.run(collect())


@pytest.mark.parametrize("prefilter_on,ranking_on", [(True, False), (False, True), (True, True)])
def test_either_flag_protects_pretool_narration_final_text_and_card_reason(
    isolated_agent, monkeypatch, prefilter_on, ranking_on,
):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", str(prefilter_on).lower())
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", str(ranking_on).lower())
    client = ScriptedLLMClient(_proposal(), text_response(BAD))
    events = _run(client)
    assert BAD not in json.dumps(events, ensure_ascii=False)
    final = events[-1]
    assert final["controlled_recommendation"] is True
    assert "09:30" in final["text"] and "36200" in final["text"]
    assert final["cards"][0]["primary_code"] == "36200"
    assert final["cards"][0]["ge_evidence"]["status"] == "unknown"
    assert "model_reason" not in json.dumps(final)
    assert "".join(event["text"] for event in events if event["type"] == "token") == final["text"]
    assert any(event["type"] == "tool_call_start" for event in events)
    assert any(event["type"] == "cards_proposed" for event in events)
    assert len(client.calls) == 2
    client.assert_exhausted()


@pytest.mark.parametrize("flags", [(False, False), (True, False)])
def test_legacy_or_general_chat_keeps_original_stream(isolated_agent, monkeypatch, flags):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", str(flags[0]).lower())
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", str(flags[1]).lower())
    query = QUERY if not any(flags) else "解释什么是算法"
    events = _run(ScriptedLLMClient(text_response("Original ", "reply.")), query)
    assert [event["text"] for event in events if event["type"] == "token"] == ["Original ", "reply."]
    assert not events[-1].get("controlled_recommendation")


def test_budget_fallback_and_continue_keep_latest_actual_selection_without_provider_prose(isolated_agent, monkeypatch):
    monkeypatch.setattr(loop, "MAX_ITERATIONS", 1)
    client = ScriptedLLMClient(_proposal(), text_response(BAD), _proposal(), text_response(BAD))
    first = _run(client)
    cid = next(event["continuation_id"] for event in first if event["type"] == "limit_reached")
    assert first[-1]["incomplete"] is True
    # An explicit replacement must update the same course, without duplicating it.
    isolated_agent[:] = [_card(code="36210", num="B", time="11:00–12:20")]

    async def resume():
        return [event async for event in loop.resume_agent(cid, client=client, model="fixture")]

    resumed = asyncio.run(resume())
    assert BAD not in json.dumps(first + resumed, ensure_ascii=False)
    assert resumed[-1]["cards"][0]["primary_code"] == "36210"
    assert len(resumed[-1]["cards"]) == 1
    assert "11:00" in resumed[-1]["text"]
    assert "09:30" not in resumed[-1]["text"]
    assert loop.pop_continuation(cid) is None
    client.assert_exhausted()


@pytest.mark.parametrize("after_card", [False, True])
def test_model_failure_never_exposes_provider_error_or_claims_complete_success(isolated_agent, after_card):
    steps = [_proposal(), RuntimeError(BAD)] if after_card else [RuntimeError(BAD)]
    events = _run(ScriptedLLMClient(*steps))
    assert BAD not in json.dumps(events, ensure_ascii=False)
    assert events[-1]["incomplete"] is True
    assert events[-1]["failure_reason"] == "model_unavailable"
    assert bool(events[-1]["cards"]) == after_card


def test_normal_sse_and_restored_turn_store_only_canonical_content(app_client, isolated_agent, monkeypatch):
    client = ScriptedLLMClient(_proposal(), text_response(BAD))

    async def stream(message, session_state, *, user_id, term, **_kwargs):
        async for event in loop.run_agent(
            [{"role": "user", "content": message}], client=client, model="fixture", user_id=user_id,
            term=term, default_term=session_state["default_term"], allowed_query_terms=session_state["query_terms"],
            query_term_source=session_state["query_term_source"], response_language="zh",
        ):
            yield event

    monkeypatch.setattr(adapter, "stream_agent_response", stream)
    sid = sessions.create_session("demo_001", title="Controlled fixture", term_scope="2026 Fall")

    async def collect():
        return [json.loads(raw.removeprefix("data: ").strip()) async for raw in chat._stream_chat(
            chat.ChatRequest(message=QUERY, session_id=sid), BackgroundTasks(), user_id="demo_001",
        )]

    events = asyncio.run(collect())
    assert BAD not in json.dumps(events, ensure_ascii=False)
    meta = next(event for event in events if event["type"] == "meta")
    tokens = "".join(event["text"] for event in events if event["type"] == "token")
    assert tokens == meta["final_answer"]
    stored = sessions.read_turns("demo_001", sid)[-1]
    assert stored["content"] == meta["final_answer"]
    assert stored["cards"] == meta["cards"]
    assert BAD not in json.dumps(stored, ensure_ascii=False)


def test_continue_sse_preserves_canonical_cards_text_and_lookup_pairing(app_client, isolated_agent, monkeypatch):
    monkeypatch.setattr(loop, "MAX_ITERATIONS", 1)
    client = ScriptedLLMClient(_proposal(), text_response(BAD), text_response(BAD))
    initial = _run(client)
    cid = next(event["continuation_id"] for event in initial if event["type"] == "limit_reached")
    # Resuming an already protected answer cannot leak prose if configuration
    # changed since the original request.
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "false")
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "false")
    monkeypatch.setattr(adapter, "LLM_ENABLED", True)
    monkeypatch.setattr(adapter, "_get_client", lambda: client)
    sid = sessions.create_session("demo_001", title="Controlled continuation", term_scope="2026 Fall")

    async def collect():
        return [json.loads(raw.removeprefix("data: ").strip()) async for raw in chat._stream_continue(
            chat.ContinueRequest(session_id=sid, continuation_id=cid), user_id="demo_001",
        )]

    events = asyncio.run(collect())
    assert BAD not in json.dumps(events, ensure_ascii=False)
    meta = next(event for event in events if event["type"] == "meta")
    stored = sessions.read_turns("demo_001", sid)[-1]
    assert stored["content"] == meta["final_answer"]
    assert stored["cards"] == meta["cards"] == initial[-1]["cards"]
    lookup, _ = chat._schedule_lookups("demo_001", sid)
    envelope = lookup("ICS33", "2026 Fall")
    assert envelope["term"] == "2026 Fall"
    assert envelope["pairing"]["requirement"] == "unknown"
    assert envelope["prefilter"]["warnings"]
    assert BAD not in json.dumps(sessions.read_turns("demo_001", sid), ensure_ascii=False)


def test_no_cards_unresolved_gate_and_unknown_ge_are_explicitly_incomplete(isolated_agent):
    empty = {"response_language": "zh", "_controlled_recommendation_cards": []}
    assert loop.controlled_recommendation_result(empty)["incomplete"] is True
    context = {"response_language": "zh", "_controlled_recommendation_cards": [_card()],
               "_prefilter_state": {"enabled": True, "plan_status": "validated", "unresolved_constraints": [],
                                    "plan": {"course_scope": "ge"}}}
    result = loop.controlled_recommendation_result(context)
    assert result["incomplete"] is True
    assert len(result["cards"]) == 1
    assert result["cards"][0]["ge_evidence"]["is_ge"] is None
    context["_prefilter_state"]["plan"] = {"course_scope": "any", "hard_filters": {"only_unmet_ge": True}}
    assert loop.controlled_recommendation_result(context)["incomplete"] is True
    context["_prefilter_state"]["unresolved_constraints"] = ["ge_audit_unverified"]
    assert loop.controlled_recommendation_result(context)["recommendation_gate"]["ready"] is False


def test_status_arguments_cannot_publish_claims_through_invalid_term_or_reason():
    visible = loop.controlled_tool_arguments({
        "term": BAD, "reason": BAD, "course_id": "ICS33", "items": [{"course_id": "ICS33", "reason": BAD}],
    })
    assert BAD not in json.dumps(visible, ensure_ascii=False)
    assert visible["course_id"] == "I&C SCI 33"
    assert loop.controlled_tool_arguments({"term": "Fall 2026"})["term"] == "2026 Fall"


def test_continue_transport_failure_discards_buffered_text_and_raw_error(app_client, isolated_agent, monkeypatch):
    async def broken(*_args, **_kwargs):
        yield {"type": "response_mode", "controlled_recommendation": True, "response_language": "zh"}
        yield {"type": "token", "text": BAD}
        raise RuntimeError(BAD)

    monkeypatch.setattr(loop, "resume_agent", broken)
    monkeypatch.setattr(adapter, "LLM_ENABLED", True)
    monkeypatch.setattr(adapter, "_get_client", lambda: object())
    sid = sessions.create_session("demo_001", title="Failed continuation", term_scope="2026 Fall")

    async def collect():
        return [json.loads(raw.removeprefix("data: ").strip()) async for raw in chat._stream_continue(
            chat.ContinueRequest(session_id=sid, continuation_id="fixture"), user_id="demo_001",
        )]

    events = asyncio.run(collect())
    assert BAD not in json.dumps(events, ensure_ascii=False)
    assert next(event for event in events if event["type"] == "final")["incomplete"] is True
    assert BAD not in json.dumps(sessions.read_turns("demo_001", sid), ensure_ascii=False)


def test_router_uses_same_requested_intent_and_count_as_loop_when_applied_rule_was_downgraded(isolated_agent):
    from app.recommendations.filter_plan import empty_filter_plan

    applied = empty_filter_plan()
    requested = empty_filter_plan()
    requested["course_scope"] = "ge"
    requested["hard_filters"]["only_unmet_ge"] = True
    context = {"response_language": "zh", "_controlled_recommendation_cards": [_card()],
               "_prefilter_state": {"enabled": True, "plan_status": "downgraded", "plan": applied,
                                    "requested_plan": requested, "requested_count": 3,
                                    "unresolved_constraints": ["ge_audit_unverified"]}}
    final = loop.controlled_recommendation_result(context)
    final["prefilter"] = {"applied_plan": applied, "requested_plan": requested}
    assert final["incomplete"] is True
    assert final["requested_count"] == 3
    assert chat._controlled_agent_reply(final["cards"], "zh", final) == final["text"]
    assert "GE" in final["text"]
    # Early provider failures bypass the normal prefilter-summary attachment.
    # The minimal rendering intent must still travel with the final result.
    context["_prefilter_state"]["unresolved_constraints"] = []
    final = loop._controlled_failure_events(context, "model_unavailable")[-1]
    assert "prefilter" not in final
    assert chat._controlled_agent_reply(final["cards"], "zh", final) == final["text"]
