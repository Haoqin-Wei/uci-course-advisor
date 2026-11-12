from __future__ import annotations

import dataclasses
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from app.academic import store as academic_store
from app.catalog import course_source, course_store
from app.catalog.cache import get_catalog
from app.catalog.course_sync import course_row_from_api, sync_courses
from app.catalog.departments import resolve_department
from app.catalog.loaders.catalog_store import CatalogStoreLoader
from app.catalog.loaders.uci_relational import UCIRelationalLoader
from app.catalog.sync import sync_term
from app.catalog.term import Term
from app.catalog.types import CourseRef
from app.data import db as data_db
from app.data.uci_general import anteater_programs

from tests.test_course_catalog_store import api_course, page

WINTER = Term(2027, "Winter")


def one_page(*courses):
    return lambda cursor: page(list(courses))


def test_csv_is_used_until_a_catalogue_is_synced_then_the_store_wins():
    assert course_source.course_rows_token() is None and course_source.source_label() == "local_csv"
    csv_rows = course_source.course_rows()
    assert len(csv_rows) > 1000 and course_source.course_rows() is csv_rows      # cached, not re-read

    sync_courses(fetch_page=one_page(api_course(title="Synced title")))

    rows = course_source.course_rows()
    assert [(r["department"], r["course_number"], r["title"]) for r in rows] == [("COMPSCI", "161", "Synced title")]
    assert course_source.source_label() == "catalog_store"
    assert course_source.course_rows() is rows

    sync_courses(fetch_page=one_page(api_course(title="Second sync")), force=True)
    assert course_source.course_rows()[0]["title"] == "Second sync"             # no restart needed


def test_store_backed_course_rows_build_the_same_records_as_the_csv(catalog_fixture_dir, tmp_path):
    db = tmp_path / "catalog.db"
    term = Term(2025, "Spring")
    expected_courses, expected_sections = UCIRelationalLoader(catalog_fixture_dir).load(term)
    csv_rows = course_source.course_rows(csv_path=catalog_fixture_dir / "courses.csv", db_path=db)
    assert csv_rows
    course_store.replace_courses(csv_rows, source="csv_copy", path=db)
    course_source.clear_cache()

    loader = CatalogStoreLoader(catalog_fixture_dir, db_path=db)
    assert loader.snapshot_token(term) is not None        # the view must follow the course snapshot
    courses, sections = loader.load(term)
    assert courses == expected_courses and sections == expected_sections
    assert loader.available_terms() == UCIRelationalLoader(catalog_fixture_dir).available_terms()


def test_synced_catalogue_supplies_metadata_for_courses_the_csv_never_had():
    new_course = api_course(
        "NEWDEPT", "7", title="Brand New Course", courseNumeric=7, terms=["2027 Winter"],
        geList=["GE III: Social & Behavioral Sciences"], prerequisiteText="NEWDEPT 1",
        prerequisites=[{"id": "NEWDEPT1", "department": "NEWDEPT", "courseNumber": "1", "title": "Intro"}],
        corequisites="NEWDEPT 7L", description="A course that postdates the CSV.")
    assert resolve_department("NEWDEPT") is None
    assert get_catalog(WINTER) is None

    sync_courses(fetch_page=one_page(new_course, api_course()))

    assert resolve_department("newdept") == "NEWDEPT"
    view = get_catalog(WINTER)                              # listed for the term by the catalogue itself
    record = view.get_course(CourseRef("NEWDEPT", "7"))
    assert (record.title, record.description) == ("Brand New Course", "A course that postdates the CSV.")
    assert record.prerequisite_text == "NEWDEPT 1" and record.prerequisites == (CourseRef("NEWDEPT", "1"),)
    assert record.ge_categories and record.course_numeric == 7
    assert view.get_sections(CourseRef("NEWDEPT", "7")) == []         # no sections were synced

    info = data_db.get_course_info("NEWDEPT 7")
    assert info["found"] and info["source"] == "db" and info["course"]["title"] == "Brand New Course"
    pool = data_db.retrieve_course_candidates("Winter 2027")
    assert {c["course_id"] for c in pool["courses"]} == {"NEWDEPT 7", "COMPSCI 161"}


def test_course_sync_refreshes_views_built_from_synced_sections():
    from tests.test_catalog_store_loader import fetcher, raw, websoc

    sync_term("2027_Winter", fetcher=fetcher(websoc({("ANTHRO", "2D", "LISTING TITLE"): [raw("60500")]})))
    before = get_catalog(WINTER).get_course(CourseRef("ANTHRO", "2D"))
    stale_ge = before.ge_categories

    sync_courses(fetch_page=one_page(api_course(
        "ANTHRO", "2D", title="Catalogue Title", geList=["GE VIII: International/Global Issues"],
        prerequisiteText="", prerequisites=[], terms=["2027 Winter"])))

    after = get_catalog(WINTER).get_course(CourseRef("ANTHRO", "2D"))
    assert after.title == "Catalogue Title" and after.ge_categories != stale_ge
    assert [s.section_code for s in get_catalog(WINTER).get_sections(CourseRef("ANTHRO", "2D"))] == ["60500"]


def test_transcript_titles_and_profile_picker_follow_the_synced_catalogue():
    assert anteater_programs.list_all_courses() and anteater_programs.all_courses_source() == "local_csv"
    csv_title = academic_store._catalog_titles().get("COMPSCI 161")
    assert csv_title

    sync_courses(fetch_page=one_page(api_course(title="Algorithms, renamed"), api_course("I&C SCI", "31", title="Intro")))

    assert academic_store._catalog_titles()["COMPSCI 161"] == "Algorithms, renamed"
    picker = anteater_programs.list_all_courses()
    assert anteater_programs.all_courses_source() == "catalog_store"
    assert {c["title"] for c in picker} == {"Algorithms, renamed", "Intro"}
    assert all(set(c) == {"id", "department", "courseNumber", "courseNumeric", "title",
                          "courseLevel", "minUnits", "maxUnits"} for c in picker)

    sync_courses(fetch_page=one_page(api_course(title="Third name")), force=True)
    assert [c["title"] for c in anteater_programs.list_all_courses()] == ["Third name"]


def test_cached_course_info_does_not_outlive_the_catalogue():
    first = data_db.get_course_info("COMPSCI 161")["course"]["title"]
    sync_courses(fetch_page=one_page(api_course(title="Renamed After Sync", terms=["2027 Winter"])))
    assert data_db.get_course_info("COMPSCI 161")["course"]["title"] == "Renamed After Sync" != first
