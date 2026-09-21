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


def test_tba_sections_are_saved_without_invented_meeting_times(app_client, monkeypatch):
    from app.data import db

    sections = [
        {"section_num": "A", "section_code": "36250", "section_type": "Lec",
         "days": "TBA", "start_time": "", "end_time": "", "location": "ON LINE"},
        {"section_num": "A1", "section_code": "36251", "section_type": "Dis",
         "days": "TBA", "start_time": "", "end_time": "", "location": "ON LINE"},
    ]
    monkeypatch.setattr(db, "get_sections", lambda *_: {"found": True, "sections": sections})
    monkeypatch.setattr(db, "get_course_info", lambda *_: {"found": False})
    session_id = sessions_data.create_session("demo_001", title="Online course", term_scope="Fall 2026")
    for section in sections:
        response = app_client.post("/api/schedule/add", json={
            "session_id": session_id, "course_id": "I&C SCI 139W",
            "section": section["section_num"], "term": "2026 Fall",
        })
        assert response.status_code == 200
        assert response.json()["events"] == []
    assert [entry["section"] for entry in response.json()["pending_schedule"]] == ["A", "A1"]
    assert response.json()["schedule_validation"]["unknowns"]
    restored = app_client.get("/api/schedule", params={"session_id": session_id}).json()
    assert len(restored["pending_schedule"]) == 2
    assert restored["events"] == []


@pytest.mark.parametrize("live_tba", [False, True])
def test_live_meeting_times_override_catalog_in_calendar_and_validation(
    app_client, fake_schedule_catalog, monkeypatch, live_tba,
):
    from app.data import db

    catalog_lookup = db.get_sections
    catalog_lecture = dict(catalog_lookup("COMPSCI161", "2025 Spring")["sections"][0])
    live_lecture = {**catalog_lecture, "days": "TBA" if live_tba else "TuTh",
                    "start_time": "" if live_tba else "10:45",
                    "end_time": "" if live_tba else "11:35"}
    # In one direction the catalog has no times; in the other its old times
    # must disappear when the live source changes this exact section to TBA.
    if not live_tba:
        catalog_lecture.update(days="TBA", start_time="", end_time="")

    def lookup(course_id, term):
        result = catalog_lookup(course_id, term)
        if course_id == "COMPSCI161":
            result = {**result, "sections": [catalog_lecture, *result["sections"][1:]]}
        return result

    def live_lookup(course_id, term, **_kwargs):
        return {"source": "live_anteater_websoc", "retrieved_at": "2026-09-21T16:00:00Z",
                "sections": [live_lecture] if course_id == "COMPSCI161" else lookup(course_id, term)["sections"]}

    monkeypatch.setattr(db, "get_sections", lookup)
    monkeypatch.setattr(db, "get_live_sections", live_lookup)
    session_id = sessions_data.create_session("demo_001", title="Refreshed meetings", term_scope="Spring 2025")
    for course_id in ("COMPSCI161", "STATS67"):
        assert app_client.post("/api/schedule/add", json={
            "session_id": session_id, "course_id": course_id, "section": "A", "term": "2025 Spring",
        }).status_code == 200
    refreshed = app_client.post("/api/schedule/refresh", json={"session_id": session_id})
    assert refreshed.status_code == 200
    restored = app_client.get("/api/schedule", params={"session_id": session_id})
    for payload in (refreshed.json(), restored.json()):
        meetings = [event for event in payload["events"] if event["course_id"] == "COMPSCI161"]
        time_conflicts = [issue for issue in payload["schedule_validation"]["conflicts"] if issue["type"] == "time_conflict"]
        if live_tba:
            assert meetings == []
            assert time_conflicts == []
            assert payload["pending_schedule"][0]["materialization_status"] == "tba"
        else:
            assert [(event["day"], event["start"], event["end"]) for event in meetings] == [
                ("Tue", "10:45", "11:35"), ("Thu", "10:45", "11:35"),
            ]
            assert time_conflicts
            assert payload["pending_schedule"][0]["materialization_status"] == "resolved"


@pytest.fixture
def recorded_registrar_card():
    from pathlib import Path

    return json.loads((Path(__file__).parent / "fixtures/schedule/registrar_45c_legacy.json").read_text())


def test_recorded_45c_card_add_restore_remove_without_remote_queries(app_client, monkeypatch, recorded_registrar_card):
    from app.data import db

    def no_remote(*args, **kwargs):
        pytest.fail(f"A shown card must not re-fetch course data: {args}")

    monkeypatch.setattr(db, "get_sections", no_remote)
    monkeypatch.setattr(db, "get_course_info", no_remote)
    sid = sessions_data.create_session("demo_001", title="Recorded 45C", term_scope="Fall 2026")
    sessions_data.append_turn("demo_001", sid, "assistant", "", cards=[recorded_registrar_card])
    # Existing broken schedules are repaired on read, with no re-adding needed.
    sessions_data.update_session_state("demo_001", sid, {"pending_schedule": [
        {"course_id": "I&C SCI 45C", "section": "A", "status": "pending", "term": "2026 Fall"},
    ]})
    restored = app_client.get("/api/schedule", params={"session_id": sid})
    added = app_client.post("/api/schedule/add", json={
        "session_id": sid, "course_id": "I&C SCI 45C", "section": "36120", "term": "2026 Fall",
        "materialized_section": {"days": "M", "start_time": "01:00", "end_time": "02:00"},
    })
    for response in (restored, added):
        assert response.status_code == 200
        payload = response.json()
        assert len(payload["pending_schedule"]) == 1
        assert [(e["day"], e["start"], e["end"]) for e in payload["events"]] == [
            ("Tue", "12:30", "13:50"), ("Thu", "12:30", "13:50"),
        ]
        assert not any(i["type"] == "time_unknown" for i in payload["schedule_validation"]["unknowns"])
    removed = app_client.post("/api/schedule/remove", json={
        "session_id": sid, "course_id": "I&C SCI 45C", "section": "36120", "term": "2026 Fall",
    })
    assert removed.json()["pending_schedule"] == []
    assert removed.json()["events"] == []


def test_schedule_card_lookup_does_not_borrow_another_term_or_session(app_client, monkeypatch, recorded_registrar_card):
    from app.data import db

    sid = sessions_data.create_session("demo_001", title="Fall card", term_scope="Fall 2026")
    other = sessions_data.create_session("demo_001", title="Other conversation", term_scope="Fall 2026")
    sessions_data.append_turn("demo_001", sid, "assistant", "", cards=[recorded_registrar_card])
    calls = []
    monkeypatch.setattr(db, "get_sections", lambda cid, term: (calls.append((cid, term)) or {"found": False}))
    monkeypatch.setattr(db, "get_course_info", lambda *_: {"found": False})
    for session_id, term in [(sid, "2027 Winter"), (other, "2026 Fall")]:
        response = app_client.post("/api/schedule/add", json={
            "session_id": session_id, "course_id": "I&C SCI 45C", "section": "A", "term": term,
        })
        assert response.status_code == 200
        assert response.json()["events"] == []
    # Each missing course is fetched once, not again for dedup, validation and events.
    assert calls == [("I&C SCI 45C", "2027 Winter"), ("I&C SCI 45C", "2026 Fall")]


def test_concurrent_schedule_adds_preserve_both_sections(app_client, fake_schedule_catalog, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from time import sleep
    from app.data import db

    original = db.get_sections
    def slow_lookup(*args):
        sleep(0.03)
        return original(*args)
    monkeypatch.setattr(db, "get_sections", slow_lookup)
    sid = sessions_data.create_session("demo_001", title="Concurrent adds", term_scope="Spring 2025")
    def add(section):
        return app_client.post("/api/schedule/add", json={
            "session_id": sid, "course_id": "COMPSCI161", "section": section, "term": "2025 Spring",
        })
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(add, ["A", "A1"]))
    assert all(response.status_code == 200 for response in responses)
    assert {e["section"] for e in sessions_data.get_session_state("demo_001", sid)["pending_schedule"]} == {"A", "A1"}


def test_schedule_real_api_browser_flow(app_client, monkeypatch, recorded_registrar_card):
    """Opt-in Chrome integration; upstream network is still blocked by conftest."""
    import os
    if os.environ.get("SOLON_BROWSER_CHECK") != "1":
        pytest.skip("Set SOLON_BROWSER_CHECK=1 to run the real API browser check")
    import socket
    import subprocess
    import threading
    import time
    from pathlib import Path
    import uvicorn
    from main import app
    from app.data import db

    def no_remote(*args, **kwargs):
        raise AssertionError(f"Unexpected course lookup during a card click: {args}")
    monkeypatch.setattr(db, "get_sections", no_remote)
    monkeypatch.setattr(db, "get_course_info", no_remote)
    sid = sessions_data.create_session("demo_001", title="Registrar schedule integration", term_scope="Fall 2026")
    sessions_data.append_turn("demo_001", sid, "assistant", "Choose a section to add to your schedule.", cards=[recorded_registrar_card])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", loop="asyncio", log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.02)
        assert server.started
        result = subprocess.run(
            ["node", "scripts/verify_schedule_api_ui.cjs"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "SOLON_BASE_URL": f"http://127.0.0.1:{port}"},
            text=True, capture_output=True, timeout=90,
        )
        print(result.stdout)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def test_refresh_completion_keeps_edits_made_while_waiting(app_client, fake_schedule_catalog):
    from app.routers.chat import _finish_schedule_refresh
    from app.data import db

    sid = sessions_data.create_session("demo_001", title="Refresh race", term_scope="Spring 2025")
    initial = {"session_id": sid, "course_id": "COMPSCI161", "section": "A", "term": "2025 Spring"}
    assert app_client.post("/api/schedule/add", json=initial).status_code == 200
    old_live_result = db.get_sections("COMPSCI161", "2025 Spring")
    assert app_client.post("/api/schedule/remove", json=initial).status_code == 200
    assert app_client.post("/api/schedule/add", json={**initial, "course_id": "IN4MATX43"}).status_code == 200
    payload = _finish_schedule_refresh("demo_001", sid, {("COMPSCI161", "2025 Spring"): old_live_result})
    assert [entry["course_id"] for entry in payload["pending_schedule"]] == ["IN4MATX43"]
    assert {event["course_id"] for event in payload["events"]} == {"IN4MATX43"}
