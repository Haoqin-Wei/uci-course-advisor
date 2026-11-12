from __future__ import annotations

import csv
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from app import config
from app.catalog import store

REPO_ROOT = Path(__file__).resolve().parents[1]


def section(code, dept="COMPSCI", number="161", **extra):
    return {"section_code": code, "department": dept, "course_number": number, **extra}


def many(count, start=10000):
    return [section(str(start + i)) for i in range(count)]


@pytest.fixture
def db(tmp_path):
    return tmp_path / "catalog.db"


def test_default_path_is_separate_from_student_records(monkeypatch):
    monkeypatch.delenv("CATALOG_DB_PATH", raising=False)
    assert config.catalog_db_path() == Path("data/catalog.db")
    assert config.catalog_db_path() != config.academic_db_path()
    monkeypatch.setenv("CATALOG_DB_PATH", "/tmp/elsewhere.db")
    assert config.catalog_db_path() == Path("/tmp/elsewhere.db")


def test_round_trip_preserves_fields_and_unknowns(db):
    result = store.replace_term_sections("2027_Winter", [
        section(
            "34010", "compsci", "161", section_type="Lec", section_num="A", units="4",
            instructors=["SHINDLER, M.", "SHINDLER, M.", ""], ge_categories=["GE-5B"],
            days="TuTh", start_time="11:00", end_time="12:20", location="DBH 1100",
            time_is_tba="false", is_cancelled=False, status="OPEN",
            meetings=[{"days": "TuTh", "timeIsTBA": False}],
            final_exam_json='{"examStatus": "TBA_FINAL"}',
            comments={"course": "Same as X"}, section_comment="",
            max_capacity="250", total_enrolled=200, num_on_waitlist="", waitlist_cap=30,
        ),
        section("34011", "COMPSCI", "161", section_type="Dis", is_cancelled=""),
    ], source="test", path=db)

    assert (result.replaced, result.reason, result.section_count, result.previous_section_count) == (True, "ok", 2, 0)
    lec, dis = store.get_sections("2027 Winter", path=db)
    assert lec["department"] == "COMPSCI"
    assert lec["instructors"] == ["SHINDLER, M."]
    assert lec["ge_categories"] == ["GE-5B"]
    assert lec["meetings"] == [{"days": "TuTh", "timeIsTBA": False}]
    assert lec["final_exam"] == {"examStatus": "TBA_FINAL"}
    assert lec["comments"] == {"course": "Same as X"}
    assert lec["section_comment"] == ""
    assert (lec["max_capacity"], lec["total_enrolled"], lec["waitlist_cap"]) == (250, 200, 30)
    assert lec["num_on_waitlist"] is None            # blank is unknown, not zero
    assert lec["time_is_tba"] is False and lec["is_cancelled"] is False
    assert dis["is_cancelled"] is None               # unknown stays unknown
    assert dis["meetings"] is None and dis["instructors"] == []

    snapshot = store.get_term_snapshot("2027_Winter", path=db)
    assert snapshot["status"] == "complete"
    assert (snapshot["year"], snapshot["quarter"]) == (2027, "Winter")
    assert (snapshot["section_count"], snapshot["course_count"], snapshot["department_count"]) == (2, 1, 1)
    assert snapshot["last_synced_at"] and snapshot["published_detected_at"]
    assert snapshot["last_error"] is None


def test_filters_and_term_isolation(db):
    store.replace_term_sections("2026_Fall", [
        section("1", "COMPSCI", "161"), section("2", "COMPSCI", "171"), section("3", "MATH", "3A"),
    ], source="test", path=db)
    store.replace_term_sections("2027_Winter", [section("1", "MATH", "2B")], source="test", path=db)

    assert [s["section_code"] for s in store.get_sections("2026_Fall", department="compsci", path=db)] == ["1", "2"]
    assert [s["section_code"] for s in store.get_sections("2026_Fall", department="MATH", course_number="3a", path=db)] == ["3"]
    assert [s["course_number"] for s in store.get_sections("2027_Winter", path=db)] == ["2B"]
    assert store.get_sections("2026_Spring", path=db) == []
    assert store.get_term_snapshot("2026_Spring", path=db) is None
    assert [s["term_id"] for s in store.list_term_snapshots(path=db)] == ["2027_Winter", "2026_Fall"]


def test_replace_removes_rows_absent_from_new_snapshot(db):
    store.replace_term_sections("2026_Fall", many(10), source="test", path=db)
    first = store.get_term_snapshot("2026_Fall", path=db)
    result = store.replace_term_sections(
        "2026_Fall", many(9, start=10001) + [section("99999", "MATH", "2A")],
        source="test", status="partial", path=db,
    )
    assert (result.replaced, result.section_count, result.previous_section_count) == (True, 10, 10)
    codes = {s["section_code"] for s in store.get_sections("2026_Fall", path=db)}
    assert "10000" not in codes and "99999" in codes and len(codes) == 10
    snapshot = store.get_term_snapshot("2026_Fall", path=db)
    assert snapshot["status"] == "partial" and snapshot["department_count"] == 2
    assert snapshot["published_detected_at"] == first["published_detected_at"]


def test_shrink_guard_keeps_previous_rows_and_status(db):
    store.replace_term_sections("2026_Fall", many(100), source="test", path=db)
    result = store.replace_term_sections("2026_Fall", many(69), source="test", path=db)

    assert (result.replaced, result.reason, result.section_count) == (False, "rejected_shrink", 100)
    assert len(store.get_sections("2026_Fall", path=db)) == 100
    snapshot = store.get_term_snapshot("2026_Fall", path=db)
    assert snapshot["status"] == "complete" and snapshot["section_count"] == 100
    assert snapshot["last_error"].startswith("rejected_shrink")

    assert store.replace_term_sections("2026_Fall", many(70), source="test", path=db).replaced
    assert store.get_term_snapshot("2026_Fall", path=db)["last_error"] is None
    assert store.replace_term_sections("2026_Fall", many(5), source="test", force=True, path=db).replaced
    assert len(store.get_sections("2026_Fall", path=db)) == 5


def test_failed_insert_rolls_back_to_previous_snapshot(db, monkeypatch):
    store.replace_term_sections("2026_Fall", many(3), source="test", path=db)
    monkeypatch.setattr(store, "_COLUMNS", store._COLUMNS + ("no_such_column",))
    with pytest.raises(sqlite3.OperationalError):
        store.replace_term_sections("2026_Fall", many(3, start=20000), source="test", path=db)
    monkeypatch.undo()
    assert [s["section_code"] for s in store.get_sections("2026_Fall", path=db)] == ["10000", "10001", "10002"]


@pytest.mark.parametrize("rows, kwargs", [
    ([], {}),
    ([{"section_code": "1", "department": "", "course_number": "161"}], {}),
    ([section("1", meetings_json="{broken")], {}),
    ([section("1")], {"status": "unpublished"}),
    ([section("1")], {"min_retain_ratio": 2}),
])
def test_invalid_input_is_rejected_without_writing(db, rows, kwargs):
    with pytest.raises(ValueError):
        store.replace_term_sections("2026_Fall", rows, source="test", path=db, **kwargs)
    assert store.get_sections("2026_Fall", path=db) == []
    assert store.get_term_snapshot("2026_Fall", path=db) is None


def test_unknown_term_is_rejected(db):
    with pytest.raises(ValueError):
        store.replace_term_sections("not a term", [section("1")], source="test", path=db)
    with pytest.raises(ValueError):
        store.get_sections("", path=db)


def test_duplicate_section_codes_keep_last_row(db):
    result = store.replace_term_sections("2026_Fall", [
        section("1", status="OPEN"), section("1", status="FULL"),
    ], source="test", path=db)
    assert result.section_count == 1
    assert store.get_sections("2026_Fall", path=db)[0]["status"] == "FULL"


def test_unpublished_marker_never_hides_stored_sections(db):
    assert store.mark_term_unpublished("2027_Winter", source="probe", path=db) is True
    snapshot = store.get_term_snapshot("2027_Winter", path=db)
    assert snapshot["status"] == "unpublished" and snapshot["section_count"] == 0
    assert snapshot["published_detected_at"] is None and snapshot["last_synced_at"] is None

    store.replace_term_sections("2027_Winter", many(2), source="test", path=db)
    assert store.mark_term_unpublished("2027_Winter", source="probe", path=db) is False
    assert store.get_term_snapshot("2027_Winter", path=db)["status"] == "complete"
    assert len(store.get_sections("2027_Winter", path=db)) == 2


def test_sync_failure_is_recorded_without_claims(db):
    store.record_sync_failure("2027_Winter", "timeout", path=db)
    fresh = store.get_term_snapshot("2027_Winter", path=db)
    assert fresh["status"] == "unknown" and fresh["last_error"] == "timeout"

    store.replace_term_sections("2026_Fall", many(2), source="test", path=db)
    store.record_sync_failure("2026_Fall", "http 500", path=db)
    kept = store.get_term_snapshot("2026_Fall", path=db)
    assert kept["status"] == "complete" and kept["section_count"] == 2
    assert kept["last_error"] == "http 500" and kept["last_attempt_at"]


def _load_import_script():
    path = REPO_ROOT / "scripts" / "import_catalog_csv_to_sqlite.py"
    import sys
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location("import_catalog_csv_to_sqlite", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(path.parent))


def _write_csv(path, header, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


def test_csv_import_is_idempotent_and_keeps_source_time(tmp_path, db, capsys):
    data = tmp_path / "uci"
    data.mkdir()
    header = ["section_id", "term_id", "sectionCode", "department", "courseNumber", "courseNumeric",
              "sectionType", "max_capacity", "is_cancelled", "meetings_json", "updated_at"]
    _write_csv(data / "sections.csv", header, [
        {"section_id": "2026_Spring_1", "term_id": "2026_Spring", "sectionCode": "1", "department": "SOC SCI",
         "courseNumber": "178C", "courseNumeric": "178", "sectionType": "Lec", "max_capacity": "40",
         "is_cancelled": "false", "meetings_json": '[{"timeIsTBA": true}]', "updated_at": "2026-04-29T21:20:12.156Z"},
        {"section_id": "2026_Spring_2", "term_id": "2026_Spring", "sectionCode": "2", "department": "MATH",
         "courseNumber": "3A", "updated_at": "2026-04-28T00:00:00.000Z"},
        {"section_id": "2026_Fall_1", "term_id": "2026_Fall", "sectionCode": "1", "department": "COMPSCI",
         "courseNumber": "161"},
        {"section_id": "x", "term_id": "garbage", "sectionCode": "9", "department": "MATH", "courseNumber": "1"},
    ])
    _write_csv(data / "section_instructors.csv", ["section_id", "instructor_name_raw"], [
        {"section_id": "2026_Spring_1", "instructor_name_raw": "B, B."},
        {"section_id": "2026_Spring_1", "instructor_name_raw": "A, A."},
    ])
    _write_csv(data / "section_ge.csv", ["section_id", "ge_code"], [
        {"section_id": "2026_Spring_1", "ge_code": "GE-3"},
    ])
    script = _load_import_script()
    args = ["--data-dir", str(data), "--db", str(db)]

    assert script.main(args + ["--dry-run"]) == 0
    assert store.list_term_snapshots(path=db) == []
    assert script.main(args) == 0
    assert script.main(args) == 0  # second run replaces, never duplicates
    capsys.readouterr()

    spring = store.get_sections("2026_Spring", path=db)
    assert [(s["department"], s["course_number"]) for s in spring] == [("MATH", "3A"), ("SOC SCI", "178C")]
    lecture = spring[1]
    assert lecture["instructors"] == ["A, A.", "B, B."] and lecture["ge_categories"] == ["GE-3"]
    assert lecture["meetings"] == [{"timeIsTBA": True}] and lecture["max_capacity"] == 40
    assert lecture["source_updated_at"] == "2026-04-29T21:20:12.156Z"
    snapshot = store.get_term_snapshot("2026_Spring", path=db)
    assert snapshot["status"] == "partial" and snapshot["source"] == "csv_import"
    assert snapshot["last_synced_at"] == "2026-04-29T21:20:12.156Z"
    assert {s["term_id"] for s in store.list_term_snapshots(path=db)} == {"2026_Spring", "2026_Fall"}

    assert script.main(args + ["--term", "Fall 2026"]) == 0
    assert len(store.get_sections("2026_Fall", path=db)) == 1
