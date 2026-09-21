from __future__ import annotations


def _create_and_login(client, email: str = "student@uci.edu") -> dict:
    from app.auth import security, store

    user = store.create_user(
        email,
        security.hash_password("password123"),
        age_18_attested=True,
        terms_version="2026-09-03",
        privacy_version="2026-09-03",
    )
    client.cookies.set(security.SESSION_COOKIE_NAME, security.sign_session(user["id"]))
    return user


def _payload(*, printed_at: str, grade: str = "F", credit_code=None) -> dict:
    return {
        "parser_version": "uci-current-v2",
        "printed_at": printed_at,
        "courses": [
            {
                "course_id": "I&C SCI 32",
                "department": "I&C SCI",
                "course_number": "32",
                "title": "PROG SOFTWARE LIBR",
                "units": 4,
                "grade": grade,
                "grade_points": 0 if grade == "F" else 13.2,
                "credit_code": credit_code,
                "effective_term": "2026 Winter Quarter",
                "confidence": 1,
            },
            {
                "course_id": "STATS 7",
                "department": "STATS",
                "course_number": "7",
                "title": "BASIC STATISTICS",
                "units": 4,
                "grade": "W",
                "grade_points": 0,
                "effective_term": "2025 Winter Quarter",
                "confidence": 1,
            },
        ],
        "exam_credits": [
            {
                "exam_type": "AP",
                "subject": "PSYCHOLOGY",
                "score": 5,
                "units": 4,
                "exam_date": "05/24",
                "uci_equivalent_course": None,
                "confidence": 1,
            }
        ],
        "transfer_credits": [],
        "university_requirements": [
            {
                "requirement_code": "Entry Level Writing",
                "status": "Course Passed",
                "status_date": "03/01/26",
            }
        ],
        "summary": {
            "official_uc_gpa": 3.4,
            "grade_units_attempted": 86,
            "total_units_passed": 86,
            "units_completed": 112.5,
        },
        "skipped_count": 0,
    }


def test_import_requires_authentication(app_client):
    response = app_client.post("/api/academic/transcript/import", json=_payload(printed_at="2026-09-03T13:25:00Z"))

    assert response.status_code == 401


def test_profile_summary_reveals_gpa_only_on_request_for_authenticated_owner(app_client):
    assert app_client.get("/api/academic/profile?include_gpa=true").status_code == 401
    _create_and_login(app_client)
    response = app_client.post(
        "/api/academic/transcript/import", json=_payload(printed_at="2026-09-03T13:25:00Z")
    )
    assert response.status_code == 200
    hidden = app_client.get("/api/academic/profile")
    assert hidden.headers["cache-control"] == "no-store"
    assert hidden.json()["gpa_available"] is True
    assert "official_uc_gpa" not in hidden.json()
    assert hidden.json()["units_completed"] == 112.5
    visible = app_client.get("/api/academic/profile?include_gpa=true").json()
    assert visible["official_uc_gpa"] == 3.4

    _create_and_login(app_client, "other-student@uci.edu")
    other = app_client.get("/api/academic/profile?include_gpa=true").json()
    assert other["official_uc_gpa"] is None
    assert other["gpa_available"] is False
    assert other["units_completed"] is None
    assert other["completed_courses"] == []


def test_profile_summary_preserves_zero_units_and_gpa(app_client):
    _create_and_login(app_client)
    payload = _payload(printed_at="2026-09-03T13:25:00Z")
    payload["summary"].update(official_uc_gpa=0, units_completed=0)
    assert app_client.post("/api/academic/transcript/import", json=payload).status_code == 200
    data = app_client.get("/api/academic/profile?include_gpa=true").json()
    assert data["gpa_available"] is True
    assert data["official_uc_gpa"] == 0
    assert data["units_completed"] == 0


def test_import_rejects_raw_transcript_fields(app_client):
    _create_and_login(app_client)
    body = _payload(printed_at="2026-09-03T13:25:00Z")
    body["raw_text"] = "must never be accepted"

    response = app_client.post("/api/academic/transcript/import", json=body)

    assert response.status_code == 422


def test_import_hides_nonpassing_courses_and_updates_effective_grade(app_client):
    _create_and_login(app_client)

    first = app_client.post(
        "/api/academic/transcript/import",
        json=_payload(printed_at="2026-01-07T13:00:00Z"),
    )
    assert first.status_code == 200
    assert first.json()["added"] == 2

    hidden = app_client.get("/api/academic/profile")
    assert hidden.status_code == 200
    assert hidden.json()["completed_courses"] == []

    second_body = _payload(
        printed_at="2026-09-03T13:25:00Z",
        grade="B+",
        credit_code="G0",
    )
    second = app_client.post("/api/academic/transcript/import", json=second_body)
    assert second.status_code == 200
    assert second.json()["updated"] == 1
    assert second.json()["unchanged"] == 1

    visible = app_client.get("/api/academic/profile").json()["completed_courses"]
    assert [course["course_id"] for course in visible] == ["I&C SCI 32"]
    assert visible[0]["title"] == "Programming with Software Libraries"
    assert "grade" not in visible[0]
    assert "units" not in visible[0]


def test_older_transcript_does_not_downgrade_newer_effective_record(app_client):
    _create_and_login(app_client)
    newer = _payload(
        printed_at="2026-09-03T13:25:00Z",
        grade="B+",
        credit_code="G0",
    )
    older = _payload(printed_at="2026-01-07T13:00:00Z", grade="F")

    assert app_client.post("/api/academic/transcript/import", json=newer).status_code == 200
    response = app_client.post("/api/academic/transcript/import", json=older)

    assert response.status_code == 200
    assert response.json()["skipped"] == 0
    assert response.json()["older_ignored"] == 2
    assert {issue["reason_code"] for issue in response.json()["issues"]} == {
        "older_record_ignored"
    }
    visible = app_client.get("/api/academic/profile").json()["completed_courses"]
    assert [course["course_id"] for course in visible] == ["I&C SCI 32"]


def test_newer_nonpassing_attempt_removes_prior_transcript_completion(app_client):
    user = _create_and_login(app_client)
    passed = _payload(
        printed_at="2026-01-07T13:00:00Z",
        grade="B+",
        credit_code="G0",
    )
    failed = _payload(printed_at="2026-09-03T13:25:00Z", grade="F")

    assert app_client.post("/api/academic/transcript/import", json=passed).status_code == 200
    assert app_client.post("/api/academic/transcript/import", json=failed).status_code == 200

    assert app_client.get("/api/academic/profile").json()["completed_courses"] == []
    from app.memory import get_memory_manager

    assert get_memory_manager().get_profile(user["id"])["completed_courses"] == []


def test_newer_missing_gpa_does_not_block_older_available_gpa(app_client):
    user = _create_and_login(app_client)
    no_gpa = _payload(printed_at="2026-09-03T13:25:00Z")
    no_gpa["summary"]["official_uc_gpa"] = None
    older_with_gpa = _payload(printed_at="2026-01-07T13:00:00Z")

    assert app_client.post("/api/academic/transcript/import", json=no_gpa).status_code == 200
    assert app_client.post("/api/academic/transcript/import", json=older_with_gpa).status_code == 200

    from app.academic import get_academic_profile
    from app.academic.store import get_ai_academic_context

    academic = get_academic_profile(user["id"])
    assert academic["gpa_as_of"].startswith("2026-01-07")
    assert get_ai_academic_context(user["id"])["uc_gpa"] == 3.4


def test_manual_courses_are_preserved_and_catalog_titled(app_client):
    user = _create_and_login(app_client)
    response = app_client.post(
        f"/api/memory/{user['id']}/profile",
        json={"completed_courses": ["I&C SCI 31"]},
    )
    assert response.status_code == 200

    academic = app_client.get("/api/academic/profile")

    assert academic.status_code == 200
    assert academic.json()["completed_courses"] == [
        {
            "course_id": "I&C SCI 31",
            "title": "Introduction to Programming",
            "catalog_matched": True,
            "transcript_seen": False,
        }
    ]


def test_all_catalog_departments_are_valid_structured_departments():
    import csv
    from pathlib import Path

    from app.catalog.departments import resolve_department

    path = Path(__file__).resolve().parents[1] / "data" / "uci" / "courses.csv"
    with path.open("r", encoding="utf-8", newline="") as stream:
        departments = {row["department"].strip() for row in csv.DictReader(stream)}

    unsupported = sorted(
        department
        for department in departments
        if resolve_department(department) != department
    )
    assert unsupported == []


def test_regression_imports_previously_rejected_departments(app_client):
    _create_and_login(app_client)
    body = _payload(printed_at="2026-09-03T13:25:00Z", grade="A")
    identifiers = [
        ("AC ENG", "22A"),
        ("AC ENG", "20B"),
        ("AC ENG", "20C"),
        ("BME", "3"),
        ("PHILOS", "1"),
        ("EARTHSS", "1"),
        ("LSCI", "3"),
        ("DRAMA", "15"),
    ]
    body["courses"] = [
        {
            "course_id": f"{department} {number}",
            "department": department,
            "course_number": number,
            "title": "SANITIZED TEST TITLE",
            "units": 4,
            "grade": "A",
            "grade_points": 16,
            "credit_code": None,
            "effective_term": "2026 Spring Quarter",
            "confidence": 1,
        }
        for department, number in identifiers
    ]

    response = app_client.post("/api/academic/transcript/import", json=body)

    assert response.status_code == 200
    result = response.json()
    assert result["read"] == 8
    assert result["accepted"] == 8
    assert result["added"] == 8
    assert result["skipped"] == 0
    assert result["issues"] == []
    visible_ids = {
        course["course_id"]
        for course in app_client.get("/api/academic/profile").json()["completed_courses"]
    }
    assert visible_ids == {f"{department} {number}" for department, number in identifiers}


def test_import_returns_sanitized_reason_for_invalid_department(app_client):
    _create_and_login(app_client)
    body = _payload(printed_at="2026-09-03T13:25:00Z")
    body["courses"] = [{
        **body["courses"][0],
        "course_id": "NOTREAL 10",
        "department": "NOTREAL",
        "course_number": "10",
    }]

    response = app_client.post("/api/academic/transcript/import", json=body)

    assert response.status_code == 200
    result = response.json()
    assert result["read"] == 1
    assert result["accepted"] == 0
    assert result["skipped"] == 1
    assert result["issues"] == [{
        "course_id": "NOTREAL 10",
        "reason_code": "unsupported_department",
        "message": "Department NOTREAL is not in the current UCI catalog.",
        "count": 1,
        "level": "warning",
    }]


def test_reimport_reports_unchanged_instead_of_skipped(app_client):
    _create_and_login(app_client)
    body = _payload(printed_at="2026-09-03T13:25:00Z", grade="B+")

    assert app_client.post("/api/academic/transcript/import", json=body).status_code == 200
    response = app_client.post("/api/academic/transcript/import", json=body)

    assert response.status_code == 200
    result = response.json()
    assert result["added"] == 0
    assert result["updated"] == 0
    assert result["unchanged"] == 2
    assert result["accepted"] == 2
    assert result["skipped"] == 0


def test_imported_grade_is_available_to_prerequisite_engine(app_client):
    user = _create_and_login(app_client)
    assert app_client.post(
        "/api/academic/transcript/import",
        json=_payload(
            printed_at="2026-09-03T13:25:00Z",
            grade="B+",
            credit_code="G0",
        ),
    ).status_code == 200

    from app.data import db

    profile = db.get_student_profile(user["id"])["profile"]
    result = db.check_prerequisites_met(
        "I&C SCI 33",
        completed_courses=profile["completed_course_attempts"],
        in_progress_courses=[],
    )

    assert result["status"] == "met"
    assert "I&C SCI 32" in " ".join(result["satisfied"])


def test_account_deletion_removes_auth_and_academic_data(app_client):
    user = _create_and_login(app_client)
    assert app_client.post(
        "/api/academic/transcript/import",
        json=_payload(printed_at="2026-09-03T13:25:00Z", grade="B+", credit_code="G0"),
    ).status_code == 200

    deleted = app_client.request(
        "DELETE",
        "/api/auth/account",
        json={"password": "password123"},
    )

    assert deleted.status_code == 200
    from app.auth import store
    from app.academic import get_academic_profile

    assert store.find_user_by_id(user["id"]) is None
    assert get_academic_profile(user["id"])["completed_courses"] == []


def test_private_beta_registration_rejects_non_uci_email(app_client):
    from app.routers.auth import CURRENT_TERMS_VERSION, CURRENT_PRIVACY_VERSION

    response = app_client.post(
        "/api/auth/register",
        json={
            "email": "student@example.edu", "password": "password123",
            "password_confirmation": "password123", "age_18_confirmed": True,
            "terms_accepted": True, "terms_version": CURRENT_TERMS_VERSION,
            "privacy_version": CURRENT_PRIVACY_VERSION,
        },
    )

    assert response.status_code == 400
    assert "@uci.edu" in response.json()["detail"]
