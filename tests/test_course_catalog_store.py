from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.catalog import course_store, store
from app.catalog.course_sync import course_row_from_api, sync_courses


def api_course(dept="COMPSCI", number="161", **extra):
    """Shape copied from a real Anteater /courses response."""
    base = {
        "id": f"{dept}{number}".replace(" ", ""), "department": dept, "courseNumber": number,
        "courseNumeric": 161, "school": "Donald Bren School of Information and Computer Sciences",
        "title": "Design and Analysis of Algorithms", "courseLevel": "Upper Division (100-199)",
        "minUnits": 4, "maxUnits": 4, "description": "Techniques for efficient algorithm design.",
        "departmentName": "Computer Science",
        "instructors": [{"name": "Amelia C Regan", "email": "someone@uci.edu", "title": "Professor",
                         "shortenedNames": ["REGAN, A.", "REGAN, A.C."]},
                        {"name": "Second Person", "shortenedNames": ["REGAN, A."]}],
        "prerequisiteTree": {"AND": [{"prereqType": "course", "coreq": False,
                                      "courseId": "I&C SCI 46", "minGrade": "C"}]},
        "prerequisiteText": "I&C SCI 46 with a minimum grade of C",
        "prerequisites": [{"id": "I&CSCI46", "department": "I&C SCI", "courseNumber": "46",
                           "title": "Data Structure Implementation and Analysis"}],
        "dependencies": [{"id": "COMPSCI134", "department": "COMPSCI", "courseNumber": "134",
                          "title": "Computer and Network Security"}],
        "repeatability": "", "repeatabilityTimes": 0, "repeatabilityType": "times",
        "gradingOption": "", "concurrent": "", "sameAs": "", "restriction": "CS majors only.",
        "overlap": "", "corequisites": "", "geList": [], "geText": "",
        "terms": ["2026 Winter", "2027 Winter"],
    }
    return {**base, **extra}


def page(items, next_cursor=None):
    return SimpleNamespace(ok=True, status="ok", error=None,
                           data={"items": items, "nextCursor": next_cursor})


def paged(pages, calls=None):
    """pages: list of item lists; cursors are the page indexes."""
    def fetch(cursor):
        if calls is not None:
            calls.append(cursor)
        index = int(cursor or 0)
        following = str(index + 1) if index + 1 < len(pages) else None
        return page(pages[index], following)
    return fetch


def bulk(count, dept="MATH"):
    return [api_course(dept, str(i + 1)) for i in range(count)]


@pytest.fixture
def db(tmp_path):
    return tmp_path / "catalog.db"


def test_api_course_becomes_a_csv_shaped_row_with_the_extra_fields():
    row = course_row_from_api(api_course(
        "PHYSICS", "7LC", corequisites="PHYSICS 7C", gradingOption="Pass/no pass only.",
        repeatability="May be taken for credit 3 times.", repeatabilityTimes=3, overlap="CHEM H2LA",
        concurrent="PHYSICS 207", sameAs="LSCI 68",
        geList=["GE II: Science and Technology", "GE Vb: Formal Reasoning"], geText="(II and VB)",
        minUnits=1.5, repeatabilityType=None, courseNumeric=7))

    assert set(row) == set(course_store.COLUMNS)
    assert all(isinstance(value, str) for value in row.values())
    assert (row["course_id"], row["department"], row["course_number"], row["course_numeric"]) == (
        "PHYSICS_7LC", "PHYSICS", "7LC", "7")
    assert (row["min_units"], row["max_units"]) == ("1.5", "4")
    assert row["ge_list"] == "GE II: Science and Technology|GE Vb: Formal Reasoning"
    assert json.loads(row["prerequisite_tree_json"])["AND"][0]["minGrade"] == "C"
    assert json.loads(row["prerequisites_flat_json"]) == [{
        "id": "I&C_SCI_46", "department": "I&C SCI", "course_number": "46",
        "title": "Data Structure Implementation and Analysis"}]
    assert json.loads(row["dependencies_flat_json"])[0]["id"] == "COMPSCI_134"
    assert json.loads(row["terms_offered_json"]) == ["2026 Winter", "2027 Winter"]
    assert json.loads(row["instructors_shortened_json"]) == ["REGAN, A.", "REGAN, A.C."]
    assert "uci.edu" not in json.dumps(row)                    # contact details are not kept
    assert (row["corequisites"], row["grading_option"], row["overlap"], row["concurrent"]) == (
        "PHYSICS 7C", "Pass/no pass only.", "CHEM H2LA", "PHYSICS 207")
    assert (row["repeatability"], row["repeatability_times"], row["repeatability_type"]) == (
        "May be taken for credit 3 times.", "3", "")
    assert row["same_as"] == "LSCI 68"


def test_store_round_trip_token_and_read_only_probes(db):
    assert course_store.course_token(path=db) is None
    assert course_store.get_course_rows(path=db) == []
    assert course_store.get_course_snapshot(path=db) is None
    assert not db.exists()                                     # probes never create the file

    result = course_store.replace_courses(
        [course_row_from_api(api_course()), course_row_from_api(api_course("soc  sci", "178c")),
         course_row_from_api(api_course(title="Second copy wins"))],
        source="test", synced_at="2026-11-11T00:00:00+00:00", path=db)
    assert (result.replaced, result.course_count, result.previous_course_count) == (True, 2, 0)

    rows = course_store.get_course_rows(path=db)
    assert [(r["department"], r["course_number"]) for r in rows] == [("COMPSCI", "161"), ("SOC SCI", "178C")]
    assert rows[0]["title"] == "Second copy wins" and rows[0]["restriction"] == "CS majors only."
    assert set(rows[0]) == set(course_store.COLUMNS)
    assert course_store.course_token(path=db) == (2, "2026-11-11T00:00:00+00:00")
    assert course_store.stored_course_departments(path=db) == ["COMPSCI", "SOC SCI"]
    snapshot = course_store.get_course_snapshot(path=db)
    assert snapshot["source"] == "test" and snapshot["last_error"] is None

    # Term sections live in the same file and are untouched.
    store.replace_term_sections("2027_Winter", [
        {"section_code": "1", "department": "MATH", "course_number": "2A"}], source="t", path=db)
    course_store.replace_courses([course_row_from_api(api_course())], source="test", force=True, path=db)
    assert len(store.get_sections("2027_Winter", path=db)) == 1
    assert len(course_store.get_course_rows(path=db)) == 1


@pytest.mark.parametrize("rows", [[], [{"department": "", "course_number": "1"}],
                                  [{"department": "MATH", "course_number": " "}]])
def test_invalid_catalogue_is_rejected_without_writing(db, rows):
    with pytest.raises(ValueError):
        course_store.replace_courses(rows, source="test", path=db)
    assert course_store.get_course_rows(path=db) == []


def test_sync_pages_through_the_whole_catalogue(db):
    calls = []
    result = sync_courses(fetch_page=paged([bulk(100), bulk(100, "CHEM"), bulk(7, "ART")], calls), path=db)
    assert calls == [None, "1", "2"]
    assert (result.outcome, result.course_count, result.previous_course_count, result.pages) == ("synced", 207, 0, 3)
    assert result.ok and len(course_store.get_course_rows(path=db)) == 207
    again = sync_courses(fetch_page=paged([bulk(100), bulk(100, "CHEM"), bulk(7, "ART")]), path=db)
    assert (again.outcome, again.course_count, again.previous_course_count) == ("synced", 207, 207)
    assert course_store.get_course_snapshot(path=db)["source"] == "anteater_courses"


def test_a_failing_page_is_retried_then_aborts_without_partial_writes(db):
    sync_courses(fetch_page=paged([bulk(50)]), path=db)
    attempts = {"second": 0}

    def flaky(cursor):
        if cursor is None:
            return page(bulk(100, "NEW"), "1")
        attempts["second"] += 1
        if attempts["second"] < 3:
            return SimpleNamespace(ok=False, status="timeout", error="read timed out", data=None)
        return page(bulk(5, "LATE"))

    recovered = sync_courses(fetch_page=flaky, retry_wait_s=0, path=db)
    assert recovered.ok and recovered.course_count == 105 and attempts["second"] == 3

    def dead(cursor):
        if cursor is None:
            return page(bulk(100, "HALF"), "1")
        raise RuntimeError("socket closed")

    failed = sync_courses(fetch_page=dead, retry_wait_s=0, path=db)
    assert failed.outcome == "failed" and not failed.ok
    assert failed.error.startswith("page 2: fetch_exception") and failed.pages == 1
    assert (failed.course_count, failed.previous_course_count) == (105, 105)
    departments = {row["department"] for row in course_store.get_course_rows(path=db)}
    assert departments == {"NEW", "LATE"}                      # the half-crawled catalogue never landed
    assert course_store.get_course_snapshot(path=db)["last_error"].startswith("page 2")


@pytest.mark.parametrize("bad, expected", [
    (SimpleNamespace(ok=True, status="ok", error=None, data=[1, 2]), "page 1: schema_error"),
    (SimpleNamespace(ok=True, status="ok", error=None, data={"items": []}), "empty_catalogue"),
    (SimpleNamespace(ok=True, status="ok", error=None, data={"items": [{"title": "no ids"}]}), "convert_error"),
])
def test_malformed_responses_fail_cleanly(db, bad, expected):
    result = sync_courses(fetch_page=lambda cursor: bad, retry_wait_s=0, path=db)
    assert result.outcome == "failed" and result.error.startswith(expected)
    assert course_store.get_course_rows(path=db) == []


def test_shrink_guard_and_runaway_cursor(db, monkeypatch):
    sync_courses(fetch_page=paged([bulk(100)]), path=db)
    rejected = sync_courses(fetch_page=paged([bulk(60)]), path=db)
    assert (rejected.outcome, rejected.course_count) == ("rejected_shrink", 100)
    assert rejected.error.startswith("rejected_shrink")
    assert len(course_store.get_course_rows(path=db)) == 100
    assert sync_courses(fetch_page=paged([bulk(60)]), force=True, path=db).course_count == 60

    from app.catalog import course_sync
    monkeypatch.setattr(course_sync, "MAX_PAGES", 3)
    endless = sync_courses(fetch_page=lambda cursor: page(bulk(1), "again"), path=db)
    assert endless.outcome == "failed" and endless.error.startswith("page_limit")
    assert len(course_store.get_course_rows(path=db)) == 60
