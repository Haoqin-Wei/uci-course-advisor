from __future__ import annotations

import asyncio
from copy import deepcopy
import csv
import json
import shutil

import pytest
from fastapi import BackgroundTasks

from app import config, observability
from app.agent import loop, prefilter, tools
from app.catalog.term import Term
from app.catalog.types import CourseRecord, CourseRef, Provenance, SectionRecord
from app.catalog.view import CatalogView
from app.data import db, recommendations, zotistics, sessions
from app.llm import adapter
from app.routers import chat
from app.recommendations.filter_plan import empty_filter_plan, validate_filter_plan
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response
from tests.test_recommendation_ranking_integration import _history, _rating


TERM = Term(2026, "Fall")


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "false")
    monkeypatch.delenv("RECOMMENDATION_RANKING_WEIGHTS", raising=False)
    monkeypatch.delenv("RECOMMENDATION_RANKING_RELIABILITY", raising=False)


def _install(monkeypatch, definitions=None, count=3, profile=None):
    definitions = definitions or {1: ("08:00", "09:00", "EARLY, E."),
                                  2: ("11:00", "12:00", "LATE, L."),
                                  3: (None, None, "UNKNOWN, T.")}
    provenance = Provenance(TERM.term_id, TERM.term_id, "prefilter_fixture")
    courses, sections = [], []
    for n in range(1, count + 1):
        ref = CourseRef("COMPSCI", str(n))
        courses.append(CourseRecord(ref, title=f"Synthetic {n}", units=4,
                                    ge_categories=("III",), provenance=provenance,
                                    ge_categories_verified=True))
        start, end, person = definitions.get(n, ("11:00", "12:00", "LECTURER, A."))
        sections.append(SectionRecord(
            section_id=f"{TERM.term_id}_{10000+n}", course=ref, section_code=str(10000 + n),
            term_id=TERM.term_id, section_type="Lec", section_num="A", instructors=(person,),
            days="MW", start_time=start, end_time=end, status="OPEN", provenance=provenance,
        ))
    view = CatalogView(TERM, courses, sections, provenance)
    monkeypatch.setattr(db, "get_catalog", lambda term: view if term == TERM else None)
    monkeypatch.setattr(recommendations, "get_catalog", lambda term: view if term == TERM else None)
    monkeypatch.setattr(db, "get_student_profile", lambda *_args: {"found": True, "profile": profile or {}})
    monkeypatch.setattr(db, "get_course_info", lambda cid: {
        "found": True, "source": "test", "course": db._course_record_to_dict(view.get_course(db._to_ref(cid))),
    })
    monkeypatch.setattr(db, "get_sections", lambda cid, term: {
        "found": True, "source": "test", "term": term,
        "sections": [db._section_record_to_dict(s) for s in view.get_sections(db._to_ref(cid))],
    })
    monkeypatch.setattr(db, "get_professor_rating", lambda *_args, **_kwargs: {"found": False})
    monkeypatch.setattr(db, "get_grade_distribution", lambda *_args: {"found": False})
    from app.data import policies
    monkeypatch.setattr(policies, "is_term_past_deadline", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(policies, "get_term_calendar", lambda *_args: {})
    return view


def _context(query="推荐简单GE课程"):
    return {"user_id": "demo_001", "term": "2026 Fall", "default_term": "2027 Winter",
            "allowed_query_terms": ["2026 Fall"], "query_term_source": "explicit",
            "response_language": "zh", "pending_schedule": [], "user_query": query}


def _parser(monkeypatch, raw=None):
    calls = []

    async def parse(message, context, profile):
        calls.append(message)
        plan = deepcopy(raw or empty_filter_plan())
        plan["course_scope"] = "ge"
        plan["explicit_constraints"].append({"field": "course_scope", "quote": message, "source": "user_message"})
        return {**validate_filter_plan(plan, message, profile),
                "metrics": {"llm_calls": 1, "parse_elapsed_ms": 0.1, "failure_reason": None}}

    from app.llm import filter_plan
    monkeypatch.setattr(filter_plan, "parse_filter_plan", parse)
    return calls


def _early_plan(message):
    raw = empty_filter_plan()
    raw["hard_filters"].update(earliest_start="10:00", earliest_start_is_hard=True)
    raw["explicit_constraints"] = [{"field": "earliest_start", "quote": message, "source": "user_message"}]
    return raw


def _init(context):
    asyncio.run(prefilter.initialize(context, [{"role": "user", "content": context["user_query"]}]))


def test_default_configuration_and_disabled_path_do_not_parse_or_change_search(monkeypatch):
    _install(monkeypatch, count=35)
    monkeypatch.delenv("DYNAMIC_PREFILTER_ENABLED")
    assert config.dynamic_prefilter_enabled() is False
    calls = _parser(monkeypatch)
    context = _context()
    _init(context)
    result = tools.dispatch("search_courses", {"term": "2026 Fall"}, context=context)
    assert calls == []
    assert "_prefilter_state" not in context
    assert "prefilter" not in result
    assert len(result["courses"]) == 30
    assert result["truncated"] is True


def test_enabled_prefilter_keeps_full_pool_over_30_and_time_unknown(monkeypatch):
    _install(monkeypatch, count=35)
    query = "推荐GE，绝对不能有10点以前的课"
    _parser(monkeypatch, _early_plan(query))
    context = _context(query)
    _init(context)
    result = tools.dispatch("search_courses", {"term": "2026 Fall"}, context=context)
    assert result["total_found"] == 34
    assert len(result["courses"]) == 34
    assert result["truncated"] is False
    assert "COMPSCI 35" in {row["course_id"] for row in result["courses"]}
    assert result["prefilter"]["excluded"][0]["id"] == "COMPSCI 1"
    assert any(row["id"] == "COMPSCI 3" for row in result["prefilter"]["needs_verification"])
    assert context["term"] == "2026 Fall" and context["default_term"] == "2027 Winter"


def test_removed_candidates_never_reach_snapshot_ranking(monkeypatch):
    _install(monkeypatch, count=35)
    query = "推荐GE，绝对不能有10点以前的课"
    _parser(monkeypatch, _early_plan(query))
    context = _context(query)
    _init(context)
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "true")
    looked_up = []

    def grades(**kwargs):
        looked_up.append(kwargs["course_id"])
        return _history(kwargs["course_id"], kwargs["instructor_name"], 1 if kwargs["course_id"] == "COMPSCI 35" else 0)

    monkeypatch.setattr(zotistics, "get_cached_query_result", grades)
    monkeypatch.setattr(recommendations, "_local_rating", lambda *_args: _rating())
    result = tools.dispatch("search_courses", {"term": "2026 Fall"}, context=context)
    assert "COMPSCI 1" not in looked_up
    assert len(looked_up) == 34
    assert result["courses"][0]["course_id"] == "COMPSCI 35"
    assert result["catalog_candidates"] == 34
    assert len(result["courses"]) == 30
    assert result["prefilter"]["counts"]["before"] == 35


@pytest.mark.parametrize("name,args", [
    ("get_zotistics_grades", {"course_id": "CS1", "instructor_name": "EARLY, E."}),
    ("get_grade_distribution", {"course_id": "CS1"}),
    ("get_course_info", {"course_id": "CS1"}),
    ("get_professor_rating", {"instructor_name": "EARLY, E."}),
    ("get_professor_reviews", {"instructor_name": "EARLY, E."}),
    ("summarize_professor_reviews", {"instructor_name": "EARLY, E."}),
])
def test_detail_tool_cannot_bypass_prefilter(monkeypatch, name, args):
    _install(monkeypatch)
    query = "推荐GE，绝对不能有10点以前的课"
    _parser(monkeypatch, _early_plan(query))
    context = _context(query)
    _init(context)
    monkeypatch.setitem(tools.DISPATCH, name, lambda **_kwargs: pytest.fail("excluded detail lookup"))
    result = tools.dispatch(name, args, context=context)
    assert "error" in result
    assert context["_prefilter_state"]["detail_query_attempts"] == 0


def test_prefilter_is_before_first_recommendation_call_and_card_grade_fetch(monkeypatch):
    _install(monkeypatch)
    # This ordering positive case supplies an already-evaluated synthetic v2
    # contract for the current product policy; unversioned old decisions must
    # instead be refreshed (covered by test_websoc_co_class_lookup).
    original_course = db._course_record_to_dict

    def verified_course(record):
        course = original_course(record)
        from app.data.websoc_pairing import POLICY_MARKER
        evidence = {**POLICY_MARKER, "status": "verified", "source": "verified_course_pairing",
                    "course_id": course["course_id"], "term": "2026 Fall"}
        code = str(10000 + int(record.ref.course_number))
        course["pairing"] = {"version": 2, "evidence": evidence, "per_primary": {code: {
            "requirement": "not_required", "required_components": [], "allowed_bundles": [],
            "relationship": "unknown", "relationship_complete": False, "evidence": deepcopy(evidence)}}}
        return course

    monkeypatch.setattr(db, "_course_record_to_dict", verified_course)
    query = "推荐GE，绝对不能有10点以前的课"
    calls = _parser(monkeypatch, _early_plan(query))
    grade_calls, rating_calls = [], []
    monkeypatch.setattr(loop, "build_primary_workflow_plan", lambda *_args: {"calls": []})
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "true")
    monkeypatch.setattr(db, "get_zotistics_grades", lambda **kwargs: grade_calls.append(kwargs) or
                        _history(kwargs["course_id"], kwargs["instructor_name"], .8))
    monkeypatch.setattr(db, "get_professor_rating", lambda name, **_kwargs: rating_calls.append(name) or _rating())

    def first(call):
        assert calls == [query]
        assert grade_calls == [] and rating_calls == []
        assert any("Dynamic prefilter is enabled" in row.get("content", "")
                   and "recommendation_gate" in row.get("content", "") for row in call.messages)
        return tool_response(
            tool_call("get_zotistics_grades", {"course_id": "CS1", "instructor_name": "EARLY, E."}, call_id="blocked", index=0),
            tool_call("propose_recommendation", {"items": [
                {"course_id": "CS1", "category": "elective", "reason": "Should be blocked"},
                {"course_id": "CS2", "category": "elective", "reason": "Fits the verified time condition"},
            ], "term": "2026 Fall"}, call_id="cards", index=1),
        )

    client = ScriptedLLMClient(first, text_response("推荐保留的班次，选课条件另行核实。"))

    async def run():
        return [event async for event in loop.run_agent(
            [{"role": "user", "content": query}], client=client, model="fixture", user_id="demo_001",
            term="2026 Fall", default_term="2027 Winter", allowed_query_terms=["2026 Fall"],
            query_term_source="explicit", response_language="zh",
        )]

    events = asyncio.run(run())
    cards = next(event["cards"] for event in events if event["type"] == "cards_proposed")
    assert len(cards) == 1 and cards[0]["course_id"] == "COMPSCI 2"
    assert cards[0]["primary_code"] == "10002"
    assert cards[0]["prefilter"]["enabled"] is True
    assert cards[0]["ranking"]["final_score"] is not None
    assert all(row["instructor_name"] == "LATE, L." for row in grade_calls)
    assert set(rating_calls) == {"LATE, L."}
    assert events[-1]["prefilter"]["excluded_count"] == 1
    client.assert_exhausted()


def test_live_refresh_is_filtered_without_reintroducing_disallowed_section(monkeypatch):
    _install(monkeypatch)
    query = "推荐GE，绝对不能有10点以前的课"
    _parser(monkeypatch, _early_plan(query))
    context = _context(query)
    _init(context)
    monkeypatch.setattr(db, "get_live_sections", lambda *_args, **_kwargs: {
        "found": True, "source": "live_fixture", "term": "2026 Fall", "is_live": True,
        "sections": [
            {"section_code": "20001", "section_type": "Lec", "section_num": "A", "status": "OPEN",
             "days": "M", "start_time": "08:00", "end_time": "09:00", "instructors": ["EARLY, E."]},
            {"section_code": "20002", "section_type": "Lec", "section_num": "B", "status": "OPEN",
             "days": "M", "start_time": "11:00", "end_time": "12:00", "instructors": ["LATE, L."]},
        ],
    })
    result = tools.dispatch("get_live_sections", {"course_id": "CS2", "term": "2026 Fall"}, context=context)
    assert [row["section_code"] for row in result["sections"]] == ["20002"]
    cached = tools.dispatch("get_sections", {"course_id": "CS2", "term": "2026 Fall"}, context=context)
    assert cached["sections"] == result["sections"]


def test_chinese_ge_scope_does_not_include_known_non_ge(monkeypatch):
    _install(monkeypatch, count=1)
    # Test through the public retrieval mapping instead of mutating frozen records.
    original = db._course_record_to_dict
    monkeypatch.setattr(db, "_course_record_to_dict", lambda record: {**original(record), "ge_categories": []})
    _parser(monkeypatch)
    context = _context("推荐GE课程")
    _init(context)
    result = tools.dispatch("search_courses", {"term": "2026 Fall"}, context=context)
    assert result["courses"] == []
    assert result["prefilter"]["excluded"][0]["reasons"] == ["not_a_ge_course"]


def test_plain_offering_comparison_does_not_enter_dynamic_prefilter(monkeypatch):
    calls = _parser(monkeypatch)
    context = _context("比较CS1在2025 Winter和2026 Fall的授课教授")
    context["query_term_source"] = "comparison"
    _init(context)
    assert calls == []
    assert not prefilter.active(context)


@pytest.mark.parametrize("message", ["不要GE，推荐专业课", "我的GE全修完了，推荐容易拿A的课程"])
def test_ge_mention_is_not_a_ge_only_constraint(monkeypatch, message):
    _install(monkeypatch, count=1)
    original = db._course_record_to_dict
    monkeypatch.setattr(db, "_course_record_to_dict", lambda record: {**original(record), "ge_categories": []})
    _parser(monkeypatch)
    context = _context(message)
    _init(context)
    result = tools.dispatch("search_courses", {"term": "2026 Fall"}, context=context)
    assert len(result["courses"]) == 1
    assert context["_prefilter_state"]["ge_scope_requested"] is False


@pytest.mark.parametrize("tool_name", ["get_professor_reviews", "get_professor_tags", "summarize_professor_reviews"])
@pytest.mark.parametrize("department", ["COMPSCI", None])
def test_bare_course_number_does_not_bypass_excluded_course(monkeypatch, tool_name, department):
    _install(monkeypatch, definitions={1: ("08:00", "09:00", "LECTURER, A."),
                                     2: ("11:00", "12:00", "LECTURER, A.")}, count=2)
    query = "推荐GE，绝对不能有10点以前的课"
    _parser(monkeypatch, _early_plan(query))
    context = _context(query)
    _init(context)
    monkeypatch.setitem(tools.DISPATCH, tool_name, lambda **_kwargs: pytest.fail("excluded bare-course lookup"))
    args = {"instructor_name": "LECTURER, A.", "course": "1"}
    if department:
        args["department"] = department
    result = tools.dispatch(tool_name, args, context=context)
    assert result["prefilter_excluded"][0]["id"] == "COMPSCI 1"


@pytest.mark.parametrize("items", ["COMPSCI 2", [None], [False]])
def test_malformed_proposal_is_rejected_without_crashing_or_fetching(monkeypatch, items):
    _install(monkeypatch)
    _parser(monkeypatch)
    context = _context()
    _init(context)
    result = tools.dispatch("propose_recommendation", {"items": items, "term": "2026 Fall"}, context=context)
    assert result["ok"] is False
    assert "_proposed_cards" not in context


def test_optional_discussion_is_not_inferred_required_from_presence(monkeypatch):
    _install(monkeypatch)
    query = "推荐GE，绝对不能有10点以前的课"
    _parser(monkeypatch, _early_plan(query))
    context = _context(query)
    _init(context)
    course = {"course_id": "COMPSCI 2", "ge_categories": ["III"], "requires_secondary": False}
    sections = [
        {"section_code": "20002", "section_type": "Lec", "section_num": "A", "start_time": "11:00", "end_time": "12:00", "days": "M"},
        {"section_code": "20003", "section_type": "Dis", "section_num": "A1", "start_time": "08:00", "end_time": "09:00", "days": "M"},
    ]
    candidate = prefilter._metadata_candidate(course, sections, context, "2026 Fall")
    assert candidate["requires_secondary"] is False
    result = prefilter._apply([candidate], context)
    assert len(result["kept"]) == 1
    assert [row["section_code"] for row in result["kept"][0]["sections"]] == ["20002"]


def test_missing_ge_tags_are_unknown_instead_of_confirmed_non_ge(monkeypatch):
    _install(monkeypatch)
    _parser(monkeypatch)
    context = _context()
    _init(context)
    row = prefilter._metadata_candidate({"course_id": "COMPSCI 2", "ge_categories": []},
                                       [{"section_type": "Lec", "section_num": "A", "section_code": "10002"}],
                                       context, "2026 Fall")
    assert row["ge_labels_verified"] is False
    result = prefilter._apply([row], context)
    assert result["excluded"] == []
    assert "ge_labels_unknown" in result["needs_verification"][0]["reasons"]


@pytest.mark.parametrize("raw,expected", [("[]", False), ("", False), ("invalid", False)])
def test_catalog_distinguishes_known_empty_ge_list_from_missing_source_data(catalog_fixture_dir, tmp_path, raw, expected):
    # Empty JSON is a parsed local list, not proof of an annual official
    # non-GE designation. S-002 keeps all undated catalogue rows unverified.
    from app.catalog.loaders.uci_relational import UCIRelationalLoader
    fixture = tmp_path / "catalog"
    shutil.copytree(catalog_fixture_dir, fixture)
    path = fixture / "courses.csv"
    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        fields, rows = reader.fieldnames, list(reader)
    if "ge_list" not in fields:
        fields.append("ge_list")
    for row in rows:
        row["ge_list"] = raw
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    courses, _ = UCIRelationalLoader(fixture).load(Term(2025, "Spring"))
    assert courses
    assert all(course.ge_categories_verified is expected for course in courses)
    assert all("ge_categories_verified" not in db._course_record_to_dict(course) for course in courses)


@pytest.mark.parametrize("response_term,coverage", [
    ("2025 Fall", {}), ("2026 Fall", {"is_historical_proxy": True}),
])
def test_historical_response_does_not_become_verified_target_sections(monkeypatch, response_term, coverage):
    _install(monkeypatch)
    query = "推荐GE，绝对不能有10点以前的课"
    _parser(monkeypatch, _early_plan(query))
    context = _context(query)
    _init(context)
    course = {"course_id": "COMPSCI 2", "ge_categories": ["III"]}
    section = {"section_code": "30001", "section_type": "Lec", "section_num": "A", "days": "M",
               "start_time": "08:00", "end_time": "09:00", "instructors": ["HISTORICAL, H."]}
    response = {"found": True, "term": response_term, "source": "fixture", "offering_status": "offered",
                "data_coverage": coverage, "sections": [section]}
    candidate = prefilter._metadata_candidate(course, [section], context, "2026 Fall", offering=response)
    result = prefilter._apply([candidate], context)
    assert result["excluded"] == []
    assert result["kept"][0]["sections"] == []
    assert result["needs_verification"]


def test_card_warnings_are_isolated_by_term(monkeypatch):
    _install(monkeypatch)
    _parser(monkeypatch)
    context = _context()
    _init(context)
    state = context["_prefilter_state"]
    state["candidates"][("2027 Winter", "COMPSCI 2")] = {"course_id": "COMPSCI 2", "prefilter": {"status": "keep", "reasons": []}}
    state["pools"]["historical_fixture"] = {
        "term": "2026 Winter", "prefilter": {"needs_verification": [
            {"id": "COMPSCI 2", "term": "2026 Winter", "reasons": ["time_unknown"]},
        ]},
    }
    assert prefilter.card_metadata(context, "CS2", "2027 Winter")["warnings"] == []


def test_ranking_retains_unknown_sections_as_unranked_without_detail_queries(monkeypatch):
    _install(monkeypatch)
    _parser(monkeypatch)
    context = _context()
    _init(context)
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "true")
    state = context["_prefilter_state"]
    row = state["candidates"][("2026 Fall", "COMPSCI 2")]
    row["sections"] = []
    state["pools"].clear()
    state["section_overrides"][("2026 Fall", "COMPSCI 2")] = {
        "found": False, "term": "2026 Fall", "offering_status": "unavailable", "sections": [],
    }
    monkeypatch.setattr(zotistics, "get_cached_query_result", lambda **kwargs: _history(kwargs["course_id"], kwargs["instructor_name"]))
    monkeypatch.setattr(recommendations, "_local_rating", lambda *_args: _rating())
    result = tools.dispatch("search_courses", {"term": "2026 Fall"}, context=context)
    retained = next(row for row in result["courses"] if row["course_id"] == "COMPSCI 2")
    assert retained["ranking"]["final_score"] is None
    assert retained["section_code"] is None


def test_prefilter_audit_survives_sse_continue_and_session_restore(app_client, monkeypatch):
    _install(monkeypatch)
    _parser(monkeypatch)
    context = _context()
    _init(context)
    audit = prefilter.summary(context)
    assert "profile" not in audit

    async def stream(*_args, **_kwargs):
        yield {"type": "token", "text": "已完成预筛，待核实项单独说明。"}
        yield {"type": "final", "text": "已完成预筛，待核实项单独说明。", "prefilter": audit}

    monkeypatch.setattr(adapter, "stream_agent_response", stream)
    session_id = sessions.create_session("demo_001", title="Prefilter audit", term_scope="2026 Fall")

    async def collect():
        return [json.loads(raw.removeprefix("data: ").strip()) async for raw in chat._stream_chat(
            chat.ChatRequest(message="推荐简单GE课程", session_id=session_id), BackgroundTasks(), user_id="demo_001",
        )]

    events = asyncio.run(collect())
    assert next(event for event in events if event["type"] == "meta")["prefilter"] == audit
    restored = app_client.get(f"/api/sessions/demo_001/{session_id}").json()
    assert restored["turns"][-1]["prefilter"] == audit

    async def resume(*_args, **_kwargs):
        yield {"type": "final", "text": "继续完成推荐。", "prefilter": audit}

    monkeypatch.setattr(loop, "resume_agent", resume)
    monkeypatch.setattr(adapter, "LLM_ENABLED", True)
    monkeypatch.setattr(adapter, "_get_client", lambda: object())

    async def continued():
        return [json.loads(raw.removeprefix("data: ").strip()) async for raw in chat._stream_continue(
            chat.ContinueRequest(session_id=session_id, continuation_id="fixture"), user_id="demo_001",
        )]

    assert next(event for event in asyncio.run(continued()) if event["type"] == "meta")["prefilter"] == audit
    assert sessions.read_turns("demo_001", session_id)[-1]["prefilter"] == audit


def test_continuation_preserves_applied_plan_and_guards_without_reparse(monkeypatch):
    _install(monkeypatch)
    query = "推荐GE，绝对不能有10点以前的课"
    calls = _parser(monkeypatch, _early_plan(query))
    monkeypatch.setattr(loop, "build_primary_workflow_plan", lambda *_args: {"calls": []})
    monkeypatch.setattr(loop, "MAX_TOTAL_TOOLS", 1)
    client = ScriptedLLMClient(
        tool_response(tool_call("get_student_profile", {}, call_id="profile")),
        tool_response(tool_call("get_grade_distribution", {"course_id": "CS2"}, call_id="pending")),
        text_response("目前已筛选候选。"),
        tool_response(tool_call("get_grade_distribution", {"course_id": "CS1"}, call_id="blocked")),
        text_response("继续保留合格候选。"),
    )

    async def run():
        initial = [event async for event in loop.run_agent(
            [{"role": "user", "content": query}], client=client, model="fixture", user_id="demo_001",
            term="2026 Fall", allowed_query_terms=["2026 Fall"],
        )]
        cid = next(event["continuation_id"] for event in initial if event["type"] == "limit_reached")
        resumed = [event async for event in loop.resume_agent(cid, client=client, model="fixture")]
        return initial, resumed

    initial, resumed = asyncio.run(run())
    assert len(calls) == 1
    assert resumed[-1]["prefilter"]["applied_plan"] == initial[-1]["prefilter"]["applied_plan"]
    blocked = next(json.loads(row["content"]) for row in client.calls[-1].messages
                   if row.get("tool_call_id") == "blocked")
    assert blocked["prefilter_excluded"][0]["id"] == "COMPSCI 1"
    assert observability.snapshot_metrics()["counters"]["prefilter.blocked_tools{tool=get_grade_distribution}"] == 1
    client.assert_exhausted()
