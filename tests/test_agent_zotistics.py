from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agent import loop, tools
from app.data import db, offerings
from app.llm.adapter import AGENT_SYSTEM_PROMPT
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response


def _evidence(*, course_id="ICS33", instructor_name=None) -> dict:
    return {
        "found": True,
        "status": "ok",
        "source": "zotistics",
        "scope": "course_instructor" if instructor_name else "course",
        "source_url": "https://zotistics.com/",
        "coverage": {"terms": ["2024 Fall", "2025 Winter"]},
        "grades": {
            "course_id": course_id,
            "instructor_name": instructor_name,
            "avg_gpa": 3.4,
            "pct_A": 60.0,
            "graded": 100,
            "samples": 105,
            "letter_counts": {"A": 60, "B": 30, "C": 5, "D": 0, "F": 5, "P": 5},
        },
        "by_instructor": [],
    }


@pytest.mark.parametrize("query", [
    {"course_id": "ICS33"},
    {"instructor_name": "Thornton"},
    {"course_id": "ICS33", "instructor_name": "THORNTON, A.", "department": "I&C SCI"},
])
def test_zotistics_dispatch_preserves_scope_and_evidence_without_changing_terms(monkeypatch, query):
    result = _evidence(instructor_name=query.get("instructor_name"))
    calls = []

    def lookup(**kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(db, "get_zotistics_grades", lookup)
    context = {
        "term": "2026 Fall",
        "default_term": "2027 Winter",
        "allowed_query_terms": ["2026 Fall"],
        "recent_query_focus": {"terms": ["2025 Winter"]},
    }
    before = dict(context)
    returned = tools.dispatch("get_zotistics_grades", query, context=context)

    assert returned is result
    assert calls == [{key: query.get(key) for key in ("course_id", "instructor_name", "department")}]
    assert {key: context[key] for key in before} == before
    assert context["_zotistics_results"][0]["result"] is result


@pytest.mark.parametrize("status", ["no_data", "unavailable", "ambiguous_instructor", "invalid_query"])
def test_zotistics_db_preserves_negative_lookup_status(monkeypatch, status):
    from app import data

    result = {"found": False, "status": status, "source": "zotistics", "grades": None,
              "by_instructor": [], "reason": "fixture reason"}
    calls = []

    def lookup(**kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(data, "zotistics", SimpleNamespace(get_grade_distribution=lookup), raising=False)
    assert db.get_zotistics_grades(instructor_name="LEE", department="COMPSCI") is result
    assert calls == [{"course_id": None, "instructor_name": "LEE", "department": "COMPSCI"}]


@pytest.mark.parametrize("found", [True, False])
def test_zotistics_dispatch_reuses_normalized_query_and_keeps_instructor_scopes_separate(monkeypatch, found):
    calls = []

    def lookup(**kwargs):
        calls.append(kwargs)
        return {"found": found, "status": "ok" if found else "no_data", "source": "zotistics"}

    monkeypatch.setattr(db, "get_zotistics_grades", lookup)
    context = {}
    course = tools.dispatch("get_zotistics_grades", {"course_id": "CS161"}, context=context)
    repeated = tools.dispatch("get_zotistics_grades", {"course_id": "compsci 161", "department": "cs"}, context=context)
    professor = tools.dispatch("get_zotistics_grades", {
        "course_id": "CS161", "instructor_name": "Shindler", "department": "COMPSCI",
    }, context=context)

    assert repeated is course
    assert professor is not course
    assert len(calls) == 2
    assert len(context["_zotistics_results"]) == 2
    assert context["_zotistics_results"][0]["query"] == {
        "course_id": "COMPSCI 161", "instructor_name": None, "department": "COMPSCI",
    }


def test_invalid_department_result_does_not_poison_valid_course_query(monkeypatch):
    calls = []

    def lookup(**query):
        calls.append(query)
        invalid = query["department"] == "UNKNOWN_DEPARTMENT"
        return {"found": not invalid, "status": "invalid_query" if invalid else "ok", "source": "zotistics"}

    monkeypatch.setattr(db, "get_zotistics_grades", lookup)
    context = {}
    invalid = tools.dispatch("get_zotistics_grades", {
        "course_id": "CS161", "department": "UNKNOWN_DEPARTMENT",
    }, context=context)
    valid = tools.dispatch("get_zotistics_grades", {"course_id": "CS161"}, context=context)
    assert invalid["status"] == "invalid_query"
    assert valid["found"] is True
    assert len(calls) == 2


@pytest.mark.parametrize("language,expected", [
    ("zh", "查询 Zotistics 历史给分 · ICS33 · THORNTON, A."),
    ("en", "Checking Zotistics historical grades · ICS33 · THORNTON, A."),
])
def test_zotistics_progress_keeps_source_and_historical_scope(language, expected):
    assert tools.humanize_tool_call("get_zotistics_grades", {
        "course_id": "ICS33", "instructor_name": "THORNTON, A.",
    }, language) == expected


def test_agent_accepts_historical_grades_and_rmp_as_separate_tool_evidence(monkeypatch):
    grades = _evidence(instructor_name="THORNTON, A.")
    rating = {"found": True, "source": "db", "instructor": {
        "avg_rating": 4.2, "num_ratings": 20,
        "tier": {"tier": "mostly_positive", "label_en": "Mostly positive"},
    }}
    monkeypatch.setattr(db, "get_zotistics_grades", lambda **kwargs: grades)
    monkeypatch.setattr(db, "get_professor_rating", lambda *args, **kwargs: rating)
    monkeypatch.setattr(loop, "build_primary_workflow_plan", lambda *args: {"calls": []})
    client = ScriptedLLMClient(
        tool_response(
            tool_call("get_zotistics_grades", {"course_id": "ICS33", "instructor_name": "THORNTON, A."}, call_id="grades", index=0),
            tool_call("get_professor_rating", {"instructor_name": "THORNTON, A."}, call_id="rating", index=1),
        ),
        text_response("Historical grades and RMP ratings support separate assessments."),
    )
    messages = [{"role": "system", "content": AGENT_SYSTEM_PROMPT},
                {"role": "user", "content": "Is Thornton worth taking for ICS33?"}]

    async def collect():
        return [event async for event in loop.run_agent(
            messages, client=client, model="fixture", user_id="demo_001",
            term="2026 Fall", allowed_query_terms=["2026 Fall"],
        )]

    events = asyncio.run(collect())
    results = {message["tool_call_id"]: json.loads(message["content"])
               for message in client.calls[1].messages if message["role"] == "tool"}
    assert results["grades"] == grades
    assert {key: results["rating"][key] for key in rating} == rating
    assert results["rating"]["historical_grades"][0]["scope"] == "course_instructor"
    assert results["rating"]["historical_grades"][0]["grades"] == grades["grades"]
    assert events[-1]["tool_calls"] == 2
    assert "get_zotistics_grades" in {schema["function"]["name"] for schema in client.calls[0].tools}
    client.assert_exhausted()


def test_plain_offering_comparison_does_not_prefetch_zotistics(monkeypatch):
    requested = []

    async def lookup(course_id, terms):
        requested.append((course_id, terms))
        return {"offerings": [
            {"term": term, "offering_status": "offered", "instructors": ["Historical Professor"]}
            for term in terms
        ]}

    def forbidden_grades(**kwargs):
        pytest.fail("plain term offering comparisons must not fetch historical grades")

    monkeypatch.setattr(offerings, "get_course_offerings", lookup)
    monkeypatch.setattr(db, "get_zotistics_grades", forbidden_grades)
    client = ScriptedLLMClient(text_response("Offering comparison complete."))

    async def collect():
        return [event async for event in loop.run_agent(
            [{"role": "user", "content": "Compare ICS33 professors in 2025 Winter and 2026 Fall"}],
            client=client, model="fixture", user_id="demo_001", term="2027 Winter",
            allowed_query_terms=["2025 Winter", "2026 Fall"],
            query_term_source="comparison", offering_course_ids=["ICS33"],
        )]

    events = asyncio.run(collect())
    assert requested == [("ICS33", ["2025 Winter", "2026 Fall"])]
    assert events[-1]["tool_calls"] == 1
    client.assert_exhausted()


def test_prompt_preserves_grade_scope_and_offering_boundaries():
    historical = AGENT_SYSTEM_PROMPT.split("# Zotistics historical grading evidence", 1)[1].split("# Tools", 1)[0]
    assert "course_id and instructor_name together" in historical
    assert "cannot identify who teaches the target or future" in historical
    assert "do not add sample counts" in historical
    assert "plain offering or cross-term instructor comparisons" in historical
    term_rules = AGENT_SYSTEM_PROMPT.split("# Term resolution and offering evidence", 1)[1]
    assert "Do not fetch ratings, grades or meeting times unless the user asks for them." in term_rules
    assert "Never predict future professors" in term_rules
    assert "A course-wide average is not an individual professor's average." in AGENT_SYSTEM_PROMPT
    assert "REQUIRES a prior `summarize_professor_reviews` call" in AGENT_SYSTEM_PROMPT


def _install_zotistics_card_catalog(monkeypatch):
    from tests.test_agent_tools_schedule_validation import _install_schedule_validation_catalog

    _install_schedule_validation_catalog(monkeypatch)

    def section(course_id, code, number, kind, instructor):
        return {
            "course_id": course_id, "section_code": code, "section_num": number,
            "section_type": kind, "status": "OPEN", "days": "MW",
            "start_time": "09:00", "end_time": "10:20", "instructors": [instructor],
        }

    sections = {
        "COMPSCI161": [
            section("COMPSCI161", "20000", "A", "Lec", "SHINDLER, M."),
            section("COMPSCI161", "20001", "A1", "Dis", "ASSISTANT, T."),
            section("COMPSCI161", "20002", "B", "Lec", "OTHER, N."),
        ],
        "IN4MATX43": [
            section("IN4MATX43", "30000", "A", "Lec", "SHINDLER, M."),
        ],
    }
    monkeypatch.setattr(db, "get_sections", lambda course_id, term: {
        "found": True, "source": "test", "sections": sections[course_id],
    })
    monkeypatch.setattr(db, "get_professor_rating", lambda *args, **kwargs: {"found": False})


def _propose_test_cards(context):
    return tools.dispatch("propose_recommendation", {
        "items": [
            {"course_id": "COMPSCI161", "category": "elective", "reason": "Historical grade evidence available."},
            {"course_id": "IN4MATX43", "category": "elective", "reason": "Relevant project course."},
        ],
        "term": "2026 Fall",
    }, context=context)


def test_grade_queries_then_cards_keep_course_professor_and_ta_scopes_separate(monkeypatch):
    _install_zotistics_card_catalog(monkeypatch)
    course_history = _evidence(course_id="COMPSCI161")
    lecturer_history = _evidence(course_id="COMPSCI161", instructor_name="SHINDLER, M.")
    ta_history = _evidence(course_id="COMPSCI161", instructor_name="ASSISTANT, T.")
    career_history = _evidence(course_id=None, instructor_name="SHINDLER, M.")
    career_history["scope"] = "instructor"
    other_course_history = _evidence(course_id="MATH2A", instructor_name="SHINDLER, M.")
    responses = {
        ("CS161", None): course_history,
        ("CS161", "SHINDLER, M."): lecturer_history,
        ("CS161", "ASSISTANT, T."): ta_history,
        (None, "SHINDLER, M."): career_history,
        ("MATH2A", "SHINDLER, M."): other_course_history,
    }
    calls = []

    def lookup(**query):
        calls.append(query)
        return responses[(query["course_id"], query["instructor_name"])]

    monkeypatch.setattr(db, "get_zotistics_grades", lookup)
    context = {"user_id": "demo_001", "term": "2026 Fall",
               "allowed_query_terms": ["2026 Fall"], "pending_schedule": []}
    for course_id, instructor_name in responses:
        tools.dispatch("get_zotistics_grades", {
            "course_id": course_id, "instructor_name": instructor_name,
        }, context=context)

    assert len(calls) == 5
    assert _propose_test_cards(context)["ok"] is True
    assert len(calls) == 5, "card construction must reuse queried evidence without fetching more grades"
    cards = {card["course_id"]: card for card in context["_proposed_cards"]}
    cs_card = cards["COMPSCI161"]
    assert cs_card["zotistics_grades"] is course_history
    assert cs_card["grade_distribution"] is None
    groups = {group["letter"]: group for group in cs_card["section_groups"]}
    assert groups["A"]["primary"]["grade_history"] == [{
        "instructor": "SHINDLER, M.", **lecturer_history,
    }]
    assert groups["B"]["primary"]["grade_history"] == []
    assert groups["A"]["secondaries"][0]["instructors"] == ["ASSISTANT, T."]
    assert "grade_history" not in groups["A"]["secondaries"][0]
    assert cards["IN4MATX43"]["zotistics_grades"] is None
    assert cards["IN4MATX43"]["section_groups"][0]["primary"]["grade_history"] == []
    assert _propose_test_cards(context)["ok"] is True
    assert len(calls) == 5


def test_cards_without_queried_zotistics_evidence_do_not_fetch_or_invent_it(monkeypatch):
    _install_zotistics_card_catalog(monkeypatch)

    def forbidden_grades(**query):
        pytest.fail("cards must not initiate Zotistics network queries")

    monkeypatch.setattr(db, "get_zotistics_grades", forbidden_grades)
    context = {"user_id": "demo_001", "term": "2026 Fall",
               "allowed_query_terms": ["2026 Fall"], "pending_schedule": []}
    assert _propose_test_cards(context)["ok"] is True
    for card in context["_proposed_cards"]:
        assert card["zotistics_grades"] is None
        assert all(not group["primary"]["grade_history"] for group in card["section_groups"])
