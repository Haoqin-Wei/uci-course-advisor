from __future__ import annotations

import pytest

from app.agent import tools
from app.catalog.term import Term
from app.catalog.types import CourseRecord, CourseRef, Provenance, SectionRecord
from app.catalog.view import CatalogView
from app.data import db, recommendations, zotistics


TERM = Term(2026, "Fall")


@pytest.fixture(autouse=True)
def isolated_ranking_config(monkeypatch):
    for key in ("RECOMMENDATION_RANKING_ENABLED", "RECOMMENDATION_RANKING_WEIGHTS",
                "RECOMMENDATION_RANKING_RELIABILITY"):
        monkeypatch.delenv(key, raising=False)


def _catalog(monkeypatch, count=1, *, sections_by_number=None):
    provenance = Provenance(TERM.term_id, TERM.term_id, "ranking_fixture")
    courses, sections = [], []
    for number in range(1, count + 1):
        ref = CourseRef("COMPSCI", str(number))
        courses.append(CourseRecord(ref, title=f"Course {number}", units=4,
                                    ge_categories=("II",), provenance=provenance))
        definitions = (sections_by_number or {}).get(number, [
            {"code": str(10000 + number), "instructors": ["LECTURER, A."]},
        ])
        for index, definition in enumerate(definitions):
            code = definition.get("code", str(10000 + number * 10 + index))
            sections.append(SectionRecord(
                section_id=f"{TERM.term_id}_{code}", course=ref, section_code=code,
                term_id=TERM.term_id, instructors=tuple(definition.get("instructors", [])),
                section_type=definition.get("type", "Lec"), section_num=definition.get("num", "A"),
                days="MW", start_time="09:00", end_time="10:20", status=definition.get("status", "OPEN"),
                is_cancelled=definition.get("is_cancelled", False), provenance=provenance,
            ))
    view = CatalogView(TERM, courses, sections, provenance)
    lookup = lambda term: view if term == TERM else None
    monkeypatch.setattr(db, "get_catalog", lookup)
    monkeypatch.setattr(recommendations, "get_catalog", lookup)
    return view


def _history(course_id, instructor_name, rate=.8, *, scope=None):
    scope = scope or ("course_instructor" if course_id and instructor_name else "course" if course_id else "instructor")
    return {
        "found": True, "status": "ok", "source": "zotistics", "scope": scope,
        "source_url": "https://zotistics.com/", "historical_only": True,
        "course_id": course_id, "instructor_name": instructor_name,
        "grades": {"course_id": course_id, "instructor_name": instructor_name,
                   "scope": scope, "letter_counts": {"A": rate * 100, "B": (1 - rate) * 100,
                                                       "C": 0, "D": 0, "F": 0},
                   "graded": 100, "samples": 105, "pct_A": rate * 100},
    }


def _rating(value=4, n=10):
    return {"found": True, "source": "rmp_snapshot", "instructor": {
        "avg_rating": value, "num_ratings": n,
    }}


def _context():
    return {"user_id": "demo_001", "term": "2026 Fall", "default_term": "2026 Fall",
            "allowed_query_terms": ["2026 Fall"], "pending_schedule": []}


def test_default_flag_retains_first_30_from_the_shared_full_catalog_pool(monkeypatch):
    _catalog(monkeypatch, count=35)

    def forbidden_rank(*args, **kwargs):
        pytest.fail("default search must retain its existing catalog order")

    monkeypatch.setattr(recommendations, "rank_courses", forbidden_rank)
    full = db.retrieve_course_candidates("2026 Fall", department="COMPSCI")
    result = tools.dispatch("search_courses", {"term": "2026 Fall", "department": "COMPSCI"}, context=_context())
    assert len(full["courses"]) == 35
    assert full["truncated"] is False
    assert result["courses"] == full["courses"][:30]
    assert result["total_found"] == 35
    assert result["truncated"] is True
    assert not result.get("ranking_enabled", False)


def test_enabled_search_scores_candidates_after_position_30_before_topk(monkeypatch):
    _catalog(monkeypatch, count=35)
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "true")
    monkeypatch.setattr(db, "get_student_profile", lambda user_id: {"found": False})
    grade_calls = []

    def grade_lookup(*, course_id=None, instructor_name=None, department=None):
        grade_calls.append(course_id)
        return _history(course_id, instructor_name, 1 if course_id == "COMPSCI 35" else 0)

    monkeypatch.setattr(zotistics, "get_cached_query_result", grade_lookup)
    monkeypatch.setattr(recommendations, "_local_rating", lambda name, department: _rating())
    result = tools.dispatch("search_courses", {"term": "2026 Fall", "department": "COMPSCI"}, context=_context())
    assert result["ranking_enabled"] is True
    assert result["catalog_candidates"] == 35
    assert result["scored_candidates"] == 35
    assert len(grade_calls) == 35
    assert len(result["courses"]) == 30
    assert result["courses"][0]["course_id"] == "COMPSCI 35"
    assert result["courses"][0]["section_code"] == "10035"


def test_full_pool_filters_are_applied_before_scoring_without_a_30_item_cutoff(monkeypatch):
    _catalog(monkeypatch, count=35)
    queried = []

    def grades(*, course_id=None, instructor_name=None, department=None):
        queried.append(course_id)
        return _history(course_id, instructor_name, 1 if course_id == "COMPSCI 34" else 0)

    result = recommendations.rank_courses(
        "2026 Fall", department="COMPSCI", ge_category="II", exclude_ids=["COMPSCI35"],
        top_k=1, grade_lookup=grades, professor_lookup=lambda name, department: _rating(),
    )
    assert result["catalog_candidates"] == 34
    assert len(queried) == 34
    assert "COMPSCI 35" not in queried
    assert result["courses"][0]["course_id"] == "COMPSCI 34"


def test_default_rank_service_reads_local_rmp_and_grade_cache_without_live_fallbacks(monkeypatch):
    from app.data import anteater, grades, professors

    _catalog(monkeypatch, count=2)
    cached, local = [], []

    def forbidden(*args, **kwargs):
        pytest.fail("ranking must not fetch remote data or write a business cache")

    for module, name in ((zotistics, "get_grade_distribution"), (zotistics, "_request"),
                         (zotistics, "_write_cache"), (db, "get_zotistics_grades"),
                         (db, "get_grade_distribution"), (db, "get_professor_rating"),
                         (grades, "_fetch_aggregate"), (grades, "_write_cache"),
                         (anteater, "fetch_instructor"), (anteater, "fetch_sections")):
        monkeypatch.setattr(module, name, forbidden)

    def cache_lookup(**query):
        cached.append(query)
        return {"found": False, "status": "cache_miss", "scope": "course_instructor", "source": "zotistics"}

    def local_lookup(name, department=None):
        local.append((name, department))
        return {"firstName": "Alice", "lastName": "Lecturer", "avgRating": 4, "numRatings": 10}

    monkeypatch.setattr(zotistics, "get_cached_query_result", cache_lookup)
    monkeypatch.setattr(professors, "lookup_professor", local_lookup)
    result = recommendations.rank_courses("2026 Fall", top_k=None)
    assert len(cached) == 2
    assert local == [("LECTURER, A.", "COMPSCI")]
    assert result["grade_data_mode"] == "snapshot_or_cache_only"
    for course in result["courses"]:
        assert course["ranking"]["features"]["grade_a_rate"]["missing_reason"] == "cache_miss"
        assert course["ranking"]["final_score"] == 62.5


def test_local_rmp_fullname_mismatch_is_not_scored_as_the_requested_professor(monkeypatch):
    from app.data import professors

    _catalog(monkeypatch, sections_by_number={1: [
        {"code": "10001", "instructors": ["Alice Thornton"]},
    ]})
    monkeypatch.setattr(zotistics, "get_cached_query_result", lambda **query: {
        "found": False, "status": "cache_miss", "scope": "course_instructor", "source": "zotistics",
    })
    monkeypatch.setattr(professors, "lookup_professor", lambda *args, **kwargs: {
        "firstName": "Alex", "lastName": "Thornton", "avgRating": 4.2, "numRatings": 385,
    })
    course = recommendations.rank_courses("2026 Fall")["courses"][0]
    assert course["instructor_name"] == "Alice Thornton"
    assert course["ranking"]["features"]["professor_rating"]["valid"] is False
    assert course["ranking"]["final_score"] is None


def test_two_lecturers_have_distinct_pair_scores_and_best_section_wins_without_ta_lookup(monkeypatch):
    _catalog(monkeypatch, sections_by_number={1: [
        {"code": "10001", "num": "A", "instructors": ["WEAK, A."]},
        {"code": "10002", "num": "B", "instructors": ["STRONG, B."]},
        {"code": "10003", "num": "A1", "type": "Dis", "instructors": ["ASSISTANT, T."]},
    ]})
    queried = []

    def grades(*, course_id=None, instructor_name=None, department=None):
        queried.append(instructor_name)
        return _history(course_id, instructor_name, 1 if instructor_name == "STRONG, B." else 0)

    result = recommendations.rank_courses("2026 Fall", grade_lookup=grades,
                                         professor_lookup=lambda name, department: _rating(5 if name == "STRONG, B." else 1))
    assert set(queried) == {"WEAK, A.", "STRONG, B."}
    assert result["scored_candidates"] == 2
    assert len(result["courses"]) == 1
    best = result["courses"][0]
    assert best["section_code"] == "10002"
    assert best["instructor_name"] == "STRONG, B."
    assert best["ranking"]["features"]["grade_a_rate"]["scope"] == "course_instructor"
    assert best["ranking"]["final_score"] == 75


def test_enabled_search_does_not_treat_a_discussion_only_ta_as_a_ranked_professor(monkeypatch):
    _catalog(monkeypatch, sections_by_number={1: [
        {"code": "10003", "num": "A1", "type": "Dis", "instructors": ["ASSISTANT, T."]},
    ]})
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "true")
    monkeypatch.setattr(db, "get_student_profile", lambda user_id: {"found": False})

    def forbidden_ta(*args, **kwargs):
        pytest.fail("a discussion TA must not supply professor ranking features")

    monkeypatch.setattr(zotistics, "get_cached_query_result", forbidden_ta)
    monkeypatch.setattr(recommendations, "_local_rating", forbidden_ta)
    result = tools.dispatch("search_courses", {"term": "2026 Fall"}, context=_context())
    assert "error" not in result
    assert all(course.get("instructor_name") != "ASSISTANT, T." for course in result["courses"])


def test_co_taught_primary_preserves_names_without_inventing_a_joint_rating(monkeypatch):
    _catalog(monkeypatch, sections_by_number={1: [
        {"code": "10001", "instructors": ["FIRST, A.", "SECOND, B."]},
    ]})

    def forbidden_joint(*args, **kwargs):
        pytest.fail("co-teachers must not be silently averaged or selected")

    result = recommendations.rank_courses("2026 Fall", grade_lookup=forbidden_joint, professor_lookup=forbidden_joint)
    course = result["courses"][0]
    assert course["instructors"] == ["FIRST, A.", "SECOND, B."]
    assert course["instructor_name"] is None
    assert course["ranking"]["final_score"] is None
    assert course["ranking"]["features"]["professor_rating"]["raw"] is None


@pytest.mark.parametrize("term", ["2027 Winter", "invalid term"])
def test_wrong_term_or_absent_catalog_remains_unknown_and_does_not_query_features(monkeypatch, term):
    _catalog(monkeypatch)

    def forbidden_lookup(*args, **kwargs):
        pytest.fail("an absent target catalog cannot be replaced with another term")

    result = recommendations.rank_courses(term, grade_lookup=forbidden_lookup, professor_lookup=forbidden_lookup)
    assert result["found"] is False
    assert result["courses"] == []
    assert result.get("offering_status") != "not_offered"
    assert not result.get("authoritative", False)


def test_enabled_search_keeps_backend_term_allowlist_and_default_immutable(monkeypatch):
    _catalog(monkeypatch)
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "true")
    context = _context()
    result = tools.dispatch("search_courses", {"term": "2027 Winter"}, context=context)
    assert "outside the allowed query-term scope" in result["error"]
    assert context["default_term"] == "2026 Fall"
    assert context["allowed_query_terms"] == ["2026 Fall"]


def test_course_aggregate_a_rate_keeps_course_scope_in_a_lecturer_candidate(monkeypatch):
    _catalog(monkeypatch)
    result = recommendations.rank_courses("2026 Fall",
        grade_lookup=lambda **query: _history(query["course_id"], None),
        professor_lookup=lambda name, department: _rating())
    course = result["courses"][0]
    feature = course["ranking"]["features"]["grade_a_rate"]
    assert course["instructor_name"] == "LECTURER, A."
    assert feature["scope"] == "course"
    assert feature["instructor_name"] is None
    assert course["zotistics_grades"]["grades"]["instructor_name"] is None


def test_confirmed_empty_pair_fallback_preserves_requested_course_and_person_scope(monkeypatch):
    _catalog(monkeypatch)
    calls = []

    def grades(**query):
        calls.append(query)
        if query.get("course_id"):
            return {"found": False, "status": "no_data", "scope": "course_instructor",
                    "source": "zotistics", "resolved_instructor_name": "LECTURER, A."}
        return _history(None, query["instructor_name"])

    result = recommendations.rank_courses("2026 Fall", grade_lookup=grades,
                                         professor_lookup=lambda name, department: _rating())
    course = result["courses"][0]
    assert len(calls) == 2
    assert "course_id" not in calls[1]
    assert course["zotistics_grades"]["requested_course_id"] == "COMPSCI 1"
    assert course["zotistics_grades"]["fallback_from_scope"] == "course_instructor"
    feature = course["ranking"]["features"]["grade_a_rate"]
    assert feature["scope"] == "instructor"
    assert feature["requested_course_id"] == "COMPSCI 1"
    assert feature["fallback"] is True
    assert feature["uncertain_reason"] == "instructor_wide_fallback"


def test_fullname_pair_resolution_is_preserved_when_fallback_uses_the_source_canonical_name(monkeypatch):
    _catalog(monkeypatch, sections_by_number={1: [
        {"code": "10001", "instructors": ["Rainer Klefstad"]},
    ]})
    calls = []

    def grades(**query):
        calls.append(query)
        if query.get("course_id"):
            return {"found": False, "status": "no_data", "source": "zotistics",
                    "scope": "course_instructor", "resolved_instructor_name": "KLEFSTAD, R.",
                    "query": {"course_id": query["course_id"], "instructor_name": "RAINER KLEFSTAD"}}
        result = _history(None, "KLEFSTAD, R.")
        result.update({"query": {"course_id": None, "instructor_name": "KLEFSTAD, R."},
                       "resolved_instructor_name": "KLEFSTAD, R."})
        return result

    result = recommendations.rank_courses("2026 Fall", grade_lookup=grades,
                                         professor_lookup=lambda name, department: _rating())
    course = result["courses"][0]
    assert calls[1]["instructor_name"] == "KLEFSTAD, R."
    assert course["instructor_name"] == "Rainer Klefstad"
    assert course["zotistics_grades"]["query"]["instructor_name"] == "KLEFSTAD, R."
    feature = course["ranking"]["features"]["grade_a_rate"]
    assert feature["valid"] is True
    assert feature["scope"] == "instructor"
    assert feature["fallback"] is True
    assert feature["requested_course_id"] == "COMPSCI 1"


@pytest.mark.parametrize("status,scope", [("unavailable", "course_instructor"), ("no_data", "course")])
def test_unavailable_or_wrong_scope_empty_result_does_not_trigger_person_fallback(monkeypatch, status, scope):
    _catalog(monkeypatch)
    calls = []

    def grades(**query):
        calls.append(query)
        assert query.get("course_id"), "fallback requires confirmed absence for the exact professor-course pair"
        return {"found": False, "status": status, "scope": scope, "source": "zotistics"}

    result = recommendations.rank_courses("2026 Fall", grade_lookup=grades,
                                         professor_lookup=lambda name, department: _rating())
    assert len(calls) == 1
    assert result["courses"][0]["ranking"]["features"]["grade_a_rate"]["missing_reason"] == status


@pytest.mark.parametrize("prereq_status", ["not_met", "unknown"])
def test_enabled_card_ranking_preserves_existing_prereq_and_conflict_states(monkeypatch, prereq_status):
    from tests.test_agent_tools_schedule_validation import _install_schedule_validation_catalog

    _install_schedule_validation_catalog(monkeypatch)
    monkeypatch.setattr(db, "check_prerequisites_met", lambda *args, **kwargs: {
        "found": True, "status": prereq_status, "met": False,
        "missing": ["ICS33"] if prereq_status == "not_met" else [],
        "unknown": ["grade missing"] if prereq_status == "unknown" else [],
        "required": ["ICS33"],
    })
    snapshots = []
    for enabled in ("false", "true"):
        monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", enabled)
        context = _context()
        context["pending_schedule"] = [{"course_id": "IN4MATX43", "section": "A", "term": "2026 Fall"}]
        result = tools.dispatch("propose_recommendation", {
            "items": [{"course_id": "COMPSCI161", "category": "elective", "reason": "Fixture course."}],
            "term": "2026 Fall",
        }, context=context)
        assert result["ok"] is True
        assert result["staged_count"] == 1
        card = context["_proposed_cards"][0]
        assert card["prereq_status"] == prereq_status
        assert card["prereq_met"] is False
        assert {conflict["type"] for conflict in card["schedule_validation"]["conflicts"]} == {"time_conflict"}
        snapshots.append({key: card[key] for key in (
            "prereq_status", "prereq_met", "prereq_missing", "prereq_unknown", "prereq_required", "schedule_validation",
        )})
    # Fixed ranking still preserves prerequisite and concrete time conflicts.
    # The opted-in evidence contract additionally records unresolved pairing;
    # the old path's absence of that state must not erase the new uncertainty.
    assert {key: value for key, value in snapshots[0].items() if key != "schedule_validation"} == {
        key: value for key, value in snapshots[1].items() if key != "schedule_validation"}
    old_validation, new_validation = [snapshot["schedule_validation"] for snapshot in snapshots]
    assert old_validation["conflicts"] == new_validation["conflicts"]
    assert new_validation["valid"] is False
    assert any(issue["type"] == "pairing_unknown" for issue in new_validation["unknowns"])
    assert new_validation["pairing"][0]["pairing"]["requirement"] == "unknown"
