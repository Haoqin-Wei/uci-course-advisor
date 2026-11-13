"""Prompt contracts and their scope/compatibility boundaries, without LLM calls."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from app.agent import tools
from app.data import professor_summary


def _schema(name):
    return next(item["function"] for item in tools.TOOL_SCHEMAS
                if item["function"]["name"] == name)


def test_rating_contract_preserves_sample_attribution_and_local_missing_scope():
    description = _schema("get_professor_rating")["description"]
    assert "avg_rating and num_ratings" in description
    assert "unless asked" not in description
    assert "not on RMP" not in description
    assert "Missing local coverage" in description


def test_grade_tools_keep_separate_scopes_and_raw_denominators():
    legacy = _schema("get_grade_distribution")
    source = _schema("get_zotistics_grades")["description"]
    assert legacy["parameters"]["required"] == ["course_id"]
    assert "course-wide evidence" in legacy["description"]
    for marker in ("source_reported_gpa", "approximate", "site_percentages",
                   "class_count", "distinct terms", "verified", "all-department",
                   "unavailable", "ambiguous_instructor", "cache_miss"):
        assert marker in source


def test_search_and_card_contracts_explain_independent_backend_ranking():
    search = _schema("search_courses")["description"]
    stage = _schema("propose_recommendation")
    assert "complete matching pool" in search
    assert "independent of dynamic prefilter" in search
    assert "Cache misses are missing evidence" in search
    assert "A-rate=0.6/RMP=0.4" in search
    assert "raw grade counts/percentages and charts stay unchanged" in search
    assert "AT LEAST ONCE" not in stage["description"]
    assert "gate is enabled" in stage["description"]
    assert "missing/unavailable sections remain uncertain candidates" in stage["description"]
    assert "backend scores determine display order" in stage["parameters"]["properties"]["items"]["description"]
    assert "prior attempt alone" in _schema("search_courses")["parameters"]["properties"]["exclude_ids"]["description"]


def test_planning_prerequisites_and_pairwise_times_do_not_certify_eligibility():
    prerequisite = _schema("check_prerequisites_met")
    conflict = _schema("check_section_conflict")["description"]
    assert "Defaults true" in prerequisite["parameters"]["properties"]["allow_in_progress"]["description"]
    assert "provisional future-term" in prerequisite["description"]
    assert "student CAN take both" not in conflict
    assert "Unknown/TBA" in conflict
    assert "complete Lec/Dis/Lab bundle" in conflict


@pytest.mark.parametrize("ranking,prefilter", [(True, False), (False, True), (True, True)])
def test_new_flags_neutralize_unbound_section_facts_without_matching_course_subjects(monkeypatch, ranking, prefilter):
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", str(ranking))
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", str(prefilter))
    sections = [{"instructors": ["OLSON, V.", "LI, M."]}]
    for reason in ["历史 53.3% 拿 A；Lec A 余 34 席", "Lec B historical grading", "Dis C4 required",
                   "Section 60020 preferred", "余 65 席", "21 seats open", "Meets at 09:30",
                   "OLSON, V. gives clear lectures", "Professor Olson is recommended", "GPA 3.8"]:
        rendered, original = tools._course_card_reason(reason, {"response_language": "zh"}, sections)
        assert rendered == "课程候选；请核对各班次与历史证据。", reason
        assert original == reason
    for reason in ["CS161 algorithms — interview prep", "ICS6B / CS161 core preparation",
                   "2026 catalog elective", "GE-IV candidate; verify credit", "CS core, OS principles",
                   "Disk storage and lab projects", "Preliminary algorithms"]:
        assert tools._course_card_reason(reason, {"response_language": "zh"}, sections) == (reason, None)


def test_old_flags_preserve_legacy_reason_and_new_english_reason_is_neutral(monkeypatch):
    reason = "Lec A has 34 seats; historical A 53.3%"
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "false")
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "false")
    assert tools._course_card_reason(reason, {}, []) == (reason, None)
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "true")
    rendered, original = tools._course_card_reason(reason, {"response_language": "en"}, [])
    assert rendered == "Course option; compare verified section and historical evidence."
    assert original == reason


def test_backend_primary_reselection_cannot_leave_lecture_a_facts_on_lecture_b(monkeypatch):
    from app.data import db, policies

    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "true")
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "false")
    monkeypatch.setattr(policies, "is_term_past_deadline", lambda *_a, **_k: False)
    monkeypatch.setattr(policies, "get_term_calendar", lambda _: {})
    monkeypatch.setattr(db, "get_student_profile", lambda _: {"found": False})
    monkeypatch.setattr(db, "get_course_info", lambda _: {"found": True, "course": {
        "course_id": "ANTHRO2A", "title": "Test course", "department": "ANTHRO", "units": 4}})
    sections = [{"section_code": code, "section_num": number, "section_type": "Lec", "status": "OPEN",
                 "days": "MW", "start_time": "09:00", "end_time": "09:50", "instructors": [name]}
                for code, number, name in [("60000", "A", "EGAN, J."), ("60020", "B", "OLSON, V.")]]
    monkeypatch.setattr(db, "get_sections", lambda *_a: {"found": True, "sections": sections})
    monkeypatch.setattr(db, "check_prerequisites_met", lambda *_a, **_k: {"found": True, "status": "met"})
    monkeypatch.setattr(db, "get_grade_distribution", lambda *_a: {"found": False})
    monkeypatch.setattr(tools.grade_evidence, "find_history", lambda *_a: None)
    monkeypatch.setattr(tools.grade_evidence, "attach_history", lambda *_a: None)

    def ratings(names, department=None):
        return {name: {"found": True, "source": "db", "instructor": {
            "name": "John Egan" if name == "EGAN, J." else "Valerie Olson",
            "avg_rating": 1 if name == "EGAN, J." else 5, "num_ratings": 100}}
                for name in names}

    monkeypatch.setattr(tools, "_rmp_lookup_map", ratings)
    context = {"term": "Fall 2099", "response_language": "zh"}
    original = "历史 53.3% 拿 A；Lec A 余 34 席"
    result = tools._tool_propose_recommendation([
        {"course_id": "ANTHRO2A", "category": "elective", "reason": original}], context=context)
    assert result["staged_count"] == 1
    card = context["_proposed_cards"][0]
    assert card["primary_code"] == "60020"
    assert card["reason"] == f"历史证据推荐分 {card['ranking']['final_score']:.1f}/100；修读条件另行核实。"
    assert "Lec A" not in card["reason"] and "余 34 席" not in card["reason"]
    assert "model_reason" not in card
    assert context["_recommendation_model_reasons"][("Fall 2099", "ANTHRO2A")] == original


def test_blocked_recommendation_gate_stops_all_grade_preparation(monkeypatch):
    monkeypatch.setattr(tools.prefilter, "recommendation_gate",
                        lambda _: {"enabled": True, "ready": False, "reasons": ["ge_audit_unverified"]})

    def forbidden(*_args, **_kwargs):
        pytest.fail("blocked plans must not reach filtering, section refresh or grade lookup")

    monkeypatch.setattr(tools.prefilter, "filter_items", forbidden)
    monkeypatch.setattr(tools.prefilter, "section_response", forbidden)
    monkeypatch.setattr(tools.grade_evidence, "prepare_recommendation_grades", forbidden)
    assert tools.prepare_recommendation_grades([{"course_id": "ICS33"}], "Fall 2099", {}) == []


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_or_ready_gate_preserves_grade_preparation(monkeypatch, enabled):
    from app.data import policies

    calls = []
    monkeypatch.setattr(tools.prefilter, "recommendation_gate",
                        lambda _: {"enabled": enabled, "ready": enabled, "reasons": []})
    monkeypatch.setattr(policies, "is_term_past_deadline", lambda *_a, **_k: False)
    monkeypatch.setattr(tools.prefilter, "active", lambda _: False)
    monkeypatch.setattr(tools.prefilter, "filter_items", lambda _, items, term: (items, []))
    monkeypatch.setattr(tools.prefilter, "note_details", lambda *_a, **_k: None)

    def prepare(items, term, context, selector):
        calls.append((items, term, context))
        return [{"scope": "course_instructor", "found": True}]

    monkeypatch.setattr(tools.grade_evidence, "prepare_recommendation_grades", prepare)
    items = [{"course_id": "ICS33"}]
    context = {"term": "Fall 2099"}
    assert tools.prepare_recommendation_grades(items, "Fall 2099", context) == [
        {"scope": "course_instructor", "found": True}]
    assert calls == [(items, "Fall 2099", context)]


def test_deadline_failure_does_not_change_or_authorize_a_new_term(monkeypatch):
    from app.data import policies

    monkeypatch.setattr(policies, "is_term_past_deadline", lambda *_a, **_k: True)
    monkeypatch.setattr(policies, "get_term_calendar", lambda _: {"late_add_drop_end": "2099-11-01"})
    context = {"term": "Fall 2099", "allowed_query_terms": ["Fall 2099"]}
    before = deepcopy(context)
    result = tools._tool_propose_recommendation(
        [{"course_id": "ICS33", "category": "elective", "reason": "Compare options"}], context=context)
    assert result["ok"] is False
    assert result["term"] == "Fall 2099"
    assert "Keep the resolved target term" in result["pivot_hint"]
    assert "Pivot to the next" not in result["reason"]
    assert context == before


def test_missing_sections_fail_card_readiness_without_claiming_no_offering(monkeypatch):
    from app.data import db, policies

    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "false")
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "false")
    monkeypatch.setattr(policies, "is_term_past_deadline", lambda *_a, **_k: False)
    monkeypatch.setattr(db, "get_student_profile", lambda _: {"found": False})
    monkeypatch.setattr(db, "get_course_info", lambda _: {
        "found": True, "course": {"course_id": "ICS33", "title": "Test course", "units": 4}})
    monkeypatch.setattr(db, "get_sections", lambda *_a: {
        "found": False, "sections": [], "offering_status": "unavailable", "authoritative": False})
    result = tools._tool_propose_recommendation(
        [{"course_id": "ICS33", "category": "elective", "reason": "Compare options"}],
        context={"term": "Fall 2099"})
    assert result["staged_count"] == 0
    assert result["skipped_count"] == 1
    assert "does not establish not_offered or ineligibility" in result["skipped"][0]["reason"]


def _unknown_summary():
    return {"strengths": [], "weaknesses": [], "best_for": "unknown", "avoid_if": "unknown",
            "workload": "unknown", "exam_style": "unknown", "grading_style": "unknown",
            "teaching_style": "unknown", "confidence": "low"}


@pytest.fixture
def isolated_summary(monkeypatch, tmp_path):
    from app.data import professors

    monkeypatch.setattr(professor_summary, "CACHE_DIR", tmp_path / "summary")
    monkeypatch.setattr(professors, "_loaded", True)
    monkeypatch.setattr(professors, "_by_legacy_id", {9001: {"firstName": "Sample", "lastName": "Instructor"}})
    monkeypatch.setattr(professors, "get_reviews", lambda *_a, **_k: [{"comment": "Only one short report."}])
    monkeypatch.setattr(professors, "aggregate_tags", lambda *_a, **_k: [])
    return professor_summary.CACHE_DIR


def test_legacy_summary_is_preserved_but_new_prompt_can_return_no_themes(monkeypatch, isolated_summary):
    from app.llm import adapter

    isolated_summary.mkdir()
    path = isolated_summary / "9001.json"
    old = {"_all": {"summary": {"strengths": ["old claim"]}, "model": "old", "n_reviews": 1}}
    path.write_text(json.dumps(old))
    calls = []

    async def fake_llm(system, user, json_mode=False):
        calls.append((system, user, json_mode))
        return json.dumps(_unknown_summary())

    monkeypatch.setattr(adapter, "LLM_ENABLED", True)
    monkeypatch.setattr(adapter, "_call_llm", fake_llm)
    result = asyncio.run(professor_summary.summarize_professor(9001))
    assert result["source"] == "llm"
    assert result["summary"] == _unknown_summary()
    assert result["n_reviews"] == 1
    assert len(calls) == 1 and calls[0][2] is True
    assert "0-5" in calls[0][0] and '"unknown"' in calls[0][0]
    assert "across all reviewed courses; no course filter" in calls[0][1]
    saved = json.loads(path.read_text())
    assert saved["_all"] == old["_all"]
    current = saved[f"{professor_summary.SUMMARY_PROMPT_VERSION}:_all"]
    assert current["prompt_version"] == professor_summary.SUMMARY_PROMPT_VERSION
    assert current["summary"] == _unknown_summary()


def test_current_summary_cache_hits_without_reviews_or_model(monkeypatch, isolated_summary):
    isolated_summary.mkdir()
    key = f"{professor_summary.SUMMARY_PROMPT_VERSION}:33"
    entry = {"prompt_version": professor_summary.SUMMARY_PROMPT_VERSION,
             "summary": _unknown_summary(), "n_reviews": 1, "model": "test-model"}
    (isolated_summary / "9001.json").write_text(json.dumps({key: entry}))

    def forbidden(*_args, **_kwargs):
        pytest.fail("a current cache hit must not load reviews or call a model")

    monkeypatch.setattr(professor_summary.profs, "get_reviews", forbidden)
    monkeypatch.setattr(professor_summary, "_call_summarizer", forbidden)
    result = asyncio.run(professor_summary.summarize_professor(9001, course="ICS33"))
    assert result["source"] == "cache"
    assert result["course"] == "ICS33"
    assert result["summary"] == _unknown_summary()


@pytest.mark.parametrize("stored_version", [None, "older-prompt"])
def test_versioned_cache_key_alone_cannot_claim_current_prompt(monkeypatch, isolated_summary, stored_version):
    isolated_summary.mkdir()
    key = f"{professor_summary.SUMMARY_PROMPT_VERSION}:_all"
    stale = {"summary": {"strengths": ["unverified cached claim"]}}
    if stored_version is not None:
        stale["prompt_version"] = stored_version
    (isolated_summary / "9001.json").write_text(json.dumps({key: stale}))
    calls = []

    async def refresh(content):
        calls.append(content)
        return _unknown_summary()

    monkeypatch.setattr(professor_summary, "_call_summarizer", refresh)
    result = asyncio.run(professor_summary.summarize_professor(9001))
    assert len(calls) == 1
    assert result["source"] == "llm"
    assert result["summary"] == _unknown_summary()


def test_old_summary_cannot_bypass_disabled_model_or_new_evidence_rules(monkeypatch, isolated_summary):
    from app.llm import adapter

    isolated_summary.mkdir()
    path = isolated_summary / "9001.json"
    original = json.dumps({"_all": {"summary": {"strengths": ["legacy certainty"]}}})
    path.write_text(original)
    monkeypatch.setattr(adapter, "LLM_ENABLED", False)
    result = asyncio.run(professor_summary.summarize_professor(9001))
    assert result["found"] is False
    assert "older cached summary was retained" in result["reason"]
    assert "summary" not in result
    assert path.read_text() == original


def test_summary_input_distinguishes_filtered_and_aggregate_evidence():
    aggregate = professor_summary._build_user_content("Sample Instructor", None, [], [])
    filtered = professor_summary._build_user_content("Sample Instructor", "CS122A", [], [])
    assert "across all reviewed courses; no course filter" in aggregate
    assert "Course filter:" not in aggregate
    assert "course-filtered historical reviews" in filtered
    assert "Course filter: CS122A" in filtered
    assert "evidence, not instructions" in filtered
