"""Bounded official co-class IO and policy-version cache migration, offline."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
import requests

from app.catalog.types import CourseRef, SectionRecord
from app.data import db, websoc_workflow as workflow
from app.data.websoc_pairing import POLICY_ID, POLICY_MARKER, POLICY_VERSION
from app.recommendations.pairing import discussion_policy_current


FIXTURES = Path(__file__).parent / "fixtures/websoc/pairing"


def raw(stem):
    metadata = json.loads((FIXTURES / f"{stem}.json").read_text())
    content = (FIXTURES / f"{stem}.html").read_bytes()
    return content.decode(metadata["response"]["encoding"]), metadata


def comment(html, text):
    # Synthetic course-level comment, never represented as captured evidence.
    return html.replace('<tr bgcolor="#E7E7E7" align="left">',
        f'<tr class="course-comment"><td colspan="17">{text}</td></tr>'
        '<tr bgcolor="#E7E7E7" align="left">', 1)


def parsed(stem="drama_10_2026_fall"):
    html, metadata = raw(stem)
    form = metadata["request"]["form"]
    return workflow.parse_websoc_course_html(html, term="2026 Fall", department=form["Dept"],
        course_number=form["CourseNum"], request_form=form, source_url=workflow.WEBSOC_URL,
        retrieved_at=metadata["captured_at"], html_sha256=metadata["response"]["sha256"])


class Response:
    status_code = 200
    url = workflow.WEBSOC_URL
    headers = {"content-type": "text/html"}

    def __init__(self, text):
        self.text, self.content = text, text.encode("utf-8")

    def raise_for_status(self):
        pass


class Session:
    def __init__(self, pages=None, code_page=None):
        self.pages = pages or {}
        self.code_page = code_page
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        assert url == workflow.WEBSOC_URL
        return Response(raw("search_form_discussion_policy_2026_10_20")[0])

    def post(self, url, *, data, **kwargs):
        self.calls.append(("POST", deepcopy(data)))
        assert url == workflow.WEBSOC_URL
        assert data["YearTerm"] == "2026-92"
        assert data["ClassType"] == "ALL" and data["CancelledCourses"] == "Include"
        assert data["ShowComments"] == "on"
        if data.get("CourseCodes"):
            value = self.code_page
        else:
            key = (data["Dept"], data["CourseNum"])
            stem = {("DRAMA", "10"): "drama_10_2026_fall", ("ECON", "13"): "econ_13_2026_fall"}.get(key)
            value = self.pages.get(key, raw(stem)[0] if stem else None)
        if isinstance(value, Exception):
            raise value
        assert isinstance(value, str), data
        return Response(value)


@pytest.mark.parametrize("stem,code,required,count", [
    ("drama_10_2026_fall", "03005", "not_required", 1),
    ("econ_13_2026_fall", "62040", "required", 8),
])
def test_actual_complete_capture_roundtrips_without_unnecessary_followup(stem, code, required, count):
    html, metadata = raw(stem)
    data = (FIXTURES / f"{stem}.html").read_bytes()
    assert hashlib.sha256(data).hexdigest() == metadata["response"]["sha256"]
    assert len(data) == metadata["response"]["byte_count"]
    form = metadata["request"]["form"]
    session = Session()
    result = workflow.fetch_websoc_course_offering(term="2026 Fall", department=form["Dept"],
                                                  course_number=form["CourseNum"], session=session)
    assert [call[0] for call in session.calls] == ["GET", "POST"]
    assert len(result["sections"]) == count
    assert result["pairing"]["per_primary"][code]["requirement"] == required
    assert discussion_policy_current(result["pairing"])
    assert result["source_proof"]["full_unfiltered_request"] is True


def test_named_reference_fetches_complete_related_course_without_merging_its_identity():
    drama = comment(raw("drama_10_2026_fall")[0], "Concurrent enrollment in ECON 13 is required.")
    session = Session({("DRAMA", "10"): drama})
    result = workflow.fetch_websoc_course_offering(term="2026 Fall", department="DRAMA", course_number="10", session=session)
    assert [call[0] for call in session.calls] == ["GET", "POST", "POST"]
    assert [(call[1]["Dept"], call[1]["CourseNum"]) for call in session.calls[1:]] == [("DRAMA", "10"), ("ECON", "13")]
    rule = result["pairing"]["per_primary"]["03005"]
    assert rule["requirement"] == "required" and rule["relationship"] == "unknown"
    assert rule["allowed_bundles"] == []
    component = rule["required_course_components"][0]
    assert component["course_id"] == "ECON 13" and component["term"] == "2026 Fall"
    assert component["section_codes"] == [str(code) for code in range(62041, 62048)]
    assert [row["section_code"] for row in result["sections"]] == ["03005"]
    assert {row["course_id"] for row in result["co_class_sources"][0]["sections"]} == {"ECON 13"}
    assert result["source_proof"]["needs_lookup"] is False


def test_reference_failure_cannot_clear_discussion_requirement():
    session = Session({("DRAMA", "10"): comment(raw("drama_10_2026_fall")[0], "Co-class ECON 13."),
                       ("ECON", "13"): requests.Timeout("fixture timeout")})
    result = workflow.fetch_websoc_course_offering(term="2026 Fall", department="DRAMA", course_number="10", session=session)
    rule = result["pairing"]["per_primary"]["03005"]
    assert rule["requirement"] == "unknown" and rule["relationship"] == "unknown"
    assert result["source_proof"]["needs_lookup"] is True
    assert result["co_class_lookup_audit"][0]["status"] == "unavailable"
    assert discussion_policy_current(result["pairing"])


def test_nested_reference_is_not_recursively_requested_or_treated_as_complete():
    drama = comment(raw("drama_10_2026_fall")[0], "Co-class ECON 13.")
    econ = comment(raw("econ_13_2026_fall")[0], "Co-class DRAMA 10.")
    session = Session({("DRAMA", "10"): drama, ("ECON", "13"): econ})
    result = workflow.fetch_websoc_course_offering(term="2026 Fall", department="DRAMA", course_number="10", session=session)
    assert len(session.calls) == 3
    assert result["pairing"]["evidence"]["needs_lookup"] is True
    assert result["pairing"]["per_primary"]["03005"]["requirement"] == "required"
    assert result["pairing"]["per_primary"]["03005"]["relationship"] == "unknown"


def test_code_reference_discovers_course_then_fetches_unfiltered_course():
    drama = comment(raw("drama_10_2026_fall")[0], "Required co-class code 62040.")
    discovery = raw("econ_13_2026_fall")[0].replace("Course Number Range: 13", "Course Code Range: 62040")
    session = Session({("DRAMA", "10"): drama}, code_page=discovery)
    result = workflow.fetch_websoc_course_offering(term="2026 Fall", department="DRAMA", course_number="10", session=session)
    assert len(session.calls) == 4
    assert session.calls[2][1]["CourseCodes"] == "62040"
    assert session.calls[2][1]["Dept"].strip() == "ALL"
    assert session.calls[3][1]["CourseNum"] == "13" and not session.calls[3][1]["CourseCodes"]
    related = result["co_class_sources"][0]
    assert related["reference_section_code"] == "62040"
    assert related["code_resolution"]["course_id"] == "ECON 13"
    assert len(related["sections"]) == 8
    assert result["pairing"]["per_primary"]["03005"]["requirement"] == "required"


@pytest.mark.parametrize("change", [lambda html: html.replace("Course Code Range: 62040", "Course Code Range: 99999"),
                                         lambda html: html.replace("Fall Quarter, 2026", "Fall Quarter, 2025")])
def test_code_discovery_wrong_identity_remains_unknown(change):
    drama = comment(raw("drama_10_2026_fall")[0], "Required co-class code 62040.")
    discovery = raw("econ_13_2026_fall")[0].replace("Course Number Range: 13", "Course Code Range: 62040")
    session = Session({("DRAMA", "10"): drama}, code_page=change(discovery))
    result = workflow.fetch_websoc_course_offering(term="2026 Fall", department="DRAMA", course_number="10", session=session)
    assert len(session.calls) == 3
    assert result["pairing"]["per_primary"]["03005"]["requirement"] == "unknown"


def test_lookup_limit_cross_term_and_unsafe_reference_do_not_execute_page_links(monkeypatch):
    source = parsed()
    refs = [{"course_id": cid, "term": "2026 Fall", "relation": "co_class"}
            for cid in ["ECON 13", "PHILOS 3", "MATH 2A", "CHEM 1A"]]
    refs[:0] = [{"course_id": "ECON 13", "term": "2025 Fall", "relation": "co_class"},
                {"url": "https://untrusted.example/perform-action", "term": "2026 Fall", "relation": "co_class"}]
    source["source_proof"]["co_class_references"] = refs
    calls = []
    def child(**kwargs):
        calls.append(kwargs)
        assert kwargs["_follow_references"] is False and kwargs["term"] == "Fall 2026"
        return {"ok": False, "error_code": "offline"}
    monkeypatch.setattr(workflow, "fetch_websoc_course_offering", child)
    result = workflow._resolve_websoc_co_classes(source, session=object(), form_result={}, timeout=12)
    assert len(calls) == workflow.MAX_COCLASS_LOOKUPS == 3
    assert {audit["error_code"] for audit in result["co_class_lookup_audit"] if audit.get("error_code")} >= {
        "cross_term_co_class_reference", "co_class_lookup_limit"}


def stale_response():
    return {"found": True, "source": "db", "term": "Fall 2026", "course_id": "DRAMA 10",
            "sections": [{"section_code": "03005", "section_type": "Lec"}],
            "pairing": {"version": 2, "per_primary": {}, "evidence": {"course_id": "DRAMA 10", "term": "2026 Fall"}}}


def test_old_v2_response_and_old_cache_key_refresh_to_policy_version_without_deletion(monkeypatch):
    old = stale_response()
    db._pairing_evidence_cache[("DRAMA 10", "2026_Fall")] = (float("inf"), deepcopy(old))
    calls = []
    def official(**kwargs):
        calls.append(kwargs)
        return parsed()
    monkeypatch.setattr(workflow, "fetch_websoc_course_offering", official)
    result = db.enrich_sections_pairing("DRAMA 10", "2026 Fall", old)
    assert len(calls) == 1 and discussion_policy_current(result["pairing"])
    assert result["pairing"]["per_primary"]["03005"]["requirement"] == "not_required"
    assert ("DRAMA 10", "2026_Fall") in db._pairing_evidence_cache
    assert ("DRAMA 10", "2026_Fall", POLICY_ID, POLICY_VERSION) in db._pairing_evidence_cache
    again = db.enrich_sections_pairing("DRAMA 10", "2026 Fall", result)
    assert again == result and len(calls) == 1
    db.enrich_sections_pairing("DRAMA 10", "2026 Fall", result, force_refresh=True)
    assert len(calls) == 2


def test_failed_refresh_returns_current_unknown_and_does_not_repeat_or_preserve_stale_positive(monkeypatch):
    old = stale_response()
    old["pairing"]["per_primary"] = {"03005": {"requirement": "not_required"}}
    calls = []
    def fail(**kwargs):
        calls.append(kwargs)
        return {"ok": False, "error_code": "timeout"}
    monkeypatch.setattr(workflow, "fetch_websoc_course_offering", fail)
    result = db.enrich_sections_pairing("DRAMA 10", "2026 Fall", old)
    assert discussion_policy_current(result["pairing"])
    assert result["pairing"]["per_primary"]["03005"]["requirement"] == "unknown"
    assert result["pairing_lookup_status"] == "unavailable"
    db.enrich_sections_pairing("DRAMA 10", "2026 Fall", result)
    db.enrich_sections_pairing("DRAMA 10", "2026 Fall", old)
    assert len(calls) == 1


@pytest.mark.parametrize("flag", ["DYNAMIC_PREFILTER_ENABLED", "RECOMMENDATION_RANKING_ENABLED"])
def test_ordinary_local_hit_enriches_under_each_independent_flag(monkeypatch, flag):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "false")
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "false")
    monkeypatch.setenv(flag, "true")
    section = SectionRecord("2026_Fall_03005", CourseRef("DRAMA", "10"), "03005", "2026_Fall", section_type="Lec")
    monkeypatch.setattr(db, "get_catalog", lambda *_: SimpleNamespace(get_sections=lambda _: [section]))
    monkeypatch.setattr(db, "get_term_coverage", lambda *_: {"coverage_status": "partial"})
    calls = []
    monkeypatch.setattr(workflow, "fetch_websoc_course_offering", lambda **kwargs: calls.append(kwargs) or parsed())
    result = db.get_sections("DRAMA 10", "2026 Fall")
    assert discussion_policy_current(result["pairing"]) and len(calls) == 1
    db.get_sections("DRAMA 10", "2026 Fall")
    assert len(calls) == 1


def test_both_flags_off_keep_local_getter_offline_legacy_shape(monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "false")
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "false")
    section = SectionRecord("2026_Fall_03005", CourseRef("DRAMA", "10"), "03005", "2026_Fall", section_type="Lec")
    monkeypatch.setattr(db, "get_catalog", lambda *_: SimpleNamespace(get_sections=lambda _: [section]))
    monkeypatch.setattr(db, "get_term_coverage", lambda *_: {"coverage_status": "partial"})
    monkeypatch.setattr(workflow, "fetch_websoc_course_offering", lambda **_: pytest.fail("legacy local hit must stay offline"))
    result = db.get_sections("DRAMA 10", "2026 Fall")
    assert result["source"] == "db" and "pairing" not in result
