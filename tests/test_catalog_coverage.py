from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.catalog.coverage import _coverage_status, get_coverage_manifest, get_term_coverage
from app.catalog.term import Term
from app.data import db


def test_coverage_manifest_marks_fall_2026_partial():
    manifest = get_coverage_manifest()
    fall_2026 = next(term for term in manifest["terms"] if term["name"] == "Fall 2026")

    assert manifest["schema_version"] == "uci-relational-v1"
    assert fall_2026["coverage_status"] == "partial"
    assert fall_2026["section_count"] == 452
    assert fall_2026["course_count"] > 0
    assert fall_2026["department_count"] > 0


def test_term_without_section_data_is_unavailable():
    coverage = get_term_coverage(Term(year=2099, quarter="Winter"))

    assert coverage["coverage_status"] == "unavailable"
    assert coverage["section_count"] == 0


def official_result(sections=()):
    return {
        "ok": True, "found": bool(sections), "authoritative": True,
        "offering_status": "offered" if sections else "not_offered",
        "source_url": db.websoc_workflow.WEBSOC_URL,
        "retrieved_at": "2026-09-18T12:00:00Z",
        "sections": list(sections),
    }


def test_many_rows_from_one_department_do_not_prove_complete_coverage(tmp_path):
    with (tmp_path / "sections.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["term_id", "department", "course_id"])
        writer.writeheader()
        writer.writerows({"term_id": "2026_Fall", "department": "COMPSCI",
                         "course_id": "COMPSCI 122A"} for _ in range(1500))
    coverage = get_term_coverage(Term(2026, "Fall"), tmp_path)
    assert coverage["section_count"] == 1500
    assert coverage["departments"] == ["COMPSCI"]
    assert coverage["coverage_status"] == "partial"


def test_small_snapshots_can_also_be_stale():
    old = (datetime.now(timezone.utc) - timedelta(days=366)).isoformat()
    assert _coverage_status(10, old) == "stale"


def test_missing_eecs_in_compsci_only_snapshot_queries_official(monkeypatch):
    section = {"section_code": "TEST70A", "section_type": "Lec", "instructors": ["Test Professor"]}
    official = Mock(return_value=official_result([section]))
    secondary = Mock()
    monkeypatch.setattr(db.websoc_workflow, "fetch_websoc_course_offering", official)
    monkeypatch.setattr(db.anteater, "fetch_sections", secondary)

    result = db.get_sections("EECS70A", "Fall 2026")

    assert result["data_coverage"]["departments"] == ["COMPSCI"]
    assert result["data_coverage"]["course_count"] == 51
    official.assert_called_once_with(term="Fall 2026", department="EECS", course_number="70A")
    secondary.assert_not_called()
    assert result["source"] == "registrar_websoc"
    assert result["offering_status"] == "offered"
    assert result["sections"] == [section]


@pytest.mark.parametrize("coverage", ["partial", "stale", "complete", "unavailable"])
def test_every_local_miss_uses_official_no_match_and_stops(monkeypatch, coverage):
    monkeypatch.setattr(db, "get_term_coverage", lambda _term: {"coverage_status": coverage})
    official = Mock(return_value=official_result())
    secondary = Mock()
    monkeypatch.setattr(db.websoc_workflow, "fetch_websoc_course_offering", official)
    monkeypatch.setattr(db.anteater, "fetch_sections", secondary)

    result = db.get_sections("COMPSCI 999", "Spring 2026")

    official.assert_called_once()
    secondary.assert_not_called()
    assert result["found"] is False
    assert result["source"] == "registrar_websoc"
    assert result["offering_status"] == "not_offered"
    assert result["authoritative"] is True


def test_local_positive_hit_avoids_network_but_stale_hit_is_refreshed(monkeypatch, minimal_catalog):
    catalog = Mock()
    catalog.get_sections.side_effect = lambda ref: [s for s in minimal_catalog.sections if s.course == ref]
    monkeypatch.setattr(db, "get_catalog", lambda _term: catalog)
    official = Mock(return_value=official_result())
    secondary = Mock()
    monkeypatch.setattr(db.websoc_workflow, "fetch_websoc_course_offering", official)
    monkeypatch.setattr(db.anteater, "fetch_sections", secondary)
    monkeypatch.setattr(db, "get_term_coverage", lambda _term: {"coverage_status": "partial"})

    result = db.get_sections("COMPSCI 161", "Spring 2025")

    assert result["source"] == "db"
    assert result["offering_status"] == "offered"
    official.assert_not_called()
    monkeypatch.setattr(db, "get_term_coverage", lambda _term: {"coverage_status": "stale"})
    refreshed = db.get_sections("COMPSCI 161", "Spring 2025")
    official.assert_called_once()
    secondary.assert_not_called()
    assert refreshed["source"] == "registrar_websoc"
    assert refreshed["offering_status"] == "not_offered"


def test_official_failure_uses_secondary_and_preserves_provenance(monkeypatch):
    official = Mock(side_effect=TimeoutError("official request timed out"))
    secondary = Mock(return_value=[{"sectionCode": "TEST70A", "sectionType": "Lec",
                                    "instructors": ["Test Professor"]}])
    monkeypatch.setattr(db.websoc_workflow, "fetch_websoc_course_offering", official)
    monkeypatch.setattr(db.anteater, "fetch_sections", secondary)

    result = db.get_sections("EECS70A", "Fall 2026")

    official.assert_called_once()
    secondary.assert_called_once_with(department="EECS", course_number="70A", year="2026", quarter="Fall")
    assert result["source"] == "api"
    assert result["offering_status"] == "offered"
    assert result["source_url"] == db.anteater.WEBSOC_URL
    assert result["checked_at"]
    assert result["sections"][0]["instructors"] == ["Test Professor"]
    assert result["lookup_attempts"][0]["error_code"] == "TimeoutError"


@pytest.mark.parametrize("secondary_result", [[], None, ConnectionError("secondary offline")])
@pytest.mark.parametrize("coverage", ["partial", "stale", "complete", "unavailable"])
def test_failed_fallback_never_claims_no_offering(monkeypatch, secondary_result, coverage):
    monkeypatch.setattr(db, "get_term_coverage", lambda _term: {"coverage_status": coverage})
    official = Mock(return_value={"ok": False, "offering_status": "unavailable",
                                 "error_code": "websoc_course_result_missing",
                                 "message": "Could not parse course results"})
    secondary = (Mock(side_effect=secondary_result) if isinstance(secondary_result, Exception)
                 else Mock(return_value=secondary_result))
    monkeypatch.setattr(db.websoc_workflow, "fetch_websoc_course_offering", official)
    monkeypatch.setattr(db.anteater, "fetch_sections", secondary)

    result = db.get_sections("EECS70A", "Fall 2026")

    official.assert_called_once()
    secondary.assert_called_once()
    assert result["found"] is False
    assert result["offering_status"] == "unavailable"
    assert result["authoritative"] is False
    assert "cannot confirm" in result["reason"]
    assert "no sections published" not in result["reason"]
    assert result["lookup_attempts"][0]["error_code"] == "websoc_course_result_missing"
    assert result["lookup_attempts"][1]["error_code"]


def test_official_term_absence_preserves_publication_evidence(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        db.websoc_workflow,
        "fetch_websoc_course_offering",
        lambda **_kwargs: {
            "ok": False,
            "found": False,
            "offering_status": "unavailable",
            "error_code": "websoc_term_unavailable",
            "message": "term unavailable",
            "source_url": db.websoc_workflow.WEBSOC_URL,
            "retrieved_at": "2026-08-14T00:00:00Z",
        },
    )
    monkeypatch.setattr(db.anteater, "fetch_sections", lambda *_args, **_kwargs: None)

    result = db.get_sections("COMPSCI 999", "Winter 2099")

    assert result["found"] is False
    assert result["source"] == "registrar_websoc"
    assert result["coverage_status"] == "unavailable"
    assert result["reason"] == "term unavailable"
    assert result["error_code"] == "websoc_term_unavailable"


def test_unavailable_local_term_uses_definitive_registrar_no_match(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        db.websoc_workflow,
        "fetch_websoc_course_offering",
        lambda **_kwargs: {
            "ok": True,
            "found": False,
            "authoritative": True,
            "offering_status": "not_offered",
            "source_url": db.websoc_workflow.WEBSOC_URL,
            "retrieved_at": "2026-08-14T00:00:00Z",
            "workflow_id": "websoc_course_offering",
            "sections": [],
            "reason": "Registrar WebSoc reported no matching sections",
        },
    )

    def unexpected_anteater(*_args, **_kwargs):
        raise AssertionError("definitive Registrar result must stop secondary lookup")

    monkeypatch.setattr(db.anteater, "fetch_sections", unexpected_anteater)

    result = db.get_sections("COMPSCI 999", "Winter 2099")

    assert result["ok"] is True
    assert result["found"] is False
    assert result["source"] == "registrar_websoc"
    assert result["offering_status"] == "not_offered"
    assert result["authoritative"] is True


def test_unavailable_local_term_uses_registrar_sections(
    monkeypatch: pytest.MonkeyPatch,
):
    section = {"section_code": "62505", "section_num": "A"}
    monkeypatch.setattr(
        db.websoc_workflow,
        "fetch_websoc_course_offering",
        lambda **_kwargs: {
            "ok": True,
            "found": True,
            "authoritative": True,
            "offering_status": "offered",
            "source_url": db.websoc_workflow.WEBSOC_URL,
            "retrieved_at": "2026-08-14T00:00:00Z",
            "workflow_id": "websoc_course_offering",
            "sections": [section],
        },
    )

    result = db.get_sections("COMPSCI 999", "Winter 2099")

    assert result["found"] is True
    assert result["source"] == "registrar_websoc"
    assert result["offering_status"] == "offered"
    assert result["sections"] == [section]
