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


def test_latest_upload_replaces_effective_record_even_when_pdf_printed_earlier(app_client):
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
    assert response.json()["older_ignored"] == 0
    assert response.json()["updated"] == 1
    assert response.json()["unchanged"] == 1
    assert response.json()["issues"] == []
    visible = app_client.get("/api/academic/profile").json()["completed_courses"]
    assert visible == []


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


def test_latest_upload_uses_its_gpa_even_when_pdf_printed_earlier(app_client):
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


def _passing_snapshot(identifiers, *, printed_at: str, gpa: float) -> dict:
    body = _payload(printed_at=printed_at, grade="A" if gpa == 4 else "B+")
    body["courses"] = [
        {
            "course_id": f"{department} {number}",
            "department": department,
            "course_number": number,
            "title": "SANITIZED TEST TITLE",
            "units": 4,
            "grade": "A" if gpa == 4 else "B+",
            "grade_points": 16 if gpa == 4 else 13.2,
            "effective_term": "2026 Spring Quarter",
            "confidence": 1,
        }
        for department, number in identifiers
    ]
    body["summary"] = {
        "official_uc_gpa": gpa,
        "grade_units_attempted": len(identifiers) * 4,
        "total_units_passed": len(identifiers) * 4,
        "units_completed": len(identifiers) * 4,
    }
    return body


def test_new_pdf_replaces_a_ten_course_4_gpa_profile_with_b_nineteen_course_3_3_gpa(
    app_client,
):
    from app.academic import store
    from app.data import db
    from app.memory import get_memory_manager

    user = _create_and_login(app_client)
    shared = [("MATH", number) for number in ("2B", "2D", "2E", "3A", "3D")]
    student_a_ids = [
        *shared,
        ("AC ENG", "20A"),
        ("AC ENG", "20B"),
        ("AC ENG", "20C"),
        ("AC ENG", "22A"),
        ("I&C SCI", "31"),
    ]
    student_b_ids = [
        *shared,
        *[("PHYSICS", number) for number in ("2", "7C", "7D", "7E", "7LC", "7LD")],
        ("CHEM", "1A"),
        ("CHEM", "1C"),
        ("DRAMA", "15"),
        ("BME", "3"),
        ("ANTHRO", "2A"),
        ("ECON", "23"),
        ("I&C SCI", "32"),
        ("WRITING", "45"),
    ]
    student_a = _passing_snapshot(
        student_a_ids, printed_at="2026-09-03T13:25:00Z", gpa=4.0
    )
    student_a["transfer_credits"] = [{"institution_name": "TEST COLLEGE", "units": 4}]
    student_a["summary"]["units_completed"] = 44
    student_b = _passing_snapshot(
        student_b_ids, printed_at="2026-01-07T13:00:00Z", gpa=3.3
    )
    student_b.update(exam_credits=[], transfer_credits=[], university_requirements=[])

    assert app_client.post("/api/academic/transcript/import", json=student_a).status_code == 200
    first_profile = app_client.get("/api/academic/profile?include_gpa=true").json()
    assert len(first_profile["completed_courses"]) == 10
    assert first_profile["official_uc_gpa"] == 4.0
    assert app_client.post(
        f"/api/memory/{user['id']}/profile",
        json={"completed_courses": [*get_memory_manager().get_profile(user["id"])["completed_courses"], "STATS 7"]},
    ).status_code == 200

    replaced = app_client.post("/api/academic/transcript/import", json=student_b)

    assert replaced.status_code == 200
    result = replaced.json()
    assert result["added"] == 14
    assert result["updated"] == 5
    assert result["accepted"] == 19
    assert result["older_ignored"] == 0
    expected_ids = {f"{department} {number}" for department, number in student_b_ids}
    profile = app_client.get("/api/academic/profile?include_gpa=true").json()
    assert {item["course_id"] for item in profile["completed_courses"]} == expected_ids
    assert len(profile["completed_courses"]) == 19
    assert profile["official_uc_gpa"] == 3.3
    assert profile["units_completed"] == 76
    assert profile["total_units_passed"] == 76
    assert profile["gpa_as_of"].startswith("2026-01-07")
    assert set(get_memory_manager().get_profile(user["id"])["completed_courses"]) == expected_ids
    context = store.get_ai_academic_context(user["id"])
    assert context["uc_gpa"] == 3.3
    assert {item["course_id"] for item in context["courses"]} == expected_ids
    assert all(item["effective_grade"] == "B+" for item in context["courses"])
    assert {item["course_id"] for item in db.get_student_profile(user["id"])["profile"]["completed_course_attempts"]} == expected_ids
    with store._conn() as conn:
        for table in ("exam_credits", "transfer_credits", "university_requirements"):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user["id"],)
            ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM student_courses WHERE user_id = ?", (user["id"],)
        ).fetchone()[0] == 19


def test_new_pdf_missing_summary_values_clears_previous_gpa_and_units(app_client):
    from app.academic import store

    user = _create_and_login(app_client)
    previous = _payload(printed_at="2026-09-03T13:25:00Z", grade="B+")
    replacement = _payload(printed_at="2026-01-07T13:00:00Z", grade="B+")
    replacement["summary"] = {}

    assert app_client.post("/api/academic/transcript/import", json=previous).status_code == 200
    assert app_client.post("/api/academic/transcript/import", json=replacement).status_code == 200

    profile = app_client.get("/api/academic/profile?include_gpa=true").json()
    assert profile["official_uc_gpa"] is None
    assert profile["gpa_available"] is False
    assert profile["gpa_as_of"] is None
    assert profile["units_completed"] is None
    assert profile["total_units_passed"] is None
    assert store.get_ai_academic_context(user["id"])["uc_gpa"] is None
    with store._conn() as conn:
        stored = conn.execute(
            "SELECT * FROM student_academic_profiles WHERE user_id = ?", (user["id"],)
        ).fetchone()
        assert stored["grade_units_attempted"] is None
        assert stored["gpa_imported_at"] is None


def test_empty_successful_snapshot_clears_courses_manual_courses_and_academic_values(app_client):
    from app.academic import store
    from app.memory import get_memory_manager

    user = _create_and_login(app_client)
    previous = _payload(printed_at="2026-09-03T13:25:00Z", grade="B+")
    previous["transfer_credits"] = [{"institution_name": "TEST COLLEGE", "units": 8}]
    assert app_client.post("/api/academic/transcript/import", json=previous).status_code == 200
    assert app_client.post(
        f"/api/memory/{user['id']}/profile",
        json={"completed_courses": ["I&C SCI 32", "I&C SCI 31"]},
    ).status_code == 200

    response = app_client.post(
        "/api/academic/transcript/import",
        json={"parser_version": "uci-current-v2", "courses": [], "summary": {}},
    )

    assert response.status_code == 200
    assert response.json()["accepted"] == 0
    assert response.json()["completed_course_ids"] == []
    assert response.json()["transcript_course_ids"] == []
    profile = app_client.get("/api/academic/profile?include_gpa=true").json()
    assert profile["completed_courses"] == []
    assert profile["official_uc_gpa"] is None
    assert profile["units_completed"] is None
    assert profile["total_units_passed"] is None
    assert profile["last_import"]["accepted_count"] == 0
    assert get_memory_manager().get_profile(user["id"])["completed_courses"] == []
    assert store.get_ai_academic_context(user["id"]) == {"courses": [], "uc_gpa": None}
    with store._conn() as conn:
        for table in ("student_courses", "exam_credits", "transfer_credits", "university_requirements"):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user["id"],)
            ).fetchone()[0] == 0


def test_new_pdf_replaces_all_credit_and_requirement_rows(app_client):
    from app.academic import store

    user = _create_and_login(app_client)
    previous = _payload(printed_at="2026-09-03T13:25:00Z", grade="B+")
    previous["transfer_credits"] = [{"institution_name": "OLD COLLEGE", "units": 8}]
    replacement = _payload(printed_at="2026-01-07T13:00:00Z", grade="B+")
    replacement["exam_credits"] = [{"exam_type": "IB", "subject": "MATHEMATICS", "units": 4}]
    replacement["transfer_credits"] = [{"institution_name": "NEW COLLEGE", "units": 12}]
    replacement["university_requirements"] = [{"requirement_code": "American History", "status": "Satisfied"}]

    assert app_client.post("/api/academic/transcript/import", json=previous).status_code == 200
    assert app_client.post("/api/academic/transcript/import", json=replacement).status_code == 200

    with store._conn() as conn:
        exams = conn.execute("SELECT exam_type, subject, units FROM exam_credits WHERE user_id = ?", (user["id"],)).fetchall()
        assert [dict(row) for row in exams] == [{"exam_type": "IB", "subject": "MATHEMATICS", "units": 4}]
        transfers = conn.execute("SELECT institution_name, units FROM transfer_credits WHERE user_id = ?", (user["id"],)).fetchall()
        assert [dict(row) for row in transfers] == [{"institution_name": "NEW COLLEGE", "units": 12}]
        requirements = conn.execute("SELECT requirement_code, status FROM university_requirements WHERE user_id = ?", (user["id"],)).fetchall()
        assert [dict(row) for row in requirements] == [{"requirement_code": "American History", "status": "Satisfied"}]


def test_valid_partial_replacement_drops_old_courses_and_reports_skipped_new_rows(app_client):
    from app.memory import get_memory_manager

    user = _create_and_login(app_client)
    previous = _payload(printed_at="2026-09-03T13:25:00Z", grade="B+")
    replacement = _passing_snapshot([("MATH", "2B")], printed_at="2026-01-07T13:00:00Z", gpa=3.3)
    replacement["courses"].append({**previous["courses"][0], "course_id": "NOTREAL 10", "department": "NOTREAL", "course_number": "10"})
    assert app_client.post("/api/academic/transcript/import", json=previous).status_code == 200

    response = app_client.post("/api/academic/transcript/import", json=replacement)

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert response.json()["skipped"] == 1
    assert response.json()["issues"][0]["reason_code"] == "unsupported_department"
    assert [item["course_id"] for item in app_client.get("/api/academic/profile").json()["completed_courses"]] == ["MATH 2B"]
    assert get_memory_manager().get_profile(user["id"])["completed_courses"] == ["MATH 2B"]


def test_invalid_replacement_request_preserves_previous_snapshot(app_client):
    from app.memory import get_memory_manager

    user = _create_and_login(app_client)
    previous = _payload(printed_at="2026-09-03T13:25:00Z", grade="B+")
    assert app_client.post("/api/academic/transcript/import", json=previous).status_code == 200
    before = app_client.get("/api/academic/profile?include_gpa=true").json()
    memory_before = get_memory_manager().get_profile(user["id"])
    invalid = _payload(printed_at="2026-01-07T13:00:00Z")
    invalid["summary"]["official_uc_gpa"] = 99

    assert app_client.post("/api/academic/transcript/import", json=invalid).status_code == 422

    assert app_client.get("/api/academic/profile?include_gpa=true").json() == before
    assert get_memory_manager().get_profile(user["id"]) == memory_before


def test_failed_snapshot_transaction_rolls_back_replacement(app_client, monkeypatch):
    import pytest
    from app.academic import store
    from app.memory import get_memory_manager

    user = _create_and_login(app_client)
    previous = _payload(printed_at="2026-09-03T13:25:00Z", grade="B+")
    previous["transfer_credits"] = [{"institution_name": "TEST COLLEGE", "units": 8}]
    assert app_client.post("/api/academic/transcript/import", json=previous).status_code == 200
    before = app_client.get("/api/academic/profile?include_gpa=true").json()
    memory_before = get_memory_manager().get_profile(user["id"])
    replacement = _passing_snapshot([("MATH", "2B")], printed_at="2026-01-07T13:00:00Z", gpa=3.3)
    replacement.update(exam_credits=[], transfer_credits=[], university_requirements=[])

    def fail_requirements(*args, **kwargs):
        raise RuntimeError("synthetic persistence failure")

    monkeypatch.setattr(store, "_upsert_requirements", fail_requirements)
    with pytest.raises(RuntimeError, match="synthetic persistence failure"):
        app_client.post("/api/academic/transcript/import", json=replacement)

    assert app_client.get("/api/academic/profile?include_gpa=true").json() == before
    assert get_memory_manager().get_profile(user["id"]) == memory_before
    with store._conn() as conn:
        for table in ("exam_credits", "transfer_credits", "university_requirements", "transcript_imports"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user["id"],)).fetchone()[0] == 1


def test_failed_memory_write_aborts_import_and_same_request_retry_recovers(app_client, monkeypatch):
    from app.academic import store
    from app.memory import get_memory_manager

    user = _create_and_login(app_client)
    previous = _payload(printed_at="2026-09-03T13:25:00Z", grade="B+")
    previous["client_request_id"] = "mirror-request-student-a"
    assert app_client.post("/api/academic/transcript/import", json=previous).status_code == 200
    assert app_client.post(
        f"/api/memory/{user['id']}/profile",
        json={"completed_courses": ["I&C SCI 32", "ANTHRO 2A"]},
    ).status_code == 200
    manager = get_memory_manager()
    provider = manager.provider
    real_update = provider.update_profile
    before = app_client.get("/api/academic/profile?include_gpa=true").json()
    ai_before = store.get_ai_academic_context(user["id"])
    memory_before = manager.get_profile(user["id"])
    replacement = _passing_snapshot([("MATH", "2B")], printed_at="2026-01-07T13:00:00Z", gpa=3.3)
    replacement["client_request_id"] = "mirror-request-student-b"

    def fail_update(*args, **kwargs):
        raise OSError("synthetic mirror write failure")

    monkeypatch.setattr(provider, "update_profile", fail_update)
    # The existing non-strict manager API intentionally tolerates write errors.
    # Transcript imports must use its strict path instead of accepting this
    # returned old profile as successful synchronization.
    assert manager.update_profile(user["id"], {"completed_courses": ["MATH 2B"]}) == memory_before
    failed = app_client.post("/api/academic/transcript/import", json=replacement)

    assert failed.status_code == 503
    assert "try again" in failed.json()["detail"].lower()
    assert app_client.get("/api/academic/profile?include_gpa=true").json() == before
    assert store.get_ai_academic_context(user["id"]) == ai_before
    assert manager.get_profile(user["id"]) == memory_before
    with store._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transcript_imports WHERE user_id = ?", (user["id"],)).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM transcript_imports WHERE user_id = ? AND client_request_id = ?",
            (user["id"], replacement["client_request_id"]),
        ).fetchone()[0] == 0

    monkeypatch.setattr(provider, "update_profile", real_update)
    recovered = app_client.post("/api/academic/transcript/import", json=replacement)

    assert recovered.status_code == 200
    assert recovered.json()["duplicate"] is False
    assert manager.get_profile(user["id"])["completed_courses"] == ["MATH 2B"]
    assert [course["course_id"] for course in app_client.get("/api/academic/profile").json()["completed_courses"]] == ["MATH 2B"]
    assert store.get_ai_academic_context(user["id"])["uc_gpa"] == 3.3

    assert app_client.post(
        f"/api/memory/{user['id']}/profile",
        json={"completed_courses": ["MATH 2B", "I&C SCI 31"]},
    ).status_code == 200
    after_manual_edit = app_client.get("/api/academic/profile?include_gpa=true").json()
    monkeypatch.setattr(provider, "update_profile", fail_update)
    # Neither a duplicate B request nor a stale A request may rewrite the mirror
    # after a valid manual edit. A disabled write path proves no write is attempted.
    for body in (replacement, previous):
        retry = app_client.post("/api/academic/transcript/import", json=body)
        assert retry.status_code == 200
        assert retry.json()["duplicate"] is True
        assert app_client.get("/api/academic/profile?include_gpa=true").json() == after_manual_edit


def test_silently_incomplete_memory_mirror_aborts_import(app_client, monkeypatch):
    from app.memory import get_memory_manager

    user = _create_and_login(app_client)
    previous = _payload(printed_at="2026-09-03T13:25:00Z", grade="B+")
    assert app_client.post("/api/academic/transcript/import", json=previous).status_code == 200
    manager = get_memory_manager()
    memory_before = manager.get_profile(user["id"])
    before = app_client.get("/api/academic/profile?include_gpa=true").json()
    replacement = _passing_snapshot([("MATH", "2B")], printed_at="2026-01-07T13:00:00Z", gpa=3.3)
    monkeypatch.setattr(manager, "update_profile", lambda *args, **kwargs: memory_before)

    assert app_client.post("/api/academic/transcript/import", json=replacement).status_code == 503
    assert app_client.get("/api/academic/profile?include_gpa=true").json() == before
    assert manager.get_profile(user["id"]) == memory_before


def test_unavailable_memory_provider_aborts_import(app_client, monkeypatch):
    from app.academic import store
    from app.memory import get_memory_manager

    user = _create_and_login(app_client)
    previous = _payload(printed_at="2026-09-03T13:25:00Z", grade="B+")
    assert app_client.post("/api/academic/transcript/import", json=previous).status_code == 200
    ai_before = store.get_ai_academic_context(user["id"])
    replacement = _passing_snapshot([("MATH", "2B")], printed_at="2026-01-07T13:00:00Z", gpa=3.3)
    monkeypatch.setattr(get_memory_manager(), "_provider", None)

    assert app_client.post("/api/academic/transcript/import", json=replacement).status_code == 503
    assert store.get_ai_academic_context(user["id"]) == ai_before


def test_replaying_older_upload_request_does_not_replace_newer_snapshot_or_manual_edits(app_client):
    from app.academic import store
    from app.memory import get_memory_manager

    user = _create_and_login(app_client)
    previous = _payload(printed_at="2026-09-03T13:25:00Z", grade="B+")
    previous["client_request_id"] = "request-student-a"
    replacement = _passing_snapshot([("MATH", "2B")], printed_at="2026-01-07T13:00:00Z", gpa=3.3)
    replacement["client_request_id"] = "request-student-b"
    assert app_client.post("/api/academic/transcript/import", json=previous).status_code == 200
    assert app_client.post("/api/academic/transcript/import", json=replacement).status_code == 200
    assert app_client.post(
        f"/api/memory/{user['id']}/profile",
        json={"completed_courses": ["MATH 2B", "I&C SCI 31"]},
    ).status_code == 200
    before = app_client.get("/api/academic/profile?include_gpa=true").json()

    retry = app_client.post("/api/academic/transcript/import", json=previous)

    assert retry.status_code == 200
    assert retry.json()["duplicate"] is True
    assert app_client.get("/api/academic/profile?include_gpa=true").json() == before
    assert set(get_memory_manager().get_profile(user["id"])["completed_courses"]) == {"MATH 2B", "I&C SCI 31"}
    assert store.get_ai_academic_context(user["id"])["uc_gpa"] == 3.3
    with store._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transcript_imports WHERE user_id = ?", (user["id"],)).fetchone()[0] == 2


def test_replacing_one_account_snapshot_does_not_change_other_account(app_client):
    from app.auth import security
    from app.academic import store
    from app.memory import get_memory_manager

    first_user = _create_and_login(app_client)
    previous = _payload(printed_at="2026-09-03T13:25:00Z", grade="B+")
    assert app_client.post("/api/academic/transcript/import", json=previous).status_code == 200
    first_context = store.get_ai_academic_context(first_user["id"])
    first_memory = get_memory_manager().get_profile(first_user["id"])
    first_profile = app_client.get("/api/academic/profile?include_gpa=true").json()
    _create_and_login(app_client, "other-student@uci.edu")
    assert app_client.post("/api/academic/transcript/import", json=previous).status_code == 200
    replacement = _passing_snapshot([("MATH", "2B")], printed_at="2026-01-07T13:00:00Z", gpa=3.3)
    assert app_client.post("/api/academic/transcript/import", json=replacement).status_code == 200

    app_client.cookies.set(security.SESSION_COOKIE_NAME, security.sign_session(first_user["id"]))
    assert app_client.get("/api/academic/profile?include_gpa=true").json() == first_profile
    assert store.get_ai_academic_context(first_user["id"]) == first_context
    assert get_memory_manager().get_profile(first_user["id"]) == first_memory


def test_overlapping_imports_keep_sqlite_and_memory_snapshots_in_upload_order(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock

    from fastapi import Request
    from app.academic import store
    from app.academic.models import TranscriptImportRequest
    from app.memory import get_memory_manager
    from app.routers import academic

    user = {"id": "synthetic-concurrent-student"}
    first = _passing_snapshot([("I&C SCI", "32")], printed_at="2026-09-03T13:25:00Z", gpa=4.0)
    first["client_request_id"] = "concurrent-upload-a"
    second = _passing_snapshot([("MATH", "2B")], printed_at="2026-01-07T13:00:00Z", gpa=3.3)
    second["client_request_id"] = "concurrent-upload-b"
    first_committed = Event()
    release_first_import = Event()
    first_memory_pending = Event()
    release_first_memory = Event()
    second_lock_attempted = Event()
    second_import_started = Event()
    bucket = hash(user["id"]) % len(academic._TRANSCRIPT_LOCKS)
    real_lock = academic._TRANSCRIPT_LOCKS[bucket]
    entry_counter_lock = Lock()
    entry_count = 0
    second_was_blocked = False

    class ObservedLock:
        def __enter__(self):
            nonlocal entry_count, second_was_blocked
            with entry_counter_lock:
                entry_count += 1
                second_entry = entry_count == 2
            if second_entry:
                acquired = real_lock.acquire(blocking=False)
                second_was_blocked = not acquired
                if acquired:
                    real_lock.release()
                second_lock_attempted.set()
            real_lock.acquire()
            return self

        def __exit__(self, *args):
            real_lock.release()

    locks = list(academic._TRANSCRIPT_LOCKS)
    locks[bucket] = ObservedLock()
    monkeypatch.setattr(academic, "_TRANSCRIPT_LOCKS", tuple(locks))
    monkeypatch.setattr(academic, "check_rate_limit", lambda *args: None)
    actual_import = academic.import_transcript

    def observed_import(user_id, body, **kwargs):
        if body.client_request_id == "concurrent-upload-b":
            second_import_started.set()
        result = actual_import(user_id, body, **kwargs)
        if body.client_request_id == "concurrent-upload-a":
            first_committed.set()
            assert release_first_import.wait(2), "First import was not released"
        return result

    manager = get_memory_manager()

    class ObservedMemory:
        def update_profile(self, user_id, updates, **kwargs):
            if updates["completed_courses"] == ["I&C SCI 32"]:
                first_memory_pending.set()
                assert release_first_memory.wait(2), "First memory update was not released"
            return manager.update_profile(user_id, updates, **kwargs)

    monkeypatch.setattr(academic, "import_transcript", observed_import)
    monkeypatch.setattr(academic, "get_memory_manager", lambda: ObservedMemory())
    request = Request({"type": "http", "method": "POST", "path": "/api/academic/transcript/import"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            first_result = pool.submit(academic.import_transcript_data, TranscriptImportRequest(**first), request, user)
            assert first_memory_pending.wait(2), "First request did not reach memory synchronization"
            assert not first_committed.is_set()
            second_result = pool.submit(academic.import_transcript_data, TranscriptImportRequest(**second), request, user)
            assert second_lock_attempted.wait(2), "Second request did not attempt the account lock"
            assert second_was_blocked is True
            assert not second_import_started.is_set()
            acquired_during_memory_sync = real_lock.acquire(blocking=False)
            if acquired_during_memory_sync:
                real_lock.release()
            assert acquired_during_memory_sync is False
            assert not second_import_started.is_set()
            release_first_memory.set()
            assert first_committed.wait(2), "First SQLite snapshot was not committed"
            assert not second_import_started.is_set()
            release_first_import.set()
            assert first_result.result(timeout=2)["completed_course_ids"] == ["I&C SCI 32"]
            assert second_result.result(timeout=2)["completed_course_ids"] == ["MATH 2B"]
        finally:
            release_first_import.set()
            release_first_memory.set()

    assert manager.get_profile(user["id"])["completed_courses"] == ["MATH 2B"]
    assert store.get_ai_academic_context(user["id"]) == {
        "courses": [{"course_id": "MATH 2B", "effective_grade": "B+", "units": 4}],
        "uc_gpa": 3.3,
    }


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
