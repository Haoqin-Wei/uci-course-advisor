from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import BackgroundTasks

from app.agent import grade_evidence, loop, tools
from app.data import db, sessions
from app.llm import adapter
from app.routers import chat
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response
from tests.test_agent_zotistics import _evidence, _install_zotistics_card_catalog


def test_empty_pair_falls_back_to_all_departments_and_retains_request(monkeypatch):
    calls = []
    overall = _evidence(course_id=None, instructor_name="SHINDLER, M.")
    overall["scope"] = "instructor"

    def lookup(**query):
        calls.append(query)
        if query["course_id"]:
            return {"found": False, "source": "zotistics", "status": "no_data",
                    "scope": "course_instructor", "resolved_instructor_name": "SHINDLER, M."}
        return overall

    monkeypatch.setattr(db, "get_zotistics_grades", lookup)
    context = {}
    result = tools.dispatch("get_zotistics_grades", {
        "course_id": "CS161", "instructor_name": "SHINDLER, M.", "department": "COMPSCI",
    }, context=context)
    assert calls[1] == {"course_id": None, "instructor_name": "SHINDLER, M.", "department": None}
    assert result["scope"] == "instructor"
    assert result["requested_course_id"] == "COMPSCI 161"
    assert result["fallback_from_scope"] == "course_instructor"
    assert grade_evidence.find_history(context, "COMPSCI161", "SHINDLER, M.", "COMPSCI")["grades"] == overall["grades"]
    assert grade_evidence.find_history(context, "ICS33", "SHINDLER, M.", "I&C SCI") is None


@pytest.mark.parametrize("status", ["unavailable", "ambiguous_instructor", "not_found", "invalid_query"])
def test_failed_pair_does_not_trigger_all_course_fallback(monkeypatch, status):
    calls = []

    def lookup(**query):
        calls.append(query)
        return {"found": False, "source": "zotistics", "scope": "course_instructor", "status": status}

    monkeypatch.setattr(db, "get_zotistics_grades", lookup)
    result = grade_evidence.get_history({}, "CS161", "SHINDLER, M.", "COMPSCI")
    assert len(calls) == 1
    assert result["status"] == status


def test_verified_full_name_and_canonical_alias_share_evidence_without_guessing(monkeypatch):
    calls = []
    history = _evidence(course_id="ICS45C", instructor_name="KLEFSTAD, R.")
    history["resolved_instructor_name"] = "KLEFSTAD, R."
    history["query"] = {"course_id": "ICS45C", "instructor_name": "RAINER KLEFSTAD", "department": "I&C SCI"}
    monkeypatch.setattr(db, "get_zotistics_grades", lambda **query: calls.append(query) or history)
    context = {}
    grade_evidence.get_history(context, "ICS45C", "Rainer Klefstad", "I&C SCI")
    assert grade_evidence.find_history(context, "I&C SCI 45C", "Klefstad, R", "I&C SCI") is history
    assert grade_evidence.get_history(context, "I&C SCI 45C", "KLEFSTAD, R.", "I&C SCI") is history
    assert len(calls) == 1
    assert grade_evidence.find_history(context, "ICS45C", "Riley Klefstad", "I&C SCI") is None


def test_agent_collects_current_lecturers_before_cards_without_grading_ta(monkeypatch):
    _install_zotistics_card_catalog(monkeypatch)
    calls = []

    def lookup(**query):
        calls.append(query)
        return _evidence(course_id=query["course_id"], instructor_name=query["instructor_name"])

    monkeypatch.setattr(db, "get_zotistics_grades", lookup)
    monkeypatch.setattr(loop, "build_primary_workflow_plan", lambda *_args: {"calls": []})
    client = ScriptedLLMClient(
        tool_response(tool_call("propose_recommendation", {
            "items": [{"course_id": "COMPSCI161", "category": "core", "reason": "Algorithms"},
                      {"course_id": "IN4MATX43", "category": "elective", "reason": "Projects"}],
            "term": "2026 Fall",
        }, call_id="cards")),
        text_response("Two grounded recommendations."),
    )

    async def collect():
        return [event async for event in loop.run_agent(
            [{"role": "user", "content": "Recommend CS161 and IN4MATX43"}],
            client=client, model="fixture", user_id="demo_001", term="2026 Fall",
            default_term="2027 Winter", allowed_query_terms=["2026 Fall"],
        )]

    events = asyncio.run(collect())
    assert {query["instructor_name"] for query in calls} == {"SHINDLER, M.", "OTHER, N."}
    assert all(query["instructor_name"] != "ASSISTANT, T." for query in calls)
    cards = next(event["cards"] for event in events if event["type"] == "cards_proposed")
    assert cards[0]["term"] == "2026 Fall"
    assert cards[0]["section_groups"][0]["primary"]["grade_history"][0]["scope"] == "course_instructor"
    assert cards[1]["section_groups"][0]["primary"]["grade_history"][0]["grades"]["course_id"] == "IN4MATX43"
    assert not events[-1].get("grade_charts"), "cards already carry their own grade charts"
    result = next(json.loads(message["content"]) for message in client.calls[1].messages
                  if message.get("tool_call_id") == "cards")
    assert result["ranking_enabled"] is False
    assert len(result["historical_grades"]) == 3
    client.assert_exhausted()


@pytest.mark.parametrize("second_status", ["ok", "unavailable"])
def test_professor_comparison_grade_charts_survive_stream_and_session_restore(app_client, monkeypatch, second_status):
    _install_zotistics_card_catalog(monkeypatch)
    def grades_lookup(**query):
        result = _evidence(course_id=query["course_id"], instructor_name=query["instructor_name"])
        if query["instructor_name"] == "OTHER, N." and second_status != "ok":
            result.update(found=False, status=second_status, grades=None)
        return result
    monkeypatch.setattr(db, "get_zotistics_grades", grades_lookup)
    monkeypatch.setattr(loop, "build_primary_workflow_plan", lambda *_args: {"calls": []})
    client = ScriptedLLMClient(
        tool_response(
            tool_call("get_professor_rating", {"instructor_name": "SHINDLER, M.", "department": "COMPSCI"}, call_id="one", index=0),
            tool_call("get_professor_rating", {"instructor_name": "OTHER, N.", "department": "COMPSCI"}, call_id="two", index=1),
        ),
        text_response("Historical grading evidence for the two professors."),
    )

    async def stream(message, session_state, *, user_id, term, **_kwargs):
        async for event in loop.run_agent(
            [{"role": "user", "content": message}], client=client, model="fixture",
            user_id=user_id, term=term, allowed_query_terms=session_state["query_terms"],
            query_term_source=session_state["query_term_source"],
            offering_course_ids=session_state["query_course_ids"],
        ):
            yield event

    monkeypatch.setattr(adapter, "stream_agent_response", stream)
    session_id = sessions.create_session("demo_001", title="Grade comparison", term_scope="2026 Fall")

    async def collect():
        return [json.loads(raw.removeprefix("data: ").strip()) async for raw in chat._stream_chat(
            chat.ChatRequest(message="比较 CS161 的 SHINDLER 和 OTHER 给分", session_id=session_id),
            BackgroundTasks(), user_id="demo_001",
        )]

    events = asyncio.run(collect())
    meta = next(event for event in events if event["type"] == "meta")
    assert meta["cards"] == []
    assert {row["instructor"] for row in meta["grade_charts"]} == {"SHINDLER, M.", "OTHER, N."}
    assert all(row["scope"] == "course_instructor" for row in meta["grade_charts"])
    second = next(row for row in meta["grade_charts"] if row["instructor"] == "OTHER, N.")
    assert second["status"] == second_status
    if second_status != "ok":
        assert second["grades"] is None
    restored = app_client.get(f"/api/sessions/demo_001/{session_id}").json()
    assert restored["turns"][-1]["grade_charts"] == meta["grade_charts"]
    assert "cards" not in restored["turns"][-1]
    client.assert_exhausted()


def _card_context():
    return {"user_id": "demo_001", "term": "2026 Fall",
            "allowed_query_terms": ["2026 Fall"], "pending_schedule": []}


def _section(code, number, kind, instructor, status="OPEN"):
    return {"course_id": "COMPSCI161", "section_code": code,
            "section_num": number, "section_type": kind,
            "instructors": [instructor], "status": status,
            "days": "MW", "start_time": "09:00", "end_time": "10:20"}


def _one_card(context):
    result = tools.dispatch("propose_recommendation", {
        "items": [{"course_id": "COMPSCI161", "category": "elective", "reason": "Algorithms"}],
        "term": "2026 Fall",
    }, context=context)
    assert result["ok"] is True
    assert result["staged_count"] == 1
    return context["_proposed_cards"][0]


@pytest.mark.parametrize("enabled", [False, True])
def test_orphan_discussion_group_does_not_break_grade_attachment_or_ack(monkeypatch, enabled):
    _install_zotistics_card_catalog(monkeypatch)
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", str(enabled).lower())
    sections = [_section("20000", "A", "Lec", "SHINDLER, M."),
                _section("20002", "B", "Lec", "OTHER, N.", status="FULL"),
                _section("20003", "B1", "Dis", "ASSISTANT, T.")]
    monkeypatch.setattr(db, "get_sections", lambda *_args: {"found": True, "sections": sections})
    context = _card_context()
    card = _one_card(context)
    orphan = next(group for group in card["section_groups"] if group["primary"] is None
                  and any(section.get("code") == "20003" for section in group["secondaries"]))
    # Modern grouping does not invent a relationship from the B1 label.
    assert orphan["letter"] == ("?" if enabled else "B")
    assert orphan["primary"] is None
    assert orphan["secondaries"][0]["code"] == "20003"
    assert card["primary_code"] == "20000"


@pytest.mark.parametrize("enabled,expected_code", [(False, "20000"), (True, "20002")])
def test_best_ranked_seminar_matches_card_code_and_first_group(monkeypatch, enabled, expected_code):
    _install_zotistics_card_catalog(monkeypatch)
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", str(enabled).lower())
    sections = [_section("20000", "A", "Lec", "LECTURER, A."),
                _section("20002", "B", "Sem", "SEMINAR, S.")]
    monkeypatch.setattr(db, "get_sections", lambda *_args: {"found": True, "sections": sections})
    monkeypatch.setattr(db, "get_professor_rating", lambda name, **_kwargs: {
        "found": True, "instructor": {"name": name, "avg_rating": 3 if name == "LECTURER, A." else 4.9,
                                       "num_ratings": 50},
    })
    card = _one_card(_card_context())
    assert card["primary_code"] == expected_code
    assert card["enrollment_codes"][0]["code"] == expected_code
    assert card["section_groups"][0]["primary"]["code"] == expected_code
    if enabled:
        assert card["ranking"]["final_score"] == card["section_groups"][0]["primary"]["ranking"]["final_score"]
        assert card["ranking"]["final_score"] > card["section_groups"][1]["primary"]["ranking"]["final_score"]


def test_grade_evidence_and_charts_survive_limit_and_resume_without_refetch(monkeypatch):
    calls = []
    history = _evidence(course_id="CS161", instructor_name="SHINDLER, M.")
    monkeypatch.setattr(db, "get_zotistics_grades", lambda **query: calls.append(query) or history)
    monkeypatch.setattr(loop, "build_primary_workflow_plan", lambda *_args: {"calls": []})
    monkeypatch.setattr(loop, "MAX_TOTAL_TOOLS", 1)
    client = ScriptedLLMClient(
        tool_response(tool_call("get_zotistics_grades", {"course_id": "CS161", "instructor_name": "SHINDLER, M."}, call_id="grades")),
        tool_response(tool_call("get_course_info", {"course_id": "CS161"}, call_id="info")),
        text_response("Historical evidence collected."),
        tool_response(tool_call("get_zotistics_grades", {"course_id": "COMPSCI161", "instructor_name": "SHINDLER, M."}, call_id="repeat")),
        text_response("Comparison completed."),
    )

    async def collect():
        initial = [event async for event in loop.run_agent(
            [{"role": "user", "content": "Compare CS161 historical grades"}],
            client=client, model="fixture", user_id="demo_001", term="2026 Fall",
            allowed_query_terms=["2026 Fall"],
        )]
        continuation_id = next(event["continuation_id"] for event in initial if event["type"] == "limit_reached")
        resumed = [event async for event in loop.resume_agent(continuation_id, client=client, model="fixture")]
        return initial, resumed

    initial, resumed = asyncio.run(collect())
    assert initial[-1]["truncated"] is True
    assert resumed[-1]["grade_charts"] == initial[-1]["grade_charts"]
    assert len(resumed[-1]["grade_charts"]) == 1
    assert len(calls) == 1
    client.assert_exhausted()


def test_continue_stream_persists_grade_charts_for_session_restore(monkeypatch):
    charts = [{"instructor": "SHINDLER, M.", **_evidence(course_id="CS161", instructor_name="SHINDLER, M.")}]

    async def resume(*_args, **_kwargs):
        yield {"type": "token", "text": "Resumed comparison."}
        yield {"type": "final", "text": "Resumed comparison.", "grade_charts": charts}

    monkeypatch.setattr(loop, "resume_agent", resume)
    monkeypatch.setattr(adapter, "LLM_ENABLED", True)
    monkeypatch.setattr(adapter, "_get_client", lambda: object())
    session_id = sessions.create_session("demo_001", title="Continue grades", term_scope="2026 Fall")

    async def collect():
        return [json.loads(raw.removeprefix("data: ").strip()) async for raw in chat._stream_continue(
            chat.ContinueRequest(session_id=session_id, continuation_id="fixture"), user_id="demo_001",
        )]

    events = asyncio.run(collect())
    assert next(event for event in events if event["type"] == "meta")["grade_charts"] == charts
    assert sessions.read_turns("demo_001", session_id)[-1]["grade_charts"] == charts
