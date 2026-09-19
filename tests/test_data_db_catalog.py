from __future__ import annotations

import pytest

from app.data import db


def test_get_course_info_prefers_local_courses_csv(monkeypatch: pytest.MonkeyPatch):
    def fail_fetch_course(_course_key: str):
        raise AssertionError("Anteater should not be called for local course metadata")

    monkeypatch.setattr(db.anteater, "fetch_course", fail_fetch_course)

    result = db.get_course_info("COMPSCI 122A")

    assert result["found"] is True
    assert result["source"] == "db"
    course = result["course"]
    assert course["course_id"] == "COMPSCI 122A"
    assert course["title"] == "Introduction to Data Management"
    assert course["units"] == 4
    assert course["level"] == "Upper Division (100-199)"
    assert course["restriction"]
    assert course["prerequisite_text"] == (
        "I&C SCI 33 with a minimum grade of C or EECS 114"
    )
    assert course["prerequisite_tree"]["AND"][0]["OR"][0]["courseId"] == "I&C SCI 33"
    assert course["prerequisites"] == ["I&C SCI 33", "EECS 114"]
    assert course["provenance"]["loader"] == "uci_relational"


def test_batch_get_course_info_reuses_common_course_payload(
    monkeypatch: pytest.MonkeyPatch,
):
    def fail_fetch_course(_course_key: str):
        raise AssertionError("Anteater should not be called for local course metadata")

    monkeypatch.setattr(db.anteater, "fetch_course", fail_fetch_course)

    result = db.batch_get_course_info([
        "COMPSCI 122A",
        "CS122A",
        "not-a-course",
    ])

    assert result["found"] is True
    assert result["total_found"] == 1
    assert [course["course_id"] for course in result["courses"]] == ["COMPSCI 122A"]
    assert result["missing"] == [
        {
            "course_id": "not-a-course",
            "reason": "could not parse course id 'not-a-course'",
        }
    ]


def test_search_courses_order_is_stable():
    first = db.search_courses("Spring 2025")
    second = db.search_courses("Spring 2025")

    assert first["found"] is True
    assert second["found"] is True
    assert first["courses"] == second["courses"]
    assert first["courses"]
    assert all(course["course_id"] for course in first["courses"])
