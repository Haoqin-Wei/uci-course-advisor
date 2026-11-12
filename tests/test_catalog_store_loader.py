from __future__ import annotations

import dataclasses
import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from app.catalog import store
from app.catalog.cache import CatalogCache, get_catalog, get_catalog_cache
from app.catalog.coverage import get_term_coverage
from app.catalog.loaders.catalog_store import CatalogStoreLoader
from app.catalog.loaders.uci_relational import UCIRelationalLoader
from app.catalog.sync import sync_term
from app.catalog.term import Term
from app.catalog.types import CourseRef
from app.data import db as data_db

REPO_ROOT = Path(__file__).resolve().parents[1]
WINTER = Term(2027, "Winter")


def _import_script():
    path = REPO_ROOT / "scripts" / "import_catalog_csv_to_sqlite.py"
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location("import_catalog_csv_to_sqlite", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(path.parent))


def comments(dept="COMPSCI", title="DES&ANALYS OF ALGOR"):
    return {
        "school": {"name": "School", "comment": "school note " * 50},
        "department": {"code": dept, "name": dept, "comment": "dept note",
                       "course_number_range_comments": ["range"], "section_code_range_comments": []},
        "course": {"course_id": "X", "title": title, "comment": ""},
    }


def raw(code, kind="Lec", **extra):
    return {
        "sectionCode": code, "sectionType": kind, "sectionNum": "A", "units": "4", "status": "OPEN",
        "instructors": ["DILLENCOURT, M."], "maxCapacity": "165", "numWaitlistCap": "25",
        "numOnWaitlist": "0", "numRequested": "0", "restrictions": "A", "sectionComment": "",
        "numCurrentlyEnrolled": {"totalEnrolled": "12", "sectionEnrolled": ""},
        "isCancelled": False, "finalExam": {"examStatus": "NO_FINAL"},
        "meetings": [{"timeIsTBA": False, "bldg": ["SSPA 1100"], "days": "MWF",
                      "startTime": {"hour": 15, "minute": 0}, "endTime": {"hour": 15, "minute": 50}}],
        **extra,
    }


def websoc(courses):
    return {"schools": [{"schoolName": "School", "schoolComment": "s", "departments": [
        {"deptCode": dept, "deptName": dept, "deptComment": "d", "courses": [
            {"deptCode": dept, "courseNumber": number, "courseTitle": title, "courseComment": "",
             "sections": sections}]}
        for (dept, number, title), sections in courses.items()]}]}


def fetcher(data):
    return lambda term: SimpleNamespace(ok=True, status="ok", data=data, error=None)


# ── store: shared parent comments, schema reset, read-only probes ──

def test_parent_comments_are_stored_once_and_reattached(tmp_path):
    db = tmp_path / "catalog.db"
    store.replace_term_sections("2027_Winter", [
        {"section_code": "1", "department": "COMPSCI", "course_number": "161", "comments": comments()},
        {"section_code": "2", "department": "COMPSCI", "course_number": "171",
         "comments": comments(title="INTRO ARTIFCL INTEL")},
        {"section_code": "3", "department": "MATH", "course_number": "2A", "comments": comments("MATH", "CALCULUS")},
        {"section_code": "4", "department": "MATH", "course_number": "2B"},
        {"section_code": "5", "department": "MATH", "course_number": "2D", "comments_json": '{"school": null}'},
    ], source="test", path=db)

    one, two, three, four, five = store.get_sections("2027_Winter", path=db)
    assert one["comments"] == comments() and list(one["comments"]) == ["school", "department", "course"]
    assert two["comments"]["course"]["title"] == "INTRO ARTIFCL INTEL"
    assert three["comments"] == comments("MATH", "CALCULUS")
    assert four["comments"] is None                       # absent stays absent
    assert five["comments"] == {"school": None}           # no invented "course" entry
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM comment_scopes").fetchone()[0] == 3

    store.replace_term_sections("2027_Winter", [
        {"section_code": "9", "department": "MATH", "course_number": "2A"}], source="test", force=True, path=db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM comment_scopes").fetchone()[0] == 0
    with pytest.raises(ValueError):
        store.replace_term_sections("2027_Winter", [
            {"section_code": "1", "department": "M", "course_number": "1", "comments": ["x"]}],
            source="test", force=True, path=db)


def test_older_schema_is_reset_not_misread(tmp_path):
    db = tmp_path / "catalog.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE sections (term_id TEXT, section_code TEXT, comments_json TEXT);"
            "CREATE TABLE term_snapshots (term_id TEXT PRIMARY KEY, section_count INTEGER);"
            "INSERT INTO term_snapshots VALUES ('2027_Winter', 5); PRAGMA user_version = 1;")
    assert store.snapshot_token("2027_Winter", path=db) is None      # read-only probe: no reset
    assert store.stored_term_ids(path=db) == []
    assert store.list_term_snapshots(path=db) == []                  # first real open resets
    store.replace_term_sections("2027_Winter", [
        {"section_code": "1", "department": "MATH", "course_number": "2A"}], source="test", path=db)
    assert store.stored_term_ids(path=db) == ["2027_Winter"]


def test_read_only_probes_never_create_the_database(tmp_path):
    db = tmp_path / "missing" / "catalog.db"
    assert store.snapshot_token("2027_Winter", path=db) is None
    assert store.stored_term_ids(path=db) == []
    assert not db.parent.exists()

    store.mark_term_unpublished("2027_Winter", source="probe", path=tmp_path / "c.db")
    assert store.snapshot_token("2027_Winter", path=tmp_path / "c.db") is None   # no sections held
    store.replace_term_sections("2027_Winter", [
        {"section_code": "1", "department": "MATH", "course_number": "2A"}], source="t", path=tmp_path / "c.db")
    status, count, synced = store.snapshot_token("2027 Winter", path=tmp_path / "c.db")
    assert (status, count) == ("complete", 1) and synced


# ── loader ──

def test_store_backed_term_equals_csv_loader_output(catalog_fixture_dir, tmp_path):
    db = tmp_path / "catalog.db"
    term = Term(2025, "Spring")
    csv_courses, csv_sections = UCIRelationalLoader(catalog_fixture_dir).load(term)
    assert csv_sections
    for term_id, rows in _import_script().build_term_rows(catalog_fixture_dir).items():
        store.replace_term_sections(term_id, rows, source="csv_import", status="partial", path=db)

    loader = CatalogStoreLoader(catalog_fixture_dir, db_path=db)
    courses, sections = loader.load(term)

    bare = lambda record: dataclasses.replace(record, provenance=None)
    assert sorted(map(bare, sections), key=lambda s: s.section_id) == sorted(
        map(bare, csv_sections), key=lambda s: s.section_id)
    assert [bare(c) for c in courses] == [bare(c) for c in csv_courses]
    assert {s.provenance.loader for s in sections} == {"catalog_store"}
    assert sections[0].provenance.source_file == str(db)
    assert term in loader.available_terms()


def test_loader_without_snapshot_is_the_csv_loader(catalog_fixture_dir, tmp_path):
    term = Term(2025, "Spring")
    loader = CatalogStoreLoader(catalog_fixture_dir, db_path=tmp_path / "none.db")
    assert loader.snapshot_token(term) is None
    courses, sections = loader.load(term)
    expected_courses, expected_sections = UCIRelationalLoader(catalog_fixture_dir).load(term)
    assert (courses, sections) == (expected_courses, expected_sections)
    assert {s.provenance.loader for s in sections} == {"uci_relational"}
    assert loader.available_terms() == UCIRelationalLoader(catalog_fixture_dir).available_terms()
    assert not (tmp_path / "none.db").exists()


def test_synced_term_missing_from_csv_gets_sections_and_listing_title(catalog_fixture_dir, tmp_path):
    db = tmp_path / "catalog.db"
    sync_term("2027_Winter", fetcher=fetcher(websoc({
        ("NEWDEPT", "7", "BRAND NEW COURSE"): [raw("50000"), raw("50001", "Dis", sectionNum="A1")],
    })), path=db)
    loader = CatalogStoreLoader(catalog_fixture_dir, db_path=db)
    assert WINTER in loader.available_terms()
    courses, sections = loader.load(WINTER)

    assert [c.ref for c in courses] == [CourseRef("NEWDEPT", "7")]
    assert courses[0].title == "BRAND NEW COURSE"
    assert courses[0].prerequisite_text is None and courses[0].course_numeric == 7   # unknown stays unknown
    lecture = next(s for s in sections if s.section_code == "50000")
    assert (lecture.days, lecture.start_time, lecture.end_time) == ("MWF", "15:00", "15:50")
    assert lecture.instructors == ("DILLENCOURT, M.",) and lecture.max_capacity == 165
    assert lecture.section_enrolled == 12 and lecture.is_cancelled is False
    assert lecture.meetings[0]["days"] == "MWF" and lecture.comments["course"]["title"] == "BRAND NEW COURSE"
    assert lecture.section_comment == ""


# ── cache, coverage and the data layer on the per-test default store ──

def test_default_store_is_isolated_and_empty_in_tests(isolated_test_environment):
    assert config.catalog_db_path().parent == isolated_test_environment.root
    assert store.stored_term_ids() == []
    assert get_catalog(WINTER) is None
    assert get_term_coverage(WINTER)["coverage_status"] == "unavailable"


def test_sync_makes_a_new_term_visible_to_the_data_layer():
    assert data_db.retrieve_course_candidates("Winter 2027")["found"] is False

    sync_term("2027_Winter", fetcher=fetcher(websoc({
        ("COMPSCI", "161", "DES&ANALYS OF ALGOR"): [raw("34220")],
        ("MATH", "2B", "CALCULUS"): [raw("44100", instructors=["STAFF"])],
    })))

    pool = data_db.retrieve_course_candidates("Winter 2027")
    assert pool["found"] and [c["course_id"] for c in pool["courses"]] == ["COMPSCI 161", "MATH 2B"]
    assert data_db.retrieve_course_candidates("Winter 2027", department="MATH")["total_found"] == 1
    view = get_catalog(WINTER)
    assert view.get_sections(CourseRef("MATH", "2B"))[0].instructors == ()
    assert view.get_course(CourseRef("COMPSCI", "161")).prerequisite_text   # catalogue metadata still joined
    coverage = get_term_coverage(WINTER)
    assert (coverage["coverage_status"], coverage["section_count"]) == ("complete", 2)
    assert coverage["source"] == "catalog_store:anteater_websoc"

    # A later sync is served without a restart.
    sync_term("2027_Winter", fetcher=fetcher(websoc({
        ("COMPSCI", "161", "DES&ANALYS OF ALGOR"): [raw("34220", instructors=["SHINDLER, M."])],
        ("MATH", "2B", "CALCULUS"): [raw("44100")],
    })))
    assert get_catalog(WINTER).get_sections(CourseRef("COMPSCI", "161"))[0].instructors == ("SHINDLER, M.",)


def test_synced_snapshot_supersedes_csv_sections_for_the_same_term():
    spring = Term(2026, "Spring")
    before = get_catalog(spring)
    target = next(ref for ref in sorted(before.all_course_refs(), key=CourseRef.display)
                  if before.get_sections(ref))
    assert get_term_coverage(spring)["coverage_status"] == "partial"

    sync_term("2026_Spring", fetcher=fetcher(websoc({("MATH", "2B", "CALCULUS"): [raw("44100")]})))

    after = get_catalog(spring)
    assert after is not before
    assert after.get_sections(target) == []                       # CSV rows are not mixed in
    assert [s.section_code for s in after.get_sections(CourseRef("MATH", "2B"))] == ["44100"]
    assert after.get_course(target) is not None                   # catalogue entry itself remains
    assert get_term_coverage(spring)["coverage_status"] == "complete"


def test_cache_rechecks_only_after_the_interval_and_on_invalidate(catalog_fixture_dir, tmp_path, monkeypatch):
    db = tmp_path / "catalog.db"
    cache = CatalogCache()
    cache.register_loader(CatalogStoreLoader(catalog_fixture_dir, db_path=db))
    monkeypatch.setenv("CATALOG_VERSION_CHECK_SECONDS", "3600")
    assert cache.get(WINTER) is None

    store.replace_term_sections("2027_Winter", [
        {"section_code": "1", "department": "MATH", "course_number": "2A"}], source="t", path=db)
    assert cache.get(WINTER) is None            # inside the interval: no re-read yet
    cache.invalidate(WINTER)
    first = cache.get(WINTER)
    assert first is not None and cache.get(WINTER) is first

    monkeypatch.setenv("CATALOG_VERSION_CHECK_SECONDS", "0")
    assert cache.get(WINTER) is first           # unchanged marker: same view object
    store.replace_term_sections("2027_Winter", [
        {"section_code": "1", "department": "MATH", "course_number": "2A"},
        {"section_code": "2", "department": "MATH", "course_number": "2B"}], source="t", path=db)
    second = cache.get(WINTER)
    assert second is not first and len(second.all_course_refs()) == 2
    assert get_catalog_cache() is not cache
