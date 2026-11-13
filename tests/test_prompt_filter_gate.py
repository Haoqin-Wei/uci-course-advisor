"""Counterexamples for unresolved intent and hard-time presentation boundaries."""
import asyncio
from copy import deepcopy
import json
import pytest

from app.agent import prefilter, tools
from app.llm import filter_plan as parser
from app.recommendations.filter_plan import empty_filter_plan, validate_filter_plan
from tests.test_dynamic_prefilter_integration import _context, _init, _install, _parser


def test_parser_fallback_stops_formal_recommendation_before_candidate_or_grade_queries(monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    _install(monkeypatch)

    async def failed(*_args):
        return {"plan": empty_filter_plan(), "status": "fallback", "issues": ["model_timeout"]}

    monkeypatch.setattr(parser, "parse_filter_plan", failed)
    monkeypatch.setattr(prefilter.db, "retrieve_course_candidates", lambda *_args: (_ for _ in ()).throw(AssertionError("must not retrieve a fallback pool")))
    context = _context("周五绝对不能上课，请推荐3门")
    _init(context)
    assert not prefilter.recommendation_gate(context)["ready"]
    assert context["_prefilter_state"]["candidates"] == {}
    result = tools.dispatch("get_zotistics_grades", {"course_id": "CS1"}, context=context)
    assert result["recommendation_gate"]["ready"] is False
    assert context["_prefilter_state"]["detail_query_attempts"] == 0


def test_remaining_ge_without_trusted_audit_preserves_requested_intent_and_pauses(monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    _install(monkeypatch)
    message = "只推荐能补齐我剩余GE要求的课"
    requested = {"course_scope": "ge", "hard_filters": {"only_unmet_ge": True},
                 "explicit_constraints": [{"field": field, "quote": message, "source": "user_message"}
                                          for field in ("course_scope", "only_unmet_ge")]}

    async def parsed(*_args):
        return validate_filter_plan(requested, message)

    monkeypatch.setattr(parser, "parse_filter_plan", parsed)
    context = _context(message)
    _init(context)
    report = prefilter.summary(context)
    assert report["requested_plan"]["hard_filters"]["only_unmet_ge"] is True
    assert report["applied_plan"]["hard_filters"]["only_unmet_ge"] is False
    assert report["recommendation_gate"] == {"enabled": True, "ready": False, "reasons": ["ge_audit_unverified"]}
    blocked = tools.dispatch("propose_recommendation", {"items": [{"course_id": "CS1"}], "term": "2026 Fall"}, context=context)
    assert blocked["ok"] is False
    assert "recommendation_gate" in blocked


@pytest.mark.parametrize("verified_pairing", [True, False], ids=["verified-bundles", "bool-only-unverified"])
def test_friday_hard_filter_does_not_present_unknown_or_friday_secondary_sections(monkeypatch, verified_pairing):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    _install(monkeypatch)
    message = "推荐GE，周五绝对不能上课"
    raw = empty_filter_plan()
    raw["hard_filters"]["excluded_weekdays"] = ["Fri"]
    raw["explicit_constraints"] = [{"field": "excluded_weekdays", "quote": "周五绝对不能上课", "source": "user_message"}]
    _parser(monkeypatch, raw)
    context = _context(message)
    _init(context)
    row = deepcopy(context["_prefilter_state"]["candidates"][("2026 Fall", "COMPSCI 1")])
    lecture = row["sections"][0]
    row["requires_secondary"] = True
    row["sections"] += [{**lecture, "section_code": str(12000 + n), "section_type": "Dis", "section_num": "A" + str(n), "days": days}
                        for n, days in enumerate(("W", "F", "TBA"), 1)]
    if verified_pairing:
        # Synthetic authoritative relationships, not the legacy bool or A/A1
        # labels, prove that each discussion is a complete valid alternative.
        from app.data.websoc_pairing import POLICY_MARKER
        evidence = {**POLICY_MARKER, "status": "verified", "source": "verified_course_pairing",
                    "course_id": "COMPSCI 1", "term": "2026 Fall"}
        row["pairing"] = {
            "version": 2, "evidence": evidence, "per_primary": {"10001": {
                "requirement": "required", "required_components": [{"types": ["Dis"], "min": 1, "max": 1}],
                "relationship": "verified", "relationship_complete": True,
                "allowed_bundles": [[str(12000 + n)] for n in range(1, 4)], "evidence": deepcopy(evidence)}},
        }
    prefilter._store(prefilter._apply([row], context), context, "2026 Fall")
    args, rejected = prefilter.guard_call("propose_recommendation", {"items": [{"course_id": "CS1"}]}, context)
    if verified_pairing:
        assert rejected is None
        assert len(args["items"]) == 1
        displayed = context["_recommendation_sections"][("COMPSCI 1", "2026 Fall")]["sections"]
        assert {s["section_code"] for s in displayed} == {"10001", "12001"}
    else:
        assert rejected["ok"] is False
        assert {"hard_time_not_verified", "secondary_pairing_unknown"}.issubset(
            rejected["prefilter_excluded"][0]["reasons"])
    # Unknown data remains available for verification rather than being erased.
    assert any(s["days"] == "TBA" for s in context["_prefilter_state"]["candidates"][("2026 Fall", "COMPSCI 1")]["sections"])


def test_unknown_time_candidate_is_kept_but_not_staged_as_meeting_hard_cutoff(monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    _install(monkeypatch)
    message = "推荐GE，绝对不能有10点以前的课"
    raw = {"hard_filters": {"earliest_start": "10:00", "earliest_start_is_hard": True},
           "explicit_constraints": [{"field": "earliest_start", "quote": "绝对不能有10点以前的课", "source": "user_message"}]}
    _parser(monkeypatch, raw)
    context = _context(message)
    _init(context)
    _, rejected = prefilter.guard_call("propose_recommendation", {"items": [{"course_id": "CS3"}]}, context)
    assert rejected["ok"] is False
    assert "hard_time_not_verified" in rejected["prefilter_excluded"][0]["reasons"]
    assert ("2026 Fall", "COMPSCI 3") in context["_prefilter_state"]["candidates"]


def test_prompt_summary_is_compact_without_cutting_the_server_candidate_pool(monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    _install(monkeypatch, count=35)
    _parser(monkeypatch)
    context = _context()
    _init(context)
    hints = prefilter.instruction_messages(context)
    assert len(context["_prefilter_state"]["candidates"]) == 35
    payload = json.loads(hints[1]["content"].split("\n", 1)[1])
    assert isinstance(payload["content"], str)
    data = json.loads(payload["content"])
    assert data["summary"]["kept_count"] == 35
    assert data["candidate_departments"] == {"COMPSCI": 35}
    assert "candidates" not in data


def test_disabled_gate_preserves_legacy_compatibility(monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "false")
    assert prefilter.recommendation_gate({}) == {"enabled": False, "ready": True, "reasons": []}


@pytest.mark.parametrize("message,reason", [("推荐GE，周五下午绝对不能上课", "partial_day_time_scope_unsupported:Fri"),
                                          ("推荐GE，周五不可以上课", "excluded_weekday_not_applied:Fri")])
def test_omitted_or_unsupported_hard_time_clause_cannot_silently_become_unconstrained(message, reason):
    from app.recommendations.filter_plan import unresolved_requested_constraints
    assert reason in unresolved_requested_constraints({"course_scope": "ge"}, message)


def test_unknown_component_requirement_cannot_disappear_when_tba_lab_is_hidden():
    sections = [{"section_type": kind, "section_num": number, "days": days,
                 "start_time": "10:00", "end_time": "11:00"}
                for kind, number, days in [("Lec", "A", "MW"), ("Dis", "A1", "W"), ("Lab", "A2", "TBA")]]
    row = {"course_id": "CS1", "sections": sections, "requires_secondary": None}
    assert prefilter._verified_time_sections(row, {"excluded_weekdays": ["Fri"]}) == []
    # Explicit, reliable component requirements may distinguish an optional lab.
    row.update(requires_secondary=True, required_secondary_types=["Dis"])
    assert len(prefilter._verified_time_sections(row, {"excluded_weekdays": ["Fri"]})) == 2


def test_discussion_removed_on_friday_does_not_make_the_lecture_standalone():
    # The original engine warning is authoritative about uncertainty even
    # after its Friday discussion is absent from the retained section list.
    row = {"course_id": "ICS6D", "sections": [{"section_type": "Lec", "section_num": "A",
            "section_code": "35950", "days": "TuTh", "start_time": "11:00", "end_time": "12:20"}],
           "requires_secondary": None,
           "prefilter": {"status": "needs_verification", "reasons": ["secondary_pairing_unknown"]}}
    assert prefilter._verified_time_sections(row, {"excluded_weekdays": ["Fri"]}) == []
