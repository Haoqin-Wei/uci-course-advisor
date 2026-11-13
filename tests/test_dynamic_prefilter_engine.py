from __future__ import annotations

from copy import deepcopy

import pytest

from app.recommendations.filter_plan import empty_filter_plan
from app.recommendations.prefilter import apply_dynamic_prefilter


def _section(code="10001", *, number="A", kind="Lec", start="11:00", end="12:20", days="MW", **extra):
    return {"section_code": code, "section_num": number, "section_type": kind,
            "start_time": start, "end_time": end, "days": days, "term": "2026 Fall",
            "instructors": ["LECTURER, A."], **extra}


def _course(course_id="ECON 20A", *, labels=("GE-III",), sections=None, **extra):
    return {"course_id": course_id, "term": "2026 Fall", "ge_categories": list(labels),
            "ge_labels_verified": True, "prerequisite_status": "met", "offering_status": "offered",
            "requires_secondary": False, "sections": [_section()] if sections is None else sections, **extra}


def _audit(unmet=("II",), projected=()):
    return {"ge_audit": {"verified": True, "authorized": True, "source": "registrar_degree_audit",
                         "unmet_categories": list(unmet), "projected_categories": list(projected)}}


def _plan(*, only_unmet=False, earliest=None, hard_time=False, days=(), goal="balanced", **soft):
    plan = empty_filter_plan()
    plan["goal"] = goal
    plan["hard_filters"].update(only_unmet_ge=only_unmet, earliest_start=earliest,
                                earliest_start_is_hard=hard_time, excluded_weekdays=list(days))
    plan["soft_preferences"].update(soft)
    return plan


def _ids(result):
    return [course["course_id"] for course in result["kept"]]


def _reasons(result):
    return {item["id"]: item["reasons"] for item in result["needs_verification"]}


def test_case_01_finished_ge_does_not_remove_a_different_gpa_friendly_course():
    profile = {**_audit(unmet=("II",)), "completed_courses": ["ECON13"]}
    result = apply_dynamic_prefilter([_course("ECON20A")], _plan(goal="gpa_optimization", high_a_rate=True), profile)

    assert _ids(result) == ["ECON20A"]
    assert result["excluded"] == []
    assert "repeat_eligibility_unknown" not in _reasons(result).get("ECON20A", [])


def test_case_02_explicit_unmet_ge_uses_all_labels_and_reliable_audit():
    courses = [_course("SOC SCI 1", labels=("GE-II", "GE-III")),
               _course("ART 1", labels=("GE-IV",)), _course("MATH2A", labels=())]
    result = apply_dynamic_prefilter(courses, _plan(only_unmet=True), _audit(unmet=("III",)))

    assert _ids(result) == ["SOC SCI 1"]
    assert {item["id"] for item in result["excluded"]} == {"ART 1", "MATH2A"}
    assert all(item["reasons"] == ["no_unmet_ge_contribution"] for item in result["excluded"])
    assert result["kept"][0]["prefilter"]["ge_contribution"]["remaining_categories"] == ["III"]


def test_case_03_graduation_and_gpa_goal_does_not_activate_ge_exclusivity():
    plan = _plan(goal="graduation_progress", high_a_rate=True, professor_rating="high")
    plan["secondary_goals"] = ["gpa_optimization"]

    result = apply_dynamic_prefilter([_course()], plan, _audit())

    assert len(result["kept"]) == 1
    assert result["applied_plan"]["hard_filters"]["only_unmet_ge"] is False


def test_case_04_avoiding_early_classes_softly_never_deletes_an_early_section():
    result = apply_dynamic_prefilter([_course(sections=[_section(start="08:00", end="09:20")])],
                                    _plan(earliest="10:00", avoid_early_classes=True))

    assert len(result["kept"]) == 1
    assert result["kept"][0]["sections"][0]["start_time"] == "08:00"
    assert result["excluded"] == []


def test_case_05_hard_time_removes_sections_and_retains_a_compatible_later_bundle():
    sections = [_section("101", number="A", start="08:00", end="09:20"),
                _section("102", number="A1", kind="Dis", start="11:00", end="11:50"),
                _section("103", number="B", start="10:00", end="11:20"),
                _section("104", number="B1", kind="Dis", start="13:00", end="13:50")]
    candidate = _course(sections=sections, requires_secondary=True)

    result = apply_dynamic_prefilter([candidate], _plan(earliest="10:00", hard_time=True))

    assert [section["section_code"] for section in result["kept"][0]["sections"]] == ["103", "104"]
    removed = result["kept"][0]["prefilter"]["excluded_sections"]
    assert {row["section_code"] for row in removed} == {"101", "102"}
    assert result["counts"]["input_sections"] == 4
    assert result["counts"]["kept_sections"] == 2


@pytest.mark.parametrize("profile", [None, {}, {"completed_ge": ["III"]},
    {"ge_audit": {"verified": True, "authorized": True, "source": "frozen_ge_policy", "unmet_categories": []}},
    {"ge_audit": {"verified": True, "authorized": False, "source": "registrar_degree_audit", "unmet_categories": []}},
])
def test_case_06_absent_or_unreliable_ge_records_are_retained_with_verification(profile):
    result = apply_dynamic_prefilter([_course()], _plan(only_unmet=True), profile)

    assert len(result["kept"]) == 1
    assert _reasons(result)["ECON 20A"] == ["ge_audit_unverified"]


def test_case_07_confirmed_not_offered_is_distinct_from_unavailable_or_partial_catalog():
    courses = [_course("ECON1", sections=[], offering_status="not_offered", authoritative=True),
               _course("ECON2", sections=[], offering_status="not_offered", authoritative=False),
               _course("ECON3", sections=[], offering_status="unavailable")]

    result = apply_dynamic_prefilter(courses, _plan())

    assert _ids(result) == ["ECON2", "ECON3"]
    assert result["excluded"][0]["reasons"] == ["not_offered_this_term"]
    assert "offering_status_unknown" in _reasons(result)["ECON2"]
    assert "section_schedule_unknown" in _reasons(result)["ECON3"]


@pytest.mark.parametrize("status,reliable", [("unknown", False), ("not_met", False)])
def test_case_08_unknown_or_incomplete_prerequisite_evidence_is_not_excluded(status, reliable):
    course = _course(prerequisite_status=status, prerequisite_authoritative=reliable)

    result = apply_dynamic_prefilter([course], _plan())

    assert len(result["kept"]) == 1
    assert "prerequisite_status_unknown" in _reasons(result)["ECON 20A"]


def test_authoritative_prerequisite_and_eligibility_negatives_are_separate_from_unknown():
    courses = [_course("ICS33", prerequisite_status="not_met", prerequisite_authoritative=True),
               _course("ICS32", eligibility_status="not_eligible", eligibility_authoritative=True),
               _course("ICS31", eligibility_status="not_eligible", eligibility_authoritative=False)]

    result = apply_dynamic_prefilter(courses, _plan())

    assert _ids(result) == ["ICS31"]
    assert {item["id"]: item["reasons"] for item in result["excluded"]} == {
        "ICS33": ["prerequisite_not_met"], "ICS32": ["not_eligible"],
    }


def test_case_09_same_course_repeat_is_warned_without_assuming_grade_or_credit_eligibility():
    profile = {"completed_course_attempts": [{"course_id": "CS161", "grade": "C-"}]}

    result = apply_dynamic_prefilter([_course("COMPSCI161"), _course("COMPSCI162")], _plan(goal="gpa_optimization"), profile)

    assert _ids(result) == ["COMPSCI161", "COMPSCI162"]
    assert _reasons(result)["COMPSCI161"] == ["repeat_eligibility_unknown"]
    assert "COMPSCI162" not in _reasons(result)


def test_case_10_same_inputs_are_reproducible_and_all_input_objects_are_unchanged():
    inputs = ([_course()], _plan(only_unmet=True), _audit(unmet=("III",)))
    original = deepcopy(inputs)

    first = apply_dynamic_prefilter(*inputs)
    second = apply_dynamic_prefilter(*inputs)

    assert first == second
    assert inputs == original
    first["kept"][0]["sections"][0]["instructors"].append("OTHER, B.")
    assert inputs == original


def test_case_11_pure_engine_does_not_read_feature_flags_or_change_the_conservative_plan(monkeypatch):
    # The root integration tests exercise the actual flag-off legacy path.
    # This engine is deliberately flag-agnostic and has no I/O dependency.
    candidates, plan = [_course()], _plan()
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "false")
    disabled = apply_dynamic_prefilter(candidates, plan)
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")

    assert apply_dynamic_prefilter(candidates, plan) == disabled
    assert disabled["applied_plan"] == plan


def test_case_12_no_first_30_cutoff_can_hide_the_later_valid_candidate():
    courses = [_course(f"COMPSCI{number}", sections=[_section(str(number), start="08:00", end="09:20")]) for number in range(1, 35)]
    courses.append(_course("COMPSCI35", sections=[_section("35", start="11:00", end="12:20")]))

    result = apply_dynamic_prefilter(courses, _plan(earliest="10:00", hard_time=True))

    assert _ids(result) == ["COMPSCI35"]
    assert result["counts"]["input_candidates"] == 35
    assert result["counts"]["excluded_candidates"] == 34


@pytest.mark.parametrize("days,start,end", [("TBA", "TBA", "TBA"), ("Tu", "bad", "12:20"), ("TBA", "11:00", "12:20"), ("Monday", "11:00", "12:20")])
def test_tba_invalid_and_partial_weekday_labels_remain_unknown(days, start, end):
    result = apply_dynamic_prefilter([_course(sections=[_section(days=days, start=start, end=end)])],
                                    _plan(earliest="10:00", hard_time=True, days=("Mon",)))

    assert len(result["kept"]) == 1
    assert "time_unknown" in _reasons(result)["ECON 20A"]


@pytest.mark.parametrize("start,end,limit,keep", [("12:30", "1:50p", "10:00", True), ("3:30", "4:50p", "14:00", True),
                                                 ("11:00", "12:20p", "12:00", False), ("10:00", "11:20", "10:00", True)])
def test_registrar_shared_meridiem_and_hard_time_boundary_use_shared_parser(start, end, limit, keep):
    result = apply_dynamic_prefilter([_course(sections=[_section(start=start, end=end)])], _plan(earliest=limit, hard_time=True))

    assert bool(result["kept"]) is keep


def test_partial_discussion_removal_cannot_leave_a_known_unpairable_lecture():
    sections = [_section("1", number="A"), _section("2", number="A1", kind="Dis", days="F"),
                _section("3", number="B"), _section("4", number="B1", kind="Dis", days="Tu")]

    result = apply_dynamic_prefilter([_course(sections=sections, requires_secondary=True)], _plan(days=("Fri",)))

    assert [section["section_code"] for section in result["kept"][0]["sections"]] == ["3", "4"]


def test_unknown_pairing_letters_do_not_hard_delete_a_potential_bundle():
    sections = [_section("1", number=""), _section("2", number="", kind="Dis", days="F")]

    result = apply_dynamic_prefilter([_course(sections=sections, requires_secondary=True)], _plan(days=("Fri",)))

    assert [section["section_code"] for section in result["kept"][0]["sections"]] == ["1"]
    assert "secondary_pairing_unknown" in _reasons(result)["ECON 20A"]


def test_full_snapshot_does_not_prove_a_course_is_unavailable():
    result = apply_dynamic_prefilter([_course(sections=[_section(status="FULL", seats_open=0)])], _plan())

    assert len(result["kept"]) == 1
    assert result["excluded"] == []


def test_ge_tags_without_verified_label_completeness_are_not_hard_excluded():
    result = apply_dynamic_prefilter([_course(ge_labels_verified=False)], _plan(only_unmet=True), _audit(unmet=("II",)))

    assert len(result["kept"]) == 1
    assert _reasons(result)["ECON 20A"] == ["ge_labels_unknown"]


def test_projected_coverage_is_deducted_but_kept_separate_from_actual_unmet():
    result = apply_dynamic_prefilter([_course(labels=("GE-III", "GE-IV"))], _plan(only_unmet=True),
                                    _audit(unmet=("III", "IV"), projected=("III",)))

    contribution = result["kept"][0]["prefilter"]["ge_contribution"]
    assert contribution == {"unmet_categories": ["III", "IV"], "projected_categories": ["III"], "remaining_categories": ["IV"]}


def test_historical_proxy_schedule_cannot_exclude_the_future_target_course():
    sections = [_section(start="08:00", end="09:20", term="2025 Fall")]

    result = apply_dynamic_prefilter([_course(sections=sections)], _plan(earliest="10:00", hard_time=True))

    assert len(result["kept"]) == 1
    assert "time_unknown" in _reasons(result)["ECON 20A"]


def test_no_lecture_left_is_not_rescued_by_an_optional_discussion():
    sections = [_section("1", start="08:00", end="09:20"), _section("2", number="A1", kind="Dis")]

    result = apply_dynamic_prefilter([_course(sections=sections, requires_secondary=False)], _plan(earliest="10:00", hard_time=True))

    assert result["kept"] == []
    assert result["excluded"][0]["reasons"] == ["no_compatible_section_bundle"]
    assert result["counts"]["excluded_sections"] == 2


def test_historical_absence_evidence_does_not_prove_current_absence():
    course = _course(offering_status="not_offered", authoritative=True, source_term="2025 Fall")

    result = apply_dynamic_prefilter([course], _plan())

    assert len(result["kept"]) == 1
    assert "offering_status_unknown" in _reasons(result)["ECON 20A"]


def test_conflicting_prerequisite_evidence_cannot_authorize_exclusion():
    course = _course(prerequisite_status="not_met", prerequisite_authoritative=True,
                     prerequisite_result={"status": "unknown", "source": "catalog"})

    result = apply_dynamic_prefilter([course], _plan())

    assert len(result["kept"]) == 1
    assert "prerequisite_status_unknown" in _reasons(result)["ECON 20A"]


def test_projected_only_exclusion_preserves_actual_and_projected_categories():
    result = apply_dynamic_prefilter([_course()], _plan(only_unmet=True), _audit(unmet=("III",), projected=("III",)))

    assert result["kept"] == []
    assert result["excluded"][0]["ge_contribution"] == {
        "unmet_categories": ["III"], "projected_categories": ["III"], "remaining_categories": [],
    }


def test_excluded_course_sections_are_included_in_counts():
    courses = [_course("ECON1"), _course("ECON2", prerequisite_status="not_met", prerequisite_authoritative=True)]

    result = apply_dynamic_prefilter(courses, _plan())

    assert result["counts"]["input_sections"] == 2
    assert result["counts"]["kept_sections"] == 1
    assert result["counts"]["excluded_sections"] == 1


def test_course_alias_comparison_has_no_lazy_catalog_io(monkeypatch):
    from app.catalog import departments
    def forbidden():
        raise AssertionError("pure prefilter must not load a catalog to compare IDs")
    monkeypatch.setattr(departments, "_catalog_department_lookup", forbidden)

    result = apply_dynamic_prefilter([_course("KOREAN 1A")], _plan(), {"completed_courses": ["KOREAN1A"]})

    assert "repeat_eligibility_unknown" in _reasons(result)["KOREAN 1A"]
