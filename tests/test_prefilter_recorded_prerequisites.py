"""Recorded courses are what the student has taken.

The prefilter only excluded on prerequisites when two profile flags said the
record was complete and verified, and nothing ever set them: every course
stayed "needs verification" and the pool was never narrowed.
"""

from __future__ import annotations

from app.agent import prefilter
from app.recommendations.prefilter import apply_dynamic_prefilter
from app.recommendations.filter_plan import empty_filter_plan

TERM = "2027 Winter"
TREE = {"AND": [{"prereqType": "course", "coreq": False, "courseId": "I&C SCI 45C", "minGrade": "C"}]}


def _candidate(profile, tree=TREE, course_id="I&C SCI 46"):
    context = {"_prefilter_state": {"profile": profile}}
    course = {"course_id": course_id, "prerequisite_tree": tree, "prerequisites": []}
    return prefilter._metadata_candidate(course, [{"section_code": "1", "section_type": "Lec"}], context, TERM)


def test_missing_prerequisite_excludes_when_the_student_has_a_record():
    row = _candidate({"completed_course_attempts": [{"course_id": "I&C SCI 33", "grade": "B"}]})
    assert row["prerequisite_status"] == "not_met" and row["prerequisite_authoritative"] is True
    result = apply_dynamic_prefilter([row], empty_filter_plan(), {})
    assert result["kept"] == []
    assert result["excluded"][0]["reasons"] == ["prerequisite_not_met"]
    assert result["excluded"][0]["missing_prerequisites"] == ["I&C SCI 45C (min C)"]


def test_recorded_prerequisite_keeps_the_course():
    row = _candidate({"completed_course_attempts": [{"course_id": "I&C SCI 45C", "grade": "B"}]})
    assert row["prerequisite_status"] == "met"
    result = apply_dynamic_prefilter([row], empty_filter_plan(), {})
    assert [kept["course_id"] for kept in result["kept"]] == ["I&C SCI 46"]


def test_grade_below_the_minimum_excludes():
    row = _candidate({"completed_course_attempts": [{"course_id": "I&C SCI 45C", "grade": "D"}]})
    assert apply_dynamic_prefilter([row], empty_filter_plan(), {})["excluded"][0]["reasons"] == [
        "prerequisite_not_met"]


def test_no_record_at_all_excludes_nothing():
    row = _candidate({})
    assert row["prerequisite_status"] == "not_met" and row["prerequisite_authoritative"] is False
    result = apply_dynamic_prefilter([row], empty_filter_plan(), {})
    assert len(result["kept"]) == 1 and result["excluded"] == []
    assert "prerequisite_status_unknown" in result["kept"][0]["prefilter"]["reasons"]


def test_exam_prerequisite_stays_unknown_and_is_kept():
    tree = {"OR": [{"prereqType": "exam", "examName": "AP CALCULUS AB", "minGrade": "4"},
                   {"prereqType": "course", "coreq": False, "courseId": "MATH 1B"}]}
    row = _candidate({"completed_course_attempts": [{"course_id": "I&C SCI 33", "grade": "B"}]},
                     tree=tree, course_id="MATH 2A")
    assert row["prerequisite_status"] == "unknown"
    assert len(apply_dynamic_prefilter([row], empty_filter_plan(), {})["kept"]) == 1


def _state_with_exclusions(count):
    evidence = [{"section_code": str(index), "comments": {"school": {"comment": "x" * 4000}}} for index in range(5)]
    excluded = {(TERM, f"DEPT {index}"): {
        "id": f"DEPT {index}", "term": TERM, "reasons": ["prerequisite_not_met"],
        "missing_prerequisites": ["DEPT 0 (min C)"], "excluded_sections": [],
        "excluded_section_evidence": evidence} for index in range(count)}
    return {"enabled": True, "plan": empty_filter_plan(), "plan_status": "validated", "issues": [],
            "candidates": {}, "excluded": excluded, "detail_query_attempts": 0, "parse_metrics": {}}


def test_tool_results_carry_exclusion_reasons_not_every_excluded_section(monkeypatch):
    # 537 prerequisite exclusions once put 2.3M tokens into one tool result.
    import json

    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    context = {"_prefilter_state": _state_with_exclusions(500)}
    full = prefilter.summary(context)
    slim = prefilter.model_summary(context)
    assert len(json.dumps(full)) > 5_000_000
    assert len(json.dumps(slim)) < 10_000
    assert slim["excluded_count"] == 500 and slim["exclusion_reason_counts"] == {"prerequisite_not_met": 500}
    assert len(slim["excluded"]) == prefilter.MODEL_LISTED_EXCLUSIONS and slim["excluded_not_listed"] == 460
    assert slim["excluded"][0] == {"id": "DEPT 0", "term": TERM, "reasons": ["prerequisite_not_met"],
                                   "missing_prerequisites": ["DEPT 0 (min C)"]}
    assert len(full["excluded"]) == 500                     # the audit copy is untouched


def test_search_result_for_the_model_is_reduced_the_same_way():
    state = _state_with_exclusions(60)
    pool = {"found": True, "courses": [], "prefilter": {
        "enabled": True, "counts": {"excluded": 60}, "excluded": list(state["excluded"].values()),
        "needs_verification": [{"id": f"C {i}", "reasons": ["secondary_pairing_unknown"]} for i in range(90)]}}
    view = prefilter.model_pool_view(pool)
    assert view["prefilter"]["excluded_count"] == 60 and len(view["prefilter"]["excluded"]) == 40
    assert "excluded_section_evidence" not in view["prefilter"]["excluded"][0]
    assert view["prefilter"]["needs_verification_count"] == 90 and len(view["prefilter"]["needs_verification"]) == 40
    assert len(pool["prefilter"]["excluded"]) == 60 and "excluded_section_evidence" in pool["prefilter"]["excluded"][0]


def test_a_course_being_taken_now_is_never_a_candidate():
    profile = {"completed_course_attempts": [{"course_id": "I&C SCI 33", "grade": "A"}],
               "in_progress_courses": ["I&C SCI 45C", "SWE 43", "IN4MATX 43"]}
    context = {"_prefilter_state": {"profile": profile}}

    def filtered(course_id):
        item = {"course_id": course_id, "prerequisite_tree": {}, "prerequisites": []}
        row = prefilter._metadata_candidate(item, [{"section_code": "1", "section_type": "Lec"}], context, "2027 Winter")
        return apply_dynamic_prefilter([row], empty_filter_plan(), {})

    taking = filtered("ICS 45C")
    assert taking["kept"] == [] and taking["excluded"][0]["reasons"] == ["in_progress_now"]
    assert filtered("IN4MATX 43")["excluded"][0]["reasons"] == ["in_progress_now"]     # the old name of SWE 43
    assert len(filtered("I&C SCI 46")["kept"]) == 1
