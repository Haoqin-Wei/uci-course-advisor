from __future__ import annotations

from app.agent import tools as agent_tools


def _install_schedule_validation_catalog(monkeypatch):
    from app.data import db, policies

    sections_by_course = {
        "COMPSCI161": [
            {
                "course_id": "COMPSCI161",
                "section_code": "20000",
                "section_num": "A",
                "section_type": "Lec",
                "status": "OPEN",
                "days": "MW",
                "start_time": "09:00",
                "end_time": "10:20",
                "instructors": ["STAFF"],
            }
        ],
        "IN4MATX43": [
            {
                "course_id": "IN4MATX43",
                "section_code": "30000",
                "section_num": "A",
                "section_type": "Lec",
                "status": "OPEN",
                "days": "M",
                "start_time": "10:00",
                "end_time": "10:50",
                "instructors": ["STAFF"],
            }
        ],
    }

    def fake_get_course_info(course_id: str) -> dict:
        return {
            "found": True,
            "source": "test",
            "course": {
                "course_id": course_id,
                "title": f"{course_id} title",
                "department": course_id.rstrip("0123456789"),
                "units": "4",
            },
        }

    def fake_get_sections(course_id: str, term: str) -> dict:
        sections = sections_by_course.get(course_id, [])
        return {"found": bool(sections), "source": "test", "sections": sections}

    monkeypatch.setattr(policies, "is_term_past_deadline", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(policies, "get_term_calendar", lambda _term: {})
    monkeypatch.setattr(db, "get_course_info", fake_get_course_info)
    monkeypatch.setattr(db, "get_sections", fake_get_sections)
    monkeypatch.setattr(db, "get_student_profile", lambda _user_id: {"found": False})
    monkeypatch.setattr(db, "check_prerequisites_met", lambda *_args, **_kwargs: {"found": False})
    monkeypatch.setattr(db, "get_grade_distribution", lambda _course_id: {"found": False})


def test_propose_recommendation_attaches_schedule_bundle_validation(monkeypatch):
    _install_schedule_validation_catalog(monkeypatch)

    context = {
        "user_id": "demo_001",
        "term": "Fall 2026",
        "pending_schedule": [
            {"course_id": "IN4MATX43", "section": "A", "status": "pending"}
        ],
    }

    result = agent_tools.dispatch(
        "propose_recommendation",
        {
            "items": [
                {
                    "course_id": "COMPSCI161",
                    "category": "elective",
                    "priority": "high",
                    "reason": "Good systems preparation.",
                }
            ],
            "term": "Fall 2026",
        },
        context=context,
    )

    validation = result["schedule_validation"]
    assert result["ok"] is True
    assert validation["valid"] is False
    assert validation["unknowns"] == []
    assert validation["conflicts"] == [
        {
            "type": "time_conflict",
            "scope": "pending_schedule",
            "message": "COMPSCI161 A conflicts with pending IN4MATX43 A",
            "sections": [
                {
                    "course_id": "COMPSCI161",
                    "section_code": "20000",
                    "section_num": "A",
                    "window": "MW 09:00–10:20",
                },
                {
                    "course_id": "IN4MATX43",
                    "section_code": "30000",
                    "section_num": "A",
                    "window": "M 10:00–10:50",
                },
            ],
        }
    ]
    assert context["_proposed_cards"][0]["schedule_validation"] == validation


def test_agent_and_manual_add_return_same_schedule_conflict(
    app_client,
    monkeypatch,
):
    from app.data import sessions

    _install_schedule_validation_catalog(monkeypatch)
    pending_schedule = [
        {"course_id": "IN4MATX43", "section": "A", "status": "pending"}
    ]

    agent_context = {
        "user_id": "demo_001",
        "term": "Fall 2026",
        "pending_schedule": pending_schedule,
    }
    agent_result = agent_tools.dispatch(
        "propose_recommendation",
        {
            "items": [
                {
                    "course_id": "COMPSCI161",
                    "category": "elective",
                    "priority": "high",
                    "reason": "Good systems preparation.",
                }
            ],
            "term": "Fall 2026",
        },
        context=agent_context,
    )

    session_id = sessions.create_session(
        "demo_001",
        title="Manual conflict parity",
        term_scope="Fall 2026",
    )
    sessions.update_session_state(
        "demo_001",
        session_id,
        {"pending_schedule": pending_schedule},
    )
    manual_response = app_client.post(
        "/api/schedule/add",
        json={
            "session_id": session_id,
            "course_id": "COMPSCI161",
            "section": "A",
            "term": "Fall 2026",
        },
    )

    assert manual_response.status_code == 409
    assert manual_response.json()["requires_confirmation"] is True
    assert (
        manual_response.json()["schedule_validation"]
        == agent_result["schedule_validation"]
    )
