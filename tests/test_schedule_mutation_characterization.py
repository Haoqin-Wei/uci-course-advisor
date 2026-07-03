from __future__ import annotations

import pytest

from app.modules import state as state_module


@pytest.fixture
def fake_schedule_catalog(monkeypatch):
    from app.data import db

    sections_by_course = {
        "COMPSCI161": [
            {
                "section_code": "20000",
                "section_num": "A",
                "section_type": "Lec",
                "days": "TuTh",
                "start_time": "10:00",
                "end_time": "11:20",
                "location": "DBH 1100",
                "instructors": ["TESTER, A."],
            },
            {
                "section_code": "20001",
                "section_num": "A1",
                "section_type": "Dis",
                "days": "F",
                "start_time": "09:00",
                "end_time": "09:50",
                "location": "ICS 174",
                "instructors": ["STAFF"],
            },
        ],
        "IN4MATX43": [
            {
                "section_code": "30000",
                "section_num": "A",
                "section_type": "Lec",
                "days": "MWF",
                "start_time": "13:00",
                "end_time": "13:50",
                "location": "SSL 140",
                "instructors": ["BUILDER, B."],
            },
        ],
    }
    titles = {
        "COMPSCI161": "Design and Analysis of Algorithms",
        "IN4MATX43": "Introduction to Software Engineering",
    }

    def fake_get_sections(course_id: str, term: str) -> dict:
        assert term == "Spring 2025"
        sections = sections_by_course.get(course_id, [])
        return {"found": bool(sections), "source": "test", "sections": sections}

    def fake_get_course_info(course_id: str) -> dict:
        title = titles.get(course_id)
        if not title:
            return {"found": False, "source": "none"}
        return {
            "found": True,
            "source": "test",
            "course": {"course_id": course_id, "title": title},
        }

    monkeypatch.setattr(db, "get_sections", fake_get_sections)
    monkeypatch.setattr(db, "get_course_info", fake_get_course_info)


def test_schedule_add_normalizes_section_codes_dedupes_and_builds_events(
    app_client,
    fake_schedule_catalog,
):
    session_id = "schedule_add_case"

    first = app_client.post(
        "/api/schedule/add",
        json={
            "session_id": session_id,
            "course_id": "COMPSCI161",
            "section": "20000",
            "term": "Spring 2025",
        },
    )
    duplicate = app_client.post(
        "/api/schedule/add",
        json={
            "session_id": session_id,
            "course_id": "COMPSCI161",
            "section": "A",
            "term": "Spring 2025",
        },
    )
    discussion = app_client.post(
        "/api/schedule/add",
        json={
            "session_id": session_id,
            "course_id": "COMPSCI161",
            "section": "A1",
            "term": "Spring 2025",
        },
    )

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert discussion.status_code == 200

    assert first.json()["pending_schedule"] == [
        {"course_id": "COMPSCI161", "section": "A", "status": "pending"}
    ]
    assert duplicate.json()["pending_schedule"] == first.json()["pending_schedule"]

    payload = discussion.json()
    assert payload["ok"] is True
    assert payload["pending_schedule"] == [
        {"course_id": "COMPSCI161", "section": "A", "status": "pending"},
        {"course_id": "COMPSCI161", "section": "A1", "status": "pending"},
    ]

    assert [
        (event["course_id"], event["section_num"], event["section_code"], event["day"])
        for event in payload["events"]
    ] == [
        ("COMPSCI161", "A", "20000", "Tue"),
        ("COMPSCI161", "A", "20000", "Thu"),
        ("COMPSCI161", "A1", "20001", "Fri"),
    ]
    assert payload["events"][0]["title"] == "Design and Analysis of Algorithms"
    assert payload["events"][0]["section"] == "20000"
    assert state_module._sessions[session_id]["pending_schedule"] == payload[
        "pending_schedule"
    ]


def test_schedule_remove_specific_section_then_whole_course_with_null_section(
    app_client,
    fake_schedule_catalog,
):
    session_id = "schedule_remove_case"
    for course_id, section in (
        ("COMPSCI161", "A"),
        ("COMPSCI161", "A1"),
        ("IN4MATX43", "A"),
    ):
        response = app_client.post(
            "/api/schedule/add",
            json={
                "session_id": session_id,
                "course_id": course_id,
                "section": section,
                "term": "Spring 2025",
            },
        )
        assert response.status_code == 200

    remove_lecture = app_client.post(
        "/api/schedule/remove",
        json={
            "session_id": session_id,
            "course_id": "COMPSCI161",
            "section": "20000",
            "term": "Spring 2025",
        },
    )

    assert remove_lecture.status_code == 200
    assert remove_lecture.json()["pending_schedule"] == [
        {"course_id": "COMPSCI161", "section": "A1", "status": "pending"},
        {"course_id": "IN4MATX43", "section": "A", "status": "pending"},
    ]
    assert all(
        not (
            event["course_id"] == "COMPSCI161"
            and event["section_num"] == "A"
        )
        for event in remove_lecture.json()["events"]
    )

    remove_course = app_client.post(
        "/api/schedule/remove",
        json={
            "session_id": session_id,
            "course_id": "COMPSCI161",
            "section": None,
            "term": "Spring 2025",
        },
    )

    assert remove_course.status_code == 200
    assert remove_course.json()["pending_schedule"] == [
        {"course_id": "IN4MATX43", "section": "A", "status": "pending"}
    ]
    assert {
        (event["course_id"], event["section_num"], event["day"])
        for event in remove_course.json()["events"]
    } == {
        ("IN4MATX43", "A", "Mon"),
        ("IN4MATX43", "A", "Wed"),
        ("IN4MATX43", "A", "Fri"),
    }


def test_schedule_clear_wipes_only_requested_session(
    app_client,
    fake_schedule_catalog,
):
    first_session = "schedule_clear_first"
    second_session = "schedule_clear_second"

    for session_id in (first_session, second_session):
        response = app_client.post(
            "/api/schedule/add",
            json={
                "session_id": session_id,
                "course_id": "COMPSCI161",
                "section": "A",
                "term": "Spring 2025",
            },
        )
        assert response.status_code == 200

    cleared = app_client.post(
        "/api/schedule/clear",
        json={"session_id": first_session, "term": "Spring 2025"},
    )

    assert cleared.status_code == 200
    assert cleared.json() == {"ok": True, "pending_schedule": [], "events": []}
    assert state_module._sessions[first_session]["pending_schedule"] == []
    assert state_module._sessions[second_session]["pending_schedule"] == [
        {"course_id": "COMPSCI161", "section": "A", "status": "pending"}
    ]
