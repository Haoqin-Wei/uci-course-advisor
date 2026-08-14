from __future__ import annotations

import json

import pytest

from app.data import sessions as sessions_data


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
        "STATS67": [
            {
                "section_code": "40000",
                "section_num": "A",
                "section_type": "Lec",
                "days": "Tu",
                "start_time": "10:30",
                "end_time": "11:50",
                "location": "SSL 290",
                "instructors": ["STAFF"],
            },
        ],
    }
    titles = {
        "COMPSCI161": "Design and Analysis of Algorithms",
        "IN4MATX43": "Introduction to Software Engineering",
        "STATS67": "Introduction to Probability and Statistics",
    }
    section_calls: list[tuple[str, str]] = []

    def fake_get_sections(course_id: str, term: str) -> dict:
        assert term in {"2025 Spring", "2025 Fall"}
        section_calls.append((course_id, term))
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
    return section_calls


def test_schedule_add_normalizes_section_codes_dedupes_and_builds_events(
    app_client,
    fake_schedule_catalog,
):
    session_id = sessions_data.create_session(
        "demo_001",
        title="Schedule add fixture",
        term_scope="Spring 2025",
    )

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
        {
            "course_id": "COMPSCI161",
            "section": "A",
            "status": "pending",
            "term": "2025 Spring",
        }
    ]
    assert duplicate.json()["pending_schedule"] == first.json()["pending_schedule"]

    payload = discussion.json()
    assert payload["ok"] is True
    assert payload["pending_schedule"] == [
        {"course_id": "COMPSCI161", "section": "A", "status": "pending", "term": "2025 Spring"},
        {"course_id": "COMPSCI161", "section": "A1", "status": "pending", "term": "2025 Spring"},
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
    assert {event["term"] for event in payload["events"]} == {"2025 Spring"}
    assert payload["events"][0]["section"] == "20000"
    assert sessions_data.get_session_state("demo_001", session_id)["pending_schedule"] == payload[
        "pending_schedule"
    ]


def test_schedule_add_ignores_removed_check_metadata_without_fake_event(
    app_client,
    fake_schedule_catalog,
):
    session_id = sessions_data.create_session(
        "demo_001",
        title="Direct schedule fixture",
        term_scope="Spring 2025",
    )

    response = app_client.post(
        "/api/schedule/add",
        json={
            "session_id": session_id,
            "course_id": "COMPSCI161",
            "section": "Z9",
            "term": "Spring 2025",
            "verification_status": "conflict",
            "verification_notices": ["Section identity could not be confirmed."],
            "verified_at": "2026-07-24T02:00:00Z",
            "source_badges": ["live_anteater_websoc"],
            "materialization_status": "unresolved",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    entry = payload["pending_schedule"][0]
    assert "verification_status" not in entry
    assert "verification_notices" not in entry
    assert "source_badges" not in entry
    assert payload["events"] == []


def test_schedule_refresh_upgrades_unresolved_entry_without_deleting_it(
    app_client,
    fake_schedule_catalog,
    monkeypatch,
):
    from app.data import db

    session_id = sessions_data.create_session(
        "demo_001",
        title="Schedule refresh fixture",
        term_scope="Spring 2025",
    )
    added = app_client.post(
        "/api/schedule/add",
        json={
            "session_id": session_id,
            "course_id": "COMPSCI161",
            "section": "Z9",
            "term": "Spring 2025",
            "verification_status": "unverified",
            "verification_notices": [
                "Section could not be resolved; no calendar block was created."
            ],
            "materialization_status": "unresolved",
        },
    )
    assert added.status_code == 200
    assert added.json()["events"] == []

    def fake_live(course_id, term, **kwargs):
        assert (course_id, term) == ("COMPSCI161", "2025 Spring")
        assert kwargs["force_refresh"] is True
        return {
            "found": True,
            "source": "live_anteater_websoc",
            "is_live": True,
            "stale": False,
            "retrieved_at": "2026-07-24T09:30:00Z",
            "sections": [
                {
                    "section_code": "99999",
                    "section_num": "Z9",
                    "section_type": "Lec",
                    "days": "TuTh",
                    "start_time": "14:00",
                    "end_time": "15:20",
                    "location": "DBH 1500",
                    "instructors": ["TESTER, A."],
                    "status": "OPEN",
                    "is_cancelled": False,
                    "seats_open": 4,
                }
            ],
        }

    monkeypatch.setattr(db, "get_live_sections", fake_live)
    refreshed = app_client.post(
        "/api/schedule/refresh",
        json={"session_id": session_id},
    )

    assert refreshed.status_code == 200
    payload = refreshed.json()
    assert payload["ok"] is True
    assert payload["timed_out"] is False
    assert len(payload["pending_schedule"]) == 1
    entry = payload["pending_schedule"][0]
    assert entry["course_id"] == "COMPSCI161"
    assert entry["section"] == "Z9"
    assert entry["data_status"] == "current"
    assert entry["materialization_status"] == "resolved"
    assert entry["sources"] == ["live_anteater_websoc"]
    assert entry["materialized_section"]["section_code"] == "99999"
    assert [(event["day"], event["section_code"]) for event in payload["events"]] == [
        ("Tue", "99999"),
        ("Thu", "99999"),
    ]
    assert payload["schedule_validation"]["unknowns"] == []

    monkeypatch.setattr(db, "get_live_sections", lambda *_args, **_kwargs: None)
    unavailable = app_client.post(
        "/api/schedule/refresh",
        json={"session_id": session_id},
    ).json()
    assert len(unavailable["pending_schedule"]) == 1
    assert unavailable["pending_schedule"][0]["materialization_status"] == "resolved"
    assert any(
        "planning item was kept" in notice
        for notice in unavailable["pending_schedule"][0]["notices"]
    )


def test_live_materialized_sections_participate_in_conflict_validation(
    app_client,
    fake_schedule_catalog,
    monkeypatch,
):
    from app.data import db

    session_id = sessions_data.create_session(
        "demo_001",
        title="Live conflict fixture",
        term_scope="Spring 2025",
    )
    for course_id, section in (("COMPSCI161", "Z9"), ("IN4MATX43", "Y8")):
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

    def fake_live(course_id, term, **_kwargs):
        section_num = "Z9" if course_id == "COMPSCI161" else "Y8"
        section_code = "99999" if course_id == "COMPSCI161" else "88888"
        return {
            "found": True,
            "source": "live_anteater_websoc",
            "is_live": True,
            "stale": False,
            "retrieved_at": "2026-07-24T09:30:00Z",
            "sections": [
                {
                    "section_code": section_code,
                    "section_num": section_num,
                    "section_type": "Lec",
                    "days": "TuTh",
                    "start_time": "14:00",
                    "end_time": "15:20",
                    "location": "DBH 1500",
                    "instructors": ["TESTER, A."],
                    "status": "OPEN",
                    "is_cancelled": False,
                    "seats_open": 4,
                }
            ],
        }

    monkeypatch.setattr(db, "get_live_sections", fake_live)
    payload = app_client.post(
        "/api/schedule/refresh",
        json={"session_id": session_id},
    ).json()

    assert len(payload["pending_schedule"]) == 2
    assert len(payload["events"]) == 4
    assert payload["schedule_validation"]["valid"] is False
    conflict = next(
        issue
        for issue in payload["schedule_validation"]["conflicts"]
        if issue["type"] == "time_conflict"
    )
    assert conflict["type"] == "time_conflict"
    assert {
        section["course_id"] for section in conflict["sections"]
    } == {"COMPSCI161", "IN4MATX43"}


def test_schedule_mutations_persist_pending_schedule_to_session_state_file(
    app_client,
    fake_schedule_catalog,
    runtime_paths,
):
    session_id = sessions_data.create_session(
        "demo_001",
        title="Schedule persistence fixture",
        term_scope="Spring 2025",
    )
    state_file = (
        runtime_paths.memory_root
        / "demo_001"
        / "sessions"
        / session_id
        / "state.json"
    )

    added = app_client.post(
        "/api/schedule/add",
        json={
            "session_id": session_id,
            "course_id": "COMPSCI161",
            "section": "A",
            "term": "Spring 2025",
        },
    )
    assert added.status_code == 200
    assert json.loads(state_file.read_text(encoding="utf-8"))["pending_schedule"] == [
        {"course_id": "COMPSCI161", "section": "A", "status": "pending", "term": "2025 Spring"}
    ]

    removed = app_client.post(
        "/api/schedule/remove",
        json={
            "session_id": session_id,
            "course_id": "COMPSCI161",
            "section": "A",
            "term": "Spring 2025",
        },
    )
    assert removed.status_code == 200
    assert json.loads(state_file.read_text(encoding="utf-8"))["pending_schedule"] == []

    app_client.post(
        "/api/schedule/add",
        json={
            "session_id": session_id,
            "course_id": "IN4MATX43",
            "section": "A",
            "term": "Spring 2025",
        },
    )
    cleared = app_client.post(
        "/api/schedule/clear",
        json={"session_id": session_id, "term": "Spring 2025"},
    )
    assert cleared.status_code == 200
    assert json.loads(state_file.read_text(encoding="utf-8"))["pending_schedule"] == []


def test_schedule_add_keeps_same_term_conflict_as_non_blocking_metadata(
    app_client,
    fake_schedule_catalog,
):
    session_id = sessions_data.create_session(
        "demo_001",
        title="Schedule conflict fixture",
        term_scope="Spring 2025",
    )

    first = app_client.post(
        "/api/schedule/add",
        json={
            "session_id": session_id,
            "course_id": "COMPSCI161",
            "section": "A",
            "term": "Spring 2025",
        },
    )
    conflict = app_client.post(
        "/api/schedule/add",
        json={
            "session_id": session_id,
            "course_id": "STATS67",
            "section": "A",
            "term": "Spring 2025",
        },
    )

    assert first.status_code == 200
    assert conflict.status_code == 200
    payload = conflict.json()
    assert payload["ok"] is True
    assert payload["pending_schedule"] == [
        {"course_id": "COMPSCI161", "section": "A", "status": "pending", "term": "2025 Spring"},
        {"course_id": "STATS67", "section": "A", "status": "pending", "term": "2025 Spring"},
    ]
    assert payload["schedule_validation"]["valid"] is False
    assert payload["schedule_validation"]["unknowns"] == []
    assert "time_conflict" in {
        issue["type"] for issue in payload["schedule_validation"]["conflicts"]
    }
    assert sessions_data.get_session_state("demo_001", session_id)["pending_schedule"] == [
        {"course_id": "COMPSCI161", "section": "A", "status": "pending", "term": "2025 Spring"},
        {"course_id": "STATS67", "section": "A", "status": "pending", "term": "2025 Spring"},
    ]


def test_schedule_add_surfaces_incomplete_primary_secondary_pairing(
    app_client,
    fake_schedule_catalog,
):
    session_id = sessions_data.create_session(
        "demo_001",
        title="Schedule pairing fixture",
        term_scope="Spring 2025",
    )

    lecture_only = app_client.post(
        "/api/schedule/add",
        json={
            "session_id": session_id,
            "course_id": "COMPSCI161",
            "section": "A",
            "term": "Spring 2025",
        },
    )
    assert lecture_only.status_code == 200
    assert lecture_only.json()["schedule_validation"]["valid"] is False
    assert lecture_only.json()["schedule_validation"]["conflicts"] == [
        {
            "type": "incomplete_pairing",
            "scope": "bundle",
            "message": "COMPSCI161 requires Lec plus Dis, but no Dis section is selected",
            "sections": [
                {
                    "course_id": "COMPSCI161",
                    "section_code": "20000",
                    "section_num": "A",
                    "window": "TuTh 10:00–11:20",
                }
            ],
        }
    ]

    with_discussion = app_client.post(
        "/api/schedule/add",
        json={
            "session_id": session_id,
            "course_id": "COMPSCI161",
            "section": "A1",
            "term": "Spring 2025",
        },
    )

    assert with_discussion.status_code == 200
    assert with_discussion.json()["schedule_validation"] == {
        "valid": True,
        "warnings": [],
        "conflicts": [],
        "unknowns": [],
    }


def test_schedule_remove_specific_section_then_whole_course_with_null_section(
    app_client,
    fake_schedule_catalog,
):
    session_id = sessions_data.create_session(
        "demo_001",
        title="Schedule remove fixture",
        term_scope="Spring 2025",
    )
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
        {"course_id": "COMPSCI161", "section": "A1", "status": "pending", "term": "2025 Spring"},
        {"course_id": "IN4MATX43", "section": "A", "status": "pending", "term": "2025 Spring"},
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
        {"course_id": "IN4MATX43", "section": "A", "status": "pending", "term": "2025 Spring"}
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
    first_session = sessions_data.create_session(
        "demo_001",
        title="Schedule clear first fixture",
        term_scope="Spring 2025",
    )
    second_session = sessions_data.create_session(
        "demo_001",
        title="Schedule clear second fixture",
        term_scope="Spring 2025",
    )

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
    assert cleared.json() == {
        "ok": True,
        "pending_schedule": [],
        "events": [],
        "schedule_validation": {
            "valid": True,
            "warnings": [],
            "conflicts": [],
            "unknowns": [],
        },
    }
    assert sessions_data.get_session_state("demo_001", first_session)["pending_schedule"] == []
    assert sessions_data.get_session_state("demo_001", second_session)["pending_schedule"] == [
        {"course_id": "COMPSCI161", "section": "A", "status": "pending", "term": "2025 Spring"}
    ]


def test_same_course_section_is_distinct_across_terms_and_removes_by_own_term(
    app_client,
    fake_schedule_catalog,
):
    session_id = sessions_data.create_session(
        "demo_001",
        title="Cross-term schedule fixture",
        term_scope="Spring 2025",
    )

    spring = app_client.post(
        "/api/schedule/add",
        json={
            "session_id": session_id,
            "course_id": "COMPSCI161",
            "section": "A",
            "term": "Spring 2025",
        },
    )
    fall = app_client.post(
        "/api/schedule/add",
        json={
            "session_id": session_id,
            "course_id": "COMPSCI161",
            "section": "A",
            "term": "Fall 2025",
        },
    )

    assert spring.status_code == 200
    assert fall.status_code == 200
    payload = fall.json()
    assert payload["pending_schedule"] == [
        {"course_id": "COMPSCI161", "section": "A", "status": "pending", "term": "2025 Spring"},
        {"course_id": "COMPSCI161", "section": "A", "status": "pending", "term": "2025 Fall"},
    ]
    assert payload["cross_term_notice"] == {
        "added_term": "2025 Fall",
        "existing_terms": ["2025 Spring"],
    }
    assert {event["term"] for event in payload["events"]} == {
        "2025 Spring",
        "2025 Fall",
    }
    assert {term for course, term in fake_schedule_catalog if course == "COMPSCI161"} == {
        "2025 Spring",
        "2025 Fall",
    }
    assert "time_conflict" not in {
        issue["type"] for issue in payload["schedule_validation"]["conflicts"]
    }

    removed = app_client.post(
        "/api/schedule/remove",
        json={
            "session_id": session_id,
            "course_id": "COMPSCI161",
            "section": "A",
            "term": "Spring 2025",
        },
    )
    assert removed.status_code == 200
    assert removed.json()["pending_schedule"] == [
        {"course_id": "COMPSCI161", "section": "A", "status": "pending", "term": "2025 Fall"}
    ]
    assert {event["term"] for event in removed.json()["events"]} == {"2025 Fall"}


def test_schedule_get_migrates_reliable_legacy_term_and_marks_ambiguous_unknown(
    app_client,
    fake_schedule_catalog,
):
    reliable_id = sessions_data.create_session(
        "demo_001",
        title="Reliable legacy schedule",
        term_scope="Spring 2025",
    )
    sessions_data.update_session_state(
        "demo_001",
        reliable_id,
        {
            "pending_schedule": [
                {"course_id": "IN4MATX43", "section": "A", "status": "pending"}
            ]
        },
    )

    reliable = app_client.get(f"/api/schedule?session_id={reliable_id}")
    assert reliable.status_code == 200
    assert reliable.json()["migrated"] is True
    assert reliable.json()["pending_schedule"][0]["term"] == "2025 Spring"
    assert {event["term"] for event in reliable.json()["events"]} == {"2025 Spring"}

    ambiguous_id = sessions_data.create_session(
        "demo_001",
        title="Ambiguous legacy schedule",
        term_scope="Spring 2025",
    )
    sessions_data.update_session_state(
        "demo_001",
        ambiguous_id,
        {
            "term": "Fall 2025",
            "pending_schedule": [
                {"course_id": "IN4MATX43", "section": "A", "status": "pending"}
            ],
        },
    )

    ambiguous = app_client.get(f"/api/schedule?session_id={ambiguous_id}")
    assert ambiguous.status_code == 200
    assert ambiguous.json()["pending_schedule"][0]["term"] == "unknown"
    assert ambiguous.json()["events"] == []
    assert ambiguous.json()["schedule_validation"]["unknowns"][0]["type"] == "unknown_term"

    removed = app_client.post(
        "/api/schedule/remove",
        json={
            "session_id": ambiguous_id,
            "course_id": "IN4MATX43",
            "section": "A",
            "term": "unknown",
        },
    )
    assert removed.status_code == 200
    assert removed.json()["pending_schedule"] == []
