from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.agent import loop, tools
from app.data import offerings, sessions
from app.terms import FixedClock, LOS_ANGELES, TermKey, calculate_week8_cutoff
from app.terms.intent import apply_semantic_plan, refine_query_scope
from app.terms.query_scope import resolve_query_scope
from app.terms.service import TermResolutionService
from app.terms.store import InMemoryTermStateStore, TermStateSnapshot
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response


def calendar_service(now: datetime) -> TermResolutionService:
    records = [
        {"term": "2026 Spring", "instructionStart": "2026-03-30", "quarterEnd": "2026-06-12"},
        {"term": "2026 Fall", "instructionStart": "2026-09-24", "quarterEnd": "2026-12-11"},
        {"term": "2027 Winter", "instructionStart": "2027-01-04", "quarterEnd": "2027-03-19"},
        {"term": "2027 Spring", "instructionStart": "2027-03-29", "quarterEnd": "2027-06-11"},
    ]
    state = TermStateSnapshot(automatic_term="2026 Fall", source="anteater", status="fresh",
                              calendar_records=records, last_success_at=now.isoformat())
    return TermResolutionService(InMemoryTermStateStore(state), clock=FixedClock(now))


def resolve(message, *, focus=None, now=None):
    now = now or datetime(2026, 11, 20, 12, tzinfo=LOS_ANGELES)
    service = calendar_service(now)
    current = service.current_term()
    default = service.automatic_term().canonical_name
    return resolve_query_scope(message, default, default, focus, service.clock.now(),
                               current_term=current.canonical_name if current else None,
                               relative_base=service.relative_base().canonical_name)


@pytest.mark.parametrize("start,expected", [
    ("2026-09-24", "2026-11-16T00:00:00-08:00"),
    ("2027-01-04", "2027-02-22T00:00:00-08:00"),
    ("2026-03-30", "2026-05-18T00:00:00-07:00"),
])
def test_week8_uses_teaching_weeks_and_pacific_dst(start, expected):
    assert calculate_week8_cutoff(start).isoformat() == expected


def test_utc_boundary_changes_default_without_publication_and_keeps_current():
    cutoff = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)
    before = calendar_service(cutoff - timedelta(seconds=1))
    after = calendar_service(cutoff)
    assert before.automatic_term().canonical_name == "2026 Fall"
    assert after.automatic_term().canonical_name == "2027 Winter"
    assert after.current_term().canonical_name == "2026 Fall"
    assert after.automatic_term().data_available is False


@pytest.mark.parametrize("date,default,current", [
    ((2026, 5, 18), "2026 Fall", "2026 Spring"),
    ((2026, 7, 10), "2026 Fall", None),
    ((2026, 12, 20), "2027 Winter", None),
    ((2027, 1, 5), "2027 Winter", "2027 Winter"),
])
def test_spring_skips_summer_and_breaks_keep_upcoming_default(date, default, current):
    service = calendar_service(datetime(*date, tzinfo=LOS_ANGELES))
    assert service.automatic_term().canonical_name == default
    assert (service.current_term().canonical_name if service.current_term() else None) == current


@pytest.mark.parametrize("message,expected", [
    ("本学期 ICS 33 谁教", ("2026 Fall",)),
    ("this quarter ICS 33", ("2026 Fall",)),
    ("下学期 ICS 33 谁教", ("2027 Winter",)),
    ("ICS 33 的开课情况", ("2027 Winter",)),
    ("对比 2025 Winter 和 2026 Fall 的 ICS 33", ("2025 Winter", "2026 Fall")),
    ("对比 2025 Winter 和下学期", ("2025 Winter", "2027 Winter")),
    ("比较本学期和下学期", ("2026 Fall", "2027 Winter")),
    ("Winter 的 ICS 33 谁教", ("2027 Winter",)),
    ("last Winter 的 ICS 33 谁教", ("2026 Winter",)),
    ("next Fall 的 ICS 33 谁教", ("2027 Fall",)),
])
def test_user_examples_resolve_independently_of_ui_default(message, expected):
    assert resolve(message).canonical_terms == expected


def test_followup_changes_course_without_changing_target_and_explicit_current_wins():
    focus = {"terms": ["2025 Winter"], "course_ids": ["ICS33"]}
    followup = resolve("那 ICS 32 呢", focus=focus)
    assert followup.canonical_terms == ("2025 Winter",)
    assert followup.course_ids == ("ICS32",)
    assert resolve("那门课本学期呢", focus=focus).canonical_terms == ("2026 Fall",)
    assert resolve("MATH 2A 开课情况", focus=focus).canonical_terms == ("2027 Winter",)


def test_only_winter_question_checks_other_seasons_and_two_completed_years():
    scope = resolve("ICS 33 是不是只有 Winter 开")
    assert scope.intent == "offering_pattern"
    assert scope.canonical_terms == (
        "2024 Fall", "2025 Winter", "2025 Spring", "2025 Fall", "2026 Winter", "2026 Spring",
    )


def test_semantic_followup_is_bounded_and_has_offline_fallback(monkeypatch):
    from app.llm import adapter
    scope = resolve("同样的问题也帮我看看 ICS 32")
    focus = {"terms": ["2025 Winter"], "course_ids": ["ICS33"]}
    plan = {"intent": "lookup", "use_discussion": True}
    kwargs = {"default_term": "2027 Winter", "current_term": "2026 Fall", "focus": focus}
    assert apply_semantic_plan(scope, plan, **kwargs).canonical_terms == ("2025 Winter",)
    assert apply_semantic_plan(scope, {"intent": "lookup", "terms": ["2099 Fall"]}, **kwargs) == scope
    assert apply_semantic_plan(resolve("本学期"), plan, **kwargs).canonical_terms == ("2026 Fall",)
    monkeypatch.setattr(adapter, "LLM_ENABLED", True)

    async def classify(*args, **kwargs):
        return json.dumps(plan)

    monkeypatch.setattr(adapter, "_call_llm", classify)
    args = {**kwargs, "message": "同样的问题也帮我看看 ICS 32", "recent_turns": [],
            "uci_now": datetime(2026, 11, 20, tzinfo=LOS_ANGELES)}
    assert asyncio.run(refine_query_scope(scope, **args)).canonical_terms == ("2025 Winter",)

    async def failed(*args, **kwargs):
        raise TimeoutError()

    monkeypatch.setattr(adapter, "_call_llm", failed)
    assert asyncio.run(refine_query_scope(scope, **args)) == scope


def test_unpublished_future_uses_same_season_history_without_predicting_professor(monkeypatch):
    calls = []

    def sections(course, term):
        calls.append((course, term))
        if term == "2027 Winter":
            return {"sections": [], "source": "registrar_websoc",
                    "error_code": "websoc_term_unavailable"}
        return {"sections": [
            {"section_type": "Lec", "instructors": ["Professor A"]},
            {"section_type": "Lec", "instructors": ["Professor A", "STAFF"]},
            {"section_type": "Dis", "instructors": ["Teaching Assistant"]},
        ], "source": "db", "coverage_status": "complete"}

    monkeypatch.setattr(offerings.db, "get_sections", sections)
    service = calendar_service(datetime(2026, 11, 20, tzinfo=LOS_ANGELES))
    result = asyncio.run(offerings.get_course_offerings("ICS33", ["2027 Winter"], service=service))
    row = result["offerings"][0]
    assert result["query_terms"] == ["2027 Winter"]
    assert row["offering_status"] == "unpublished"
    assert row["instructors"] == []
    assert row["prediction"]["status"] == "possibly_offered"
    assert row["prediction"]["offered_in_years"] == 2
    assert row["prediction"]["professor_prediction"] is None
    assert [r["term"] for r in row["historical_reference"]] == ["2025 Winter", "2026 Winter"]
    assert row["historical_reference"][0]["instructors"] == ["Professor A"]
    assert row["historical_reference"][0]["instructors_pending"] is True
    assert set(calls) == {("ICS33", t) for t in ("2027 Winter", "2025 Winter", "2026 Winter")}


@pytest.mark.parametrize("raw,expected", [
    ({"source": "db", "coverage_status": "partial"}, "unavailable"),
    ({"source": "db", "coverage_status": "stale"}, "unavailable"),
    ({"source": "none", "error_code": "TimeoutError"}, "unavailable"),
    ({"source": "db", "coverage_status": "complete"}, "unavailable"),
    ({"authoritative": True, "offering_status": "not_offered"}, "not_offered"),
])
def test_no_record_does_not_imply_unpublished_or_not_offered(raw, expected):
    assert offerings.summarize_offering(raw, "2027 Winter", future=True)["offering_status"] == expected


def test_failed_future_query_does_not_fetch_history_or_make_prediction(monkeypatch):
    calls = []

    def fail(course, term):
        calls.append(term)
        raise TimeoutError()

    monkeypatch.setattr(offerings.db, "get_sections", fail)
    result = asyncio.run(offerings.get_course_offerings("ICS33", ["2027 Winter"],
                        service=calendar_service(datetime(2026, 11, 20, tzinfo=LOS_ANGELES))))
    assert result["offerings"][0]["offering_status"] == "unavailable"
    assert "prediction" not in result["offerings"][0]
    assert calls == ["2027 Winter"]


def test_partial_cache_comparison_checks_each_term_and_keeps_distinct_evidence(monkeypatch):
    monkeypatch.setattr(offerings.db, "get_catalog", lambda _term: None)
    monkeypatch.setattr(offerings.db, "get_term_coverage", lambda _term: {"coverage_status": "partial"})

    def official(**kwargs):
        sections = ([{"section_type": "Lec", "instructors": ["Historical Professor"]}]
                    if kwargs["term"] == "Winter 2025" else [])
        return {"ok": True, "authoritative": True,
                "offering_status": "offered" if sections else "not_offered", "sections": sections,
                "source_url": "https://www.reg.uci.edu/perl/WebSoc", "retrieved_at": "2026-09-18T12:00:00Z"}

    primary = Mock(side_effect=official)
    secondary = Mock()
    monkeypatch.setattr(offerings.db.websoc_workflow, "fetch_websoc_course_offering", primary)
    monkeypatch.setattr(offerings.db.anteater, "fetch_sections", secondary)
    result = asyncio.run(offerings.get_course_offerings("EECS70A", ["2025 Winter", "2026 Fall"],
                        service=calendar_service(datetime(2026, 11, 20, tzinfo=LOS_ANGELES))))

    assert {call.kwargs["term"] for call in primary.call_args_list} == {"Winter 2025", "Fall 2026"}
    assert primary.call_count == 2
    secondary.assert_not_called()
    winter, fall = result["offerings"]
    assert (winter["term"], winter["offering_status"], winter["instructors"]) == (
        "2025 Winter", "offered", ["Historical Professor"])
    assert (fall["term"], fall["offering_status"], fall["instructors"]) == ("2026 Fall", "not_offered", [])
    assert winter["authoritative"] and fall["authoritative"]
    assert all(row["retrieved_at"] and row["source_url"] and row["lookup_attempts"] for row in result["offerings"])


@pytest.mark.parametrize("error_code,expected,lookup_count", [
    ("websoc_term_unavailable", "unpublished", 3),
    ("TimeoutError", "unavailable", 1),
    ("websoc_course_result_missing", "unavailable", 1),
])
def test_future_history_requires_publication_evidence_through_full_fallback(
    monkeypatch, error_code, expected, lookup_count,
):
    monkeypatch.setattr(offerings.db, "get_catalog", lambda _term: None)
    monkeypatch.setattr(offerings.db, "get_term_coverage", lambda _term: {"coverage_status": "partial"})

    def official(**kwargs):
        if kwargs["term"] == "Winter 2027":
            return {"ok": False, "error_code": error_code, "offering_status": "unavailable",
                    "message": "Official lookup could not return sections"}
        return {"ok": True, "authoritative": True, "offering_status": "offered",
                "sections": [{"section_type": "Lec", "instructors": ["Historical Professor"]}]}

    primary = Mock(side_effect=official)
    secondary = Mock(return_value=None)
    monkeypatch.setattr(offerings.db.websoc_workflow, "fetch_websoc_course_offering", primary)
    monkeypatch.setattr(offerings.db.anteater, "fetch_sections", secondary)
    result = asyncio.run(offerings.get_course_offerings("EECS70A", ["2027 Winter"],
                        service=calendar_service(datetime(2026, 11, 20, tzinfo=LOS_ANGELES))))
    row = result["offerings"][0]
    assert row["offering_status"] == expected
    assert row["instructors"] == []
    assert primary.call_count == lookup_count
    if expected == "unpublished":
        secondary.assert_not_called()
        assert [item["term"] for item in row["historical_reference"]] == ["2025 Winter", "2026 Winter"]
        assert row["prediction"]["status"] == "possibly_offered"
        assert row["prediction"]["professor_prediction"] is None
    else:
        secondary.assert_called_once()
        assert "historical_reference" not in row and "prediction" not in row
        assert row["lookup_attempts"][0]["error_code"] == error_code


def test_past_missing_official_term_is_not_future_unpublished():
    row = offerings.summarize_offering(
        {"source": "registrar_websoc", "error_code": "websoc_term_unavailable"},
        "2025 Winter", future=False,
    )
    assert row["offering_status"] == "unavailable"


def test_sse_no_match_does_not_infer_no_offering_from_legacy_complete_coverage():
    event = {}
    loop._add_offering_event_fields(event, "get_sections", {
        "found": False, "source": "db", "sections": [], "coverage_status": "complete",
    })
    assert event["offering_status"] == "unavailable"


def test_multi_term_tool_guard_rejects_terms_outside_request():
    context = {"allowed_query_terms": ["2025 Winter", "2026 Fall"]}
    args, error = tools.enforce_query_term_scope("get_course_offerings",
        {"course_id": "ICS33", "terms": ["Winter 2025", "Fall 2026"]}, context=context)
    assert error is None
    assert args["terms"] == context["allowed_query_terms"]
    _, error = tools.enforce_query_term_scope("get_course_offerings",
        {"course_id": "ICS33", "terms": ["2027 Winter"]}, context=context)
    assert error and "outside" in error


def test_comparison_fetches_both_terms_before_model_answer(monkeypatch):
    requested = []

    async def lookup(course_id, terms):
        requested.append((course_id, terms))
        return {"offerings": [{"term": terms[0], "offering_status": "offered", "instructors": ["Professor A"]},
                              {"term": terms[1], "offering_status": "not_offered", "instructors": []}]}

    monkeypatch.setattr(offerings, "get_course_offerings", lookup)
    client = ScriptedLLMClient(text_response("Comparison complete."))

    async def collect():
        return [event async for event in loop.run_agent(
            [{"role": "user", "content": "Compare ICS 33 in 2025 Winter and 2026 Fall"}],
            client=client, model="fake", user_id="demo_001", term="2027 Winter",
            allowed_query_terms=["2025 Winter", "2026 Fall"], query_term_source="comparison",
            offering_course_ids=["ICS33"],
        )]

    events = asyncio.run(collect())
    assert requested == [("ICS33", ["2025 Winter", "2026 Fall"])]
    assert events[0]["type"] == "tool_call_start"
    assert events[0]["server_forced"] is True
    evidence = json.dumps(client.calls[0].messages)
    assert "Professor A" in evidence and "not_offered" in evidence
    assert events[-1]["tool_calls"] == 1


def test_eecs_offering_answer_gets_official_evidence_and_blocks_redundant_search(monkeypatch):
    official = Mock(return_value={
        "ok": True, "authoritative": True, "offering_status": "not_offered", "sections": [],
        "source_url": "https://www.reg.uci.edu/perl/WebSoc", "retrieved_at": "2026-09-18T12:00:00Z",
    })
    secondary = Mock()
    monkeypatch.setattr(offerings.db.websoc_workflow, "fetch_websoc_course_offering", official)
    monkeypatch.setattr(offerings.db.anteater, "fetch_sections", secondary)
    client = ScriptedLLMClient(
        tool_response(tool_call("web_search", {
            "query": "UCI EECS70A Fall 2026 offering WebSoc",
            "reason": "Double-check the offering",
        }, call_id="redundant_search")),
        text_response("The official timetable currently lists no matching course."),
    )
    messages = [{"role": "user", "content": "EECS70A开课了吗?"}]

    async def collect():
        return [event async for event in loop.run_agent(
            messages, client=client, model="fake", user_id="demo_001", term="2026 Fall",
            allowed_query_terms=["2026 Fall"], offering_course_ids=["EECS70A"],
        )]

    events = asyncio.run(collect())
    official.assert_called_once_with(term="Fall 2026", department="EECS", course_number="70A")
    secondary.assert_not_called()
    assert events[0]["name"] == "get_course_offerings" and events[0]["server_forced"]
    assert "not_offered" in json.dumps(client.calls[0].messages)
    blocked = next(json.loads(message["content"]) for message in messages
                   if message.get("tool_call_id") == "redundant_search")
    assert blocked["error_code"] == "definitive_offering_already_resolved"
    client.assert_exhausted()


def test_one_resolved_term_does_not_block_research_for_unresolved_comparison():
    context = {}
    loop._record_definitive_course_offering(context, "get_course_offerings", {}, {
        "course_id": "EECS70A", "offerings": [
            {"term": "2025 Winter", "source": "registrar_websoc", "authoritative": True,
             "offering_status": "offered"},
            {"term": "2026 Fall", "source": "none", "offering_status": "unavailable"},
        ],
    })
    assert loop._definitive_offering_stop_reason("web_search", {
        "query": "UCI EECS70A Fall 2026 offering WebSoc",
    }, context=context) is None


def test_v2_manual_metadata_migrates_without_losing_discussion_or_schedule(tmp_path):
    folder = tmp_path / "u1" / "sessions" / "sess_old"
    folder.mkdir(parents=True)
    original = {"term_schema_version": 2, "term_mode": "manual", "default_term": "2025 Winter",
                "term_updated_by": "user_ui", "recent_query_focus": {"terms": ["2025 Winter"]}}
    (folder / "meta.json").write_text(json.dumps(original))
    (folder / "state.json").write_text('{"pending_schedule":[{"term":"2025 Winter"}]}')
    assert sessions.migrate_term_metadata(tmp_path)["migrated"] == 1
    migrated = json.loads((folder / "meta.json").read_text())
    assert migrated["term_mode"] == "auto"
    assert migrated["recent_query_focus"] == original["recent_query_focus"]
    assert (folder / "state.json").read_text() == '{"pending_schedule":[{"term":"2025 Winter"}]}'
    assert sessions.migrate_term_metadata(tmp_path)["skipped"] == 1
