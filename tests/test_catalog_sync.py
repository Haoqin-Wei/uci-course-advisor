from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.catalog import store
from app.catalog.sync import sections_from_websoc, sync_term
from app.terms.models import TermKey


def raw_section(code, kind="Lec", num="A", **extra):
    """Shape copied from a real Anteater WebSoc response (2027 Winter COMPSCI 161)."""
    base = {
        "units": "4", "status": "OPEN",
        "meetings": [{"timeIsTBA": False, "bldg": ["SSPA 1100"], "days": "MWF",
                      "startTime": {"hour": 15, "minute": 0}, "endTime": {"hour": 15, "minute": 50}}],
        "finalExam": {"examStatus": "NO_FINAL"},
        "sectionNum": num, "instructors": ["DILLENCOURT, M."], "maxCapacity": "165",
        "sectionCode": code, "sectionType": kind, "numRequested": "0", "restrictions": "A",
        "numOnWaitlist": "0", "numWaitlistCap": "25", "sectionComment": "",
        "numNewOnlyReserved": "0",
        "numCurrentlyEnrolled": {"totalEnrolled": "12", "sectionEnrolled": ""},
        "isCancelled": False, "updatedAt": "2026-11-11T23:27:08.275Z", "webURL": "",
    }
    return {**base, **extra}


def payload(courses):
    """courses: {(dept, number): [sections]} → WebSoc data object."""
    departments: dict[str, list] = {}
    for (dept, number), sections in courses.items():
        departments.setdefault(dept, []).append({
            "sections": sections, "deptCode": dept, "courseNumber": number,
            "courseTitle": "TITLE", "courseId": f"{dept}{number}".replace(" ", ""),
            "courseComment": "course note",
        })
    return {"schools": [{
        "schoolName": "School", "schoolComment": "school note",
        "departments": [{"deptCode": dept, "deptName": dept.title(), "deptComment": "dept note",
                         "courseNumberRangeComments": ["range note"], "sectionCodeRangeComments": [],
                         "courses": items} for dept, items in departments.items()],
    }]}


def bulk(count, start=10000):
    return payload({("MATH", "2A"): [raw_section(str(start + i)) for i in range(count)]})


def fetcher_for(data, calls=None):
    def fetch(term):
        if calls is not None:
            calls.append(term)
        return SimpleNamespace(ok=True, status="ok", data=data, error=None)
    return fetch


@pytest.fixture
def db(tmp_path):
    return tmp_path / "catalog.db"


def test_conversion_keeps_source_evidence():
    rows = sections_from_websoc(payload({("COMPSCI", "161"): [
        raw_section("34220"),
        raw_section("34231", "Dis", "B1", units="0",
                    instructors=["STAFF", "STAFF", "PANAGEAS, I."],
                    meetings=[{"timeIsTBA": False, "bldg": ["RH 101"], "days": "MW",
                               "startTime": {"hour": 17, "minute": 0}, "endTime": {"hour": 18, "minute": 20}},
                              {"timeIsTBA": True}],
                    isCancelled="false", sectionComment=None),
        raw_section("", "Lec"),                      # no section code: skipped
        "not a section",
    ]}))
    assert [r["section_code"] for r in rows] == ["34220", "34231"]
    lec, dis = rows
    assert (lec["department"], lec["course_number"], lec["course_numeric"]) == ("COMPSCI", "161", 161)
    assert (lec["days"], lec["start_time"], lec["end_time"], lec["location"]) == ("MWF", "15:00", "15:50", "SSPA 1100")
    assert lec["time_is_tba"] is False and lec["is_cancelled"] is False
    assert (lec["waitlist_cap"], lec["total_enrolled"], lec["section_enrolled"]) == ("25", "12", "")
    assert lec["comments"]["department"]["course_number_range_comments"] == ["range note"]
    assert lec["comments"]["course"]["comment"] == "course note"
    assert dis["instructors"] == ["PANAGEAS, I."]     # STAFF is not a person
    assert len(dis["meetings"]) == 2                  # every meeting kept
    assert dis["start_time"] == "17:00"               # flat fields from the first meeting
    assert dis["is_cancelled"] is None                # the string "false" is not a boolean
    assert dis["section_comment"] == ""


def test_sync_stores_complete_term_and_is_repeatable(db):
    calls = []
    data = payload({("COMPSCI", "161"): [raw_section("34220"), raw_section("34231", "Dis", "A1")],
                    ("MATH", "2B"): [raw_section("44100")]})
    first = sync_term("2027 Winter", fetcher=fetcher_for(data, calls), path=db)
    second = sync_term("2027_Winter", fetcher=fetcher_for(data, calls), path=db)

    assert calls == [TermKey(2027, "Winter")] * 2
    assert (first.outcome, first.status, first.section_count, first.previous_section_count) == ("synced", "complete", 3, 0)
    assert (second.outcome, second.section_count, second.previous_section_count) == ("synced", 3, 3)
    assert first.ok and second.ok
    snapshot = store.get_term_snapshot("2027_Winter", path=db)
    assert (snapshot["status"], snapshot["source"], snapshot["course_count"], snapshot["department_count"]) == (
        "complete", "anteater_websoc", 2, 2)
    lecture = store.get_sections("2027_Winter", department="COMPSCI", course_number="161", path=db)[0]
    assert lecture["max_capacity"] == 165 and lecture["waitlist_cap"] == 25
    assert lecture["total_enrolled"] == 12 and lecture["section_enrolled"] is None
    assert lecture["payload_version"] == "2" and lecture["meetings"][0]["days"] == "MWF"
    assert lecture["source_updated_at"] == "2026-11-11T23:27:08.275Z"


@pytest.mark.parametrize("result, expected", [
    (SimpleNamespace(ok=False, status="timeout", data=None, error="read timed out"), "timeout"),
    (SimpleNamespace(ok=True, status="ok", data={"nothing": 1}, error=None), "schema_error"),
    (SimpleNamespace(ok=True, status="ok", data=None, error=None), "schema_error"),
])
def test_failed_fetch_keeps_existing_rows(db, result, expected):
    sync_term("2026_Fall", fetcher=fetcher_for(bulk(4)), path=db)
    outcome = sync_term("2026_Fall", fetcher=lambda term: result, path=db)

    assert outcome.outcome == "failed" and not outcome.ok
    assert outcome.error.startswith(expected)
    assert (outcome.status, outcome.section_count) == ("complete", 4)
    assert len(store.get_sections("2026_Fall", path=db)) == 4
    assert store.get_term_snapshot("2026_Fall", path=db)["last_error"].startswith(expected)


def test_fetcher_exception_is_contained_and_claims_nothing(db):
    def boom(term):
        raise RuntimeError("socket closed")
    outcome = sync_term("2027_Winter", fetcher=boom, path=db)
    assert outcome.outcome == "failed" and "socket closed" in outcome.error
    assert store.get_term_snapshot("2027_Winter", path=db)["status"] == "unknown"
    assert store.get_sections("2027_Winter", path=db) == []


def test_empty_listing_is_unpublished_only_when_nothing_is_stored(db):
    empty = fetcher_for({"schools": []})
    fresh = sync_term("2027_Spring", fetcher=empty, path=db)
    assert (fresh.outcome, fresh.status, fresh.ok) == ("unpublished", "unpublished", True)

    published = sync_term("2027_Spring", fetcher=fetcher_for(bulk(3)), path=db)
    assert (published.outcome, published.status) == ("synced", "complete")
    outage = sync_term("2027_Spring", fetcher=empty, path=db)
    assert outage.outcome == "failed" and outage.error.startswith("empty_listing")
    assert store.get_term_snapshot("2027_Spring", path=db)["status"] == "complete"
    assert len(store.get_sections("2027_Spring", path=db)) == 3


def test_shrink_is_rejected_unless_forced(db):
    sync_term("2026_Fall", fetcher=fetcher_for(bulk(100)), path=db)
    rejected = sync_term("2026_Fall", fetcher=fetcher_for(bulk(50)), path=db)
    assert (rejected.outcome, rejected.section_count, rejected.ok) == ("rejected_shrink", 100, False)
    assert rejected.error.startswith("rejected_shrink")
    assert len(store.get_sections("2026_Fall", path=db)) == 100
    forced = sync_term("2026_Fall", fetcher=fetcher_for(bulk(50)), force=True, path=db)
    assert (forced.outcome, forced.section_count) == ("synced", 50)


def test_early_listing_is_partial_against_last_years_same_quarter(db):
    sync_term("2026_Winter", fetcher=fetcher_for(bulk(100)), path=db)
    early = sync_term("2027_Winter", fetcher=fetcher_for(bulk(49)), path=db)
    assert (early.outcome, early.status) == ("synced", "partial")
    full = sync_term("2027_Winter", fetcher=fetcher_for(bulk(50)), path=db)
    assert (full.outcome, full.status) == ("synced", "complete")
    # A different quarter, or a reference that is itself not complete, proves nothing.
    assert sync_term("2027_Spring", fetcher=fetcher_for(bulk(1)), path=db).status == "complete"
    store.replace_term_sections("2026_Fall", [
        {"section_code": str(i), "department": "MATH", "course_number": "2A"} for i in range(100)
    ], source="csv_import", status="partial", path=db)
    assert sync_term("2027_Fall", fetcher=fetcher_for(bulk(10)), path=db).status == "complete"


def test_invalid_term_raises_before_fetching(db):
    calls = []
    with pytest.raises(ValueError):
        sync_term("someday", fetcher=fetcher_for(bulk(1), calls), path=db)
    assert calls == []
