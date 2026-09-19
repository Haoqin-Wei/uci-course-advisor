from __future__ import annotations

from app.data import db
from app.data.prerequisites import evaluate_prerequisite_tree


def test_or_branch_accepts_math_3a_or_h3a():
    with_math_3a = db.check_prerequisites_met(
        "MATH 10",
        completed_courses=["MATH 2D", "MATH 3A", "MATH 9"],
        in_progress_courses=[],
    )
    with_h3a = db.check_prerequisites_met(
        "MATH 10",
        completed_courses=["MATH 2D", "MATH H3A", "MATH 9"],
        in_progress_courses=[],
    )

    assert with_math_3a["status"] == "met"
    assert with_math_3a["met"] is True
    assert with_h3a["status"] == "met"
    assert with_h3a["met"] is True


def test_minimum_grade_missing_is_unknown_not_met():
    result = db.check_prerequisites_met(
        "COMPSCI 122A",
        completed_courses=["ICS33"],
        in_progress_courses=[],
    )

    assert result["status"] == "unknown"
    assert result["met"] is False
    assert result["missing"] == ["EECS 114 (min D-)"]
    assert result["unknown"] == [
        "I&C SCI 33 (min C) completed, but recorded grade is missing for minimum C"
    ]


def test_minimum_grade_known_grade_can_pass_or_fail():
    passing = db.check_prerequisites_met(
        "COMPSCI 122A",
        completed_courses=[{"course_id": "ICS33", "grade": "C"}],
        in_progress_courses=[],
    )
    failing = db.check_prerequisites_met(
        "COMPSCI 122A",
        completed_courses=[{"course_id": "ICS33", "grade": "C-"}],
        in_progress_courses=[],
    )

    assert passing["status"] == "met"
    assert passing["met"] is True
    assert failing["status"] == "not_met"
    assert failing["met"] is False
    assert failing["missing"] == ["one of: I&C SCI 33 (min C) / EECS 114 (min D-)"]


def test_exam_branch_is_unknown_when_profile_cannot_verify_exam_score():
    result = db.check_prerequisites_met(
        "CHEM 1B",
        completed_courses=[],
        in_progress_courses=[],
    )

    assert result["status"] == "unknown"
    assert result["met"] is False
    assert "AP CHEMISTRY score 4+ cannot be verified from the student profile" in (
        result["unknown"]
    )


def test_prerequisite_text_without_tree_is_unknown():
    result = evaluate_prerequisite_tree(
        {},
        completed_courses=[],
        in_progress_courses=[],
        flat_prerequisites=[],
        prerequisite_text="I&C SCI 46",
    )

    assert result.status == "unknown"
    assert result.met is False
    assert result.unknown == [
        "prerequisite text exists but no structured prerequisite tree is available"
    ]
