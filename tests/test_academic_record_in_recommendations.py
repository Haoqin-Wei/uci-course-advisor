"""Exam credit, in-progress courses, renamed courses and confirmed GE reach the degree path."""

from __future__ import annotations

from app.academic import record, store
from app.academic.models import AcademicDetailsUpdate
from app.agent import prefilter
from app.catalog.course_equivalents import equivalents
from app.recommendations.filter_plan import empty_filter_plan
from app.recommendations.graduation_path import build_graduation_path, ge_missing
from app.recommendations.prefilter import apply_dynamic_prefilter
from tests.test_academic_transcript_import import _create_and_login, _payload

TERM = "2027 Winter"


def course(tree=None, ge=(), numeric=100):
    return {"title": "T", "units": 4.0, "numeric": numeric, "prerequisite_tree": tree or {},
            "terms_offered": ["2026 Fall", "2026 Winter", "2026 Spring"], "ge": list(ge)}


def test_record_merges_transcript_exam_credit_and_courses_in_progress(app_client):
    user = _create_and_login(app_client)
    payload = _payload(printed_at="2026-09-18T15:23:00Z")
    payload["exam_credits"] = [
        {"exam_type": "AP", "subject": "PSYCHOLOGY", "score": 5, "units": 4, "exam_date": "05/24"}]
    assert app_client.post("/api/academic/transcript/import", json=payload).status_code == 200
    store.update_academic_details(user["id"], AcademicDetailsUpdate(
        in_progress_term="2026 Fall", in_progress_courses=["SWE 43"], ge_confirmed=["VI"],
        transfer_courses=["WRITING 39B"]))

    merged = record.recommendation_record(user["id"], [{"course_id": "MATH 2A", "grade": "B"}])
    attempts = {attempt["course_id"]: attempt["grade"] for attempt in merged["completed_course_attempts"]}
    assert attempts == {"MATH 2A": "B", "PSY 9": "P", "PSCI 9": "P", "COGS 7A": "P", "WRITING 39B": "P"}
    assert merged["in_progress_courses"] == ["SWE 43", "IN4MATX 43"]
    assert set(merged["courses_done"]) == {"MATH 2A", "PSY 9", "PSCI 9", "COGS 7A", "WRITING 39B", "SWE 43", "IN4MATX 43"}
    assert merged["ge_basis"]["courses"] == ["MATH 2A", "SWE 43", "WRITING 39B"]
    assert merged["ge_basis"]["exam_credits"] == [{"ge_courses": ["PSY 9"], "ge_direct": {}}]
    assert merged["ge_basis"]["confirmed"] == ["VI"]
    assert (merged["exam_credits_total"], merged["exam_credits_unmatched"]) == (1, 0)


CATALOGUE = {
    "SOC1": course(ge=("III",)), "SOC2": course(ge=("III",)), "PSY9": course(ge=("III",)),
    "PSCI9": course(ge=("III",)), "COGS7A": course(ge=("III",)), "ARTHIS40A": course(ge=("IV", "VIII")),
    "IN4MATX43": course(), "SWE43": course(),
}


def missing(basis, done=()):
    return {entry["category"]: entry["need"] for entry in ge_missing(set(done), lambda c: CATALOGUE.get(c), basis)}


def test_an_exam_credit_is_one_ge_course_however_many_ids_it_credits():
    basis = {"courses": ["SOC 1", "SOC 2"], "exam_credits": [{"ge_courses": ["PSY 9"], "ge_direct": {}}], "confirmed": []}
    assert "III" not in missing(basis)                       # 2 courses + 1 exam = 3
    assert missing({"courses": ["SOC 1", "SOC 2"], "exam_credits": [], "confirmed": []})["III"] == 1
    # Counting every credited ID would have claimed five GE III courses.
    assert missing(None, done=["SOC1", "PSY9", "PSCI9", "COGS7A"]).get("III") is None


def test_chart_stated_categories_and_course_labels_are_the_same_credit():
    basis = {"courses": [], "confirmed": [],
             "exam_credits": [{"ge_courses": ["ART HIS 40A"], "ge_direct": {"IV": 1, "VIII": 1}}]}
    result = missing(basis)
    assert result["IV"] == 2 and "VIII" not in result        # one IV course earned, not two


def test_a_category_the_student_marked_complete_is_not_a_gap():
    result = missing({"courses": [], "exam_credits": [], "confirmed": ["VI", "VII"]})
    assert "VI" not in result and "VII" not in result and result["VIII"] == 1


def test_requirement_listed_under_an_old_id_is_met_or_offered_under_the_new_one():
    requirements = [{"label": "IN4MATX 43", "requirementType": "Course", "courseCount": 1, "courses": ["IN4MATX43"]}]
    rows = {"SWE43": {"course_id": "SWE 43", "ge_categories": [], "sections": [
        {"section_code": "1", "section_type": "Lec", "days": "MWF", "start_time": "10:00", "end_time": "10:50"}]}}
    args = dict(requirements=requirements, candidates=rows, excluded={}, term=TERM,
                course_info=lambda c: CATALOGUE.get(c), equivalents=equivalents)
    offered = build_graduation_path(completed=["MATH 2A"], **args)
    assert [item["course_id"] for item in offered["required_now"]] == ["SWE 43"]
    assert offered["not_offered_this_term"] == []
    taking = build_graduation_path(completed=["MATH 2A", "SWE 43", "IN4MATX 43"], in_progress=["SWE 43"], **args)
    assert taking["required_now"] == [] and taking["in_progress"] == ["SWE 43"]


def _row(profile, tree):
    context = {"_prefilter_state": {"profile": profile}}
    item = {"course_id": "I&C SCI 46", "prerequisite_tree": tree, "prerequisites": []}
    return prefilter._metadata_candidate(item, [{"section_code": "1", "section_type": "Lec"}], context, TERM)


def test_course_in_progress_meets_a_prerequisite_and_a_minimum_grade_waits_for_the_grade():
    profile = {"completed_course_attempts": [{"course_id": "I&C SCI 33", "grade": "B"}],
               "in_progress_courses": ["I&C SCI 45C"]}
    plain = {"AND": [{"prereqType": "course", "coreq": False, "courseId": "I&C SCI 45C"}]}
    graded = {"AND": [{"prereqType": "course", "coreq": False, "courseId": "I&C SCI 45C", "minGrade": "C"}]}
    assert _row(profile, plain)["prerequisite_status"] == "met"
    waiting = _row(profile, graded)
    assert waiting["prerequisite_status"] == "unknown"
    kept = apply_dynamic_prefilter([waiting], empty_filter_plan(), {})
    assert len(kept["kept"]) == 1 and kept["excluded"] == []
    # A schedule draft is a plan, not enrollment.
    draft = {"completed_course_attempts": profile["completed_course_attempts"], "selected_courses": ["I&C SCI 45C"]}
    assert _row(draft, plain)["prerequisite_status"] == "not_met"


def test_exam_credit_satisfies_a_prerequisite_with_a_minimum_grade():
    profile = {"completed_course_attempts": [{"course_id": "MATH 2A", "grade": "P"}]}
    tree = {"AND": [{"prereqType": "course", "coreq": False, "courseId": "MATH 2A", "minGrade": "C"}]}
    assert _row(profile, tree)["prerequisite_status"] == "met"


def test_progress_endpoint_needs_a_login_and_reports_when_there_is_nothing_to_compute(app_client):
    assert app_client.get("/api/academic/progress?term=2027 Winter").status_code == 401
    _create_and_login(app_client)
    body = app_client.get("/api/academic/progress?term=2027 Winter").json()
    assert body["ok"] is True and body["available"] is False and body["term"] == "2027 Winter"
    assert app_client.get("/api/academic/progress?term=nonsense").status_code == 422
