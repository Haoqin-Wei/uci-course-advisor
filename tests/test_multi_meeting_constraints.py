"""All meetings are evidence, including hidden conflicts and unknown blocks."""
from copy import deepcopy

import pytest

from app.recommendations.filter_plan import empty_filter_plan
from app.recommendations.prefilter import apply_dynamic_prefilter
from app.scheduling.service import (
    find_conflicts,
    normalize_section_meeting,
    section_cancellation_status,
    section_meetings,
    section_time_status,
    validate_schedule_bundle,
)


def meeting(days="M", start="11:00", end="12:00", **extra):
    return {"days": days, "start_time": start, "end_time": end, **extra}


def section(**extra):
    return {"course_id": "ICS 33", "section_code": "10001", "section_num": "A",
            "section_type": "Lec", "term": "2026 Fall", "status": "OPEN",
            "final_exam": {"examStatus": "NO_FINAL"}, **meeting(), **extra}


def filtered(item, *, earliest=None, days=()):
    plan = empty_filter_plan()
    plan["hard_filters"].update(earliest_start=earliest,
                                earliest_start_is_hard=earliest is not None,
                                excluded_weekdays=list(days))
    course = {"course_id": "ICS 33", "term": "2026 Fall", "sections": [item],
              "requires_secondary": False, "prerequisite_status": "met"}
    return apply_dynamic_prefilter([course], plan)


def test_raw_api_meetings_normalize_every_block_without_mutating_source():
    rows = [{"days": "MW", "startTime": {"hour": 11, "minute": 0},
             "endTime": {"hour": 12, "minute": 20}, "bldg": ["DBH 1100"],
             "timeIsTBA": False, "source_extra": "keep me"},
            {"days": "F", "startTime": {"hour": 8, "minute": 0},
             "endTime": {"hour": 8, "minute": 50}, "bldg": ["SSL 100"]}]
    original = section(meetings=rows, section_comment="Full source comment")
    before = deepcopy(original)

    result = section_meetings(original)
    normalized = normalize_section_meeting(original)

    assert [(row["days"], row["start_time"], row["end_time"], row["location"])
            for row in result] == [("MW", "11:00", "12:20", "DBH 1100"),
                                   ("F", "08:00", "08:50", "SSL 100")]
    assert result[0]["source_extra"] == "keep me"
    assert normalized["days"] == "MW"
    assert normalized["end_time"] == "12:20"
    assert normalized["meetings"] == rows
    assert normalized["section_comment"] == "Full source comment"
    assert original == before


def test_hidden_second_meeting_conflict_is_detected_in_both_directions():
    candidate = section(meetings=[meeting("M"), meeting("F", "13:00", "14:00")])
    existing = section(days="F", start_time="13:30", end_time="14:30")
    assert section_time_status(candidate, existing) == "conflict"
    assert section_time_status(existing, candidate) == "conflict"


def test_conflict_summary_preserves_the_other_sections_full_meeting_windows():
    existing = section(course_id="ICS 32", section_code="20001",
                       meetings=[meeting("M"), meeting("F", "13:00", "14:00")])
    candidate = section(days="F", start_time="13:30", end_time="14:30")
    result = find_conflicts([candidate], [existing])
    assert result["10001"][0]["window"] == "M 11:00–12:00 / F 13:00–14:00"


def test_proven_conflict_takes_precedence_over_another_tba_meeting():
    candidate = section(meetings=[{"timeIsTBA": True}, meeting("F", "13:00", "14:00")])
    existing = section(days="F", start_time="13:30", end_time="14:30")
    assert section_time_status(candidate, existing) == "conflict"


def test_nonconflicting_known_meeting_does_not_hide_unknown_block():
    candidate = section(meetings=[meeting("M"), {"timeIsTBA": True}])
    assert section_time_status(candidate, section(days="Tu")) == "unknown"
    result = validate_schedule_bundle([{"course_id": "ICS 33", "selected_sections": [candidate]}])
    assert any(row["type"] == "time_unknown" for row in result["unknowns"])


@pytest.mark.parametrize("raw", [[], {}, "not an array", [None], [{}], [{"timeIsTBA": True}]])
def test_explicit_empty_or_malformed_meetings_do_not_reuse_flat_times(raw):
    candidate = section(meetings=raw)
    result = section_meetings(candidate)
    assert result and result[0]["time_is_tba"] is True
    assert section_time_status(candidate, section(days="Tu")) == "unknown"
    assert normalize_section_meeting(candidate)["start_time"] is None
    kept = filtered(candidate, earliest="10:00", days=("Fri",))
    assert len(kept["kept"]) == 1
    assert "time_unknown" in kept["needs_verification"][0]["reasons"]


@pytest.mark.parametrize("clock", [{"hour": 8}, {"minute": 0},
                                   {"hour": True, "minute": 0},
                                   {"hour": 8, "minute": 60},
                                   {"hour": 24, "minute": 0}])
def test_malformed_api_clocks_never_create_a_fabricated_interval(clock):
    candidate = section(meetings=[{"days": "F", "startTime": clock,
                                   "endTime": {"hour": 10, "minute": 0}}])
    result = section_meetings(candidate)[0]
    assert result["start_time"] is None
    assert result["end_time"] is None
    assert result["time_is_tba"] is True
    assert section_time_status(candidate, section(days="F")) == "unknown"


@pytest.mark.parametrize("extra", [{}, {"meetings": None}])
def test_legacy_flat_times_and_registrar_meridiem_remain_supported(extra):
    candidate = section(start_time="12:30", end_time="1:50p", **extra)
    result = section_meetings(candidate)[0]
    assert (result["start_time"], result["end_time"], result["time_is_tba"]) == ("12:30", "13:50", False)
    assert section_time_status(candidate, section(start_time="13:00", end_time="14:00")) == "conflict"


def test_different_terms_remain_separate_even_with_unknown_meetings():
    assert section_time_status(section(meetings=[]), section(term="2027 Winter")) == "clear"


@pytest.mark.parametrize("rows, kwargs, reason", [
    ([meeting("M"), meeting("F")], {"days": ("Fri",)}, "excluded_weekday"),
    ([meeting("M"), meeting("Tu", "08:00", "09:00")], {"earliest": "10:00"}, "earliest_start_violation"),
    ([{"timeIsTBA": True}, meeting("F")], {"days": ("Fri",)}, "excluded_weekday"),
    ([{"timeIsTBA": True}, meeting("Tu", "08:00", "09:00")], {"earliest": "10:00"}, "earliest_start_violation"),
])
def test_every_meeting_is_checked_for_confirmed_hard_constraint_violations(rows, kwargs, reason):
    result = filtered(section(meetings=rows), **kwargs)
    assert result["kept"] == []
    assert reason in result["excluded"][0]["excluded_sections"][0]["reasons"]


def test_a_tba_second_meeting_remains_needs_verification_not_silently_clear():
    result = filtered(section(meetings=[meeting("M"), {"timeIsTBA": True}]), earliest="10:00")
    assert len(result["kept"]) == 1
    assert result["kept"][0]["prefilter"]["status"] == "needs_verification"
    assert "time_unknown" in result["needs_verification"][0]["reasons"]


def test_tba_with_known_nonexcluded_day_still_reports_incomplete_time_evidence():
    result = filtered(section(meetings=[meeting("M", time_is_tba=True)]), days=("Fri",))
    assert len(result["kept"]) == 1
    assert "time_unknown" in result["needs_verification"][0]["reasons"]


@pytest.mark.parametrize("extra", [{"is_cancelled": None}, {"isCancelled": None}])
def test_explicit_unknown_cancellation_is_reported_without_excluding(extra):
    candidate = section(**extra)
    assert section_cancellation_status(candidate) == "unknown"
    result = filtered(candidate)
    assert len(result["kept"]) == 1
    assert "section_status_unknown" in result["needs_verification"][0]["reasons"]
    bundle = validate_schedule_bundle([{"course_id": "ICS 33", "selected_sections": [candidate]}])
    assert any(row["type"] == "section_status_unknown" for row in bundle["warnings"])


@pytest.mark.parametrize("extra", [{}, {"is_cancelled": False}, {"is_cancelled": "false"}])
def test_known_not_cancelled_and_legacy_absent_field_do_not_gain_unknown_warnings(extra):
    candidate = section(**extra)
    assert section_cancellation_status(candidate) == "not_cancelled"
    assert filtered(candidate)["needs_verification"] == []


@pytest.mark.parametrize("extra", [{"is_cancelled": True}, {"isCancelled": True}, {"status": "CANCELLED"}])
def test_confirmed_cancellation_is_excluded(extra):
    candidate = section(**extra)
    assert section_cancellation_status(candidate) == "cancelled"
    result = filtered(candidate)
    assert result["kept"] == []
    assert "section_cancelled" in result["excluded"][0]["excluded_sections"][0]["reasons"]
    bundle = validate_schedule_bundle([{"course_id": "ICS 33", "selected_sections": [candidate]}])
    assert any(row["type"] == "section_unavailable" for row in bundle["conflicts"])
