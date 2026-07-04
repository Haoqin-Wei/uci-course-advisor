from __future__ import annotations

import pytest

from app.catalog.coverage import get_coverage_manifest, get_term_coverage
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


def test_partial_term_empty_sections_are_cannot_confirm(
    monkeypatch: pytest.MonkeyPatch,
):
    def fail_fetch_sections(*_args, **_kwargs):
        raise AssertionError("partial local data should not be treated as API-confirmed")

    monkeypatch.setattr(db.anteater, "fetch_sections", fail_fetch_sections)

    result = db.get_sections("MATH 10", "Fall 2026")

    assert result["found"] is False
    assert result["source"] == "db"
    assert result["coverage_status"] == "partial"
    assert "cannot confirm" in result["reason"]


def test_complete_term_empty_sections_are_no_offering(
    monkeypatch: pytest.MonkeyPatch,
):
    def fail_fetch_sections(*_args, **_kwargs):
        raise AssertionError("complete local data should not require API fallback")

    monkeypatch.setattr(db.anteater, "fetch_sections", fail_fetch_sections)

    result = db.get_sections("COMPSCI 999", "Spring 2026")

    assert result["found"] is False
    assert result["source"] == "db"
    assert result["coverage_status"] == "complete"
    assert result["reason"] == "no sections published for COMPSCI 999 in Spring 2026"


def test_unavailable_term_api_failure_is_cannot_confirm(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(db.anteater, "fetch_sections", lambda *_args, **_kwargs: None)

    result = db.get_sections("COMPSCI 999", "Winter 2099")

    assert result["found"] is False
    assert result["source"] == "none"
    assert result["coverage_status"] == "unavailable"
    assert "live API could not confirm" in result["reason"]
