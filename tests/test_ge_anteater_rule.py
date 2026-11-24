"""GE status comes from the synced Anteater catalogue.

A label there means the course is a GE course in that category; a catalogue
course without a label is not a GE course. The official GE page is only a
cross-check: a difference is reported, never used to change the answer.
"""

from __future__ import annotations

import pytest

from app.catalog import course_store
from app.catalog.course_sync import course_row_from_api
from app.data import ge_evidence
from app.recommendations.presentation import normalize_ge_evidence, normalize_recommendation_card
from tests.test_course_catalog_store import api_course

TERM = "2027 Winter"
SYNCED = "2026-11-12T04:09:28+00:00"
OFFICIAL_URL = "https://catalogue.uci.edu/informationforadmittedstudents/requirementsforabachelorsdegree/"


def _official(courses):
    return {"status": "verified", "catalogue_year": "2026-27", "courses": courses,
            "source": {"source": ge_evidence.SOURCE, "status": "verified", "source_url": OFFICIAL_URL,
                       "catalogue_year": "2026-27"}}


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    path = tmp_path / "catalog.db"
    monkeypatch.setenv("CATALOG_DB_PATH", str(path))
    monkeypatch.setenv("CATALOG_AUTO_SYNC_ENABLED", "false")
    monkeypatch.setenv("CATALOG_VERSION_CHECK_SECONDS", "0")
    course_store.replace_courses([
        course_row_from_api(api_course("ANTHRO", "2A", geList=[
            "GE III: Social & Behavioral Sciences", "GE VIII: International/Global Issues"])),
        course_row_from_api(api_course("WRITING", "39C", geList=["GE Ia: Lower Division Writing"])),
        course_row_from_api(api_course("COMPSCI", "161")),
        course_row_from_api(api_course("ECON", "20B", geList=["GE III: Social & Behavioral Sciences"])),
    ], source="anteater_courses", synced_at=SYNCED, path=path)
    monkeypatch.setattr(ge_evidence, "_anteater_index_cache", (None, {}, None))
    monkeypatch.setattr(ge_evidence, "_official_view_cache", {})
    monkeypatch.setattr(ge_evidence, "_annual_evidence", lambda year, allow_network: _official({
        "ANTHRO 2A": ["III", "VIII"], "ECON 20B": ["III", "VA"], "COMPSCI 161": ["II"]}))
    return path


def test_a_label_means_ge_in_that_category(catalogue):
    evidence = ge_evidence.get_course_ge_evidence("ANTHRO 2A", TERM)
    assert evidence["status"] == "verified" and evidence["is_ge"] is True
    assert evidence["categories"] == ["III", "VIII"]
    assert evidence["source"]["source"] == ge_evidence.ANTEATER_SOURCE
    assert evidence["source"]["retrieved_at"] == SYNCED
    assert evidence["source"]["official_check"]["status"] == "agrees"


def test_no_label_means_not_ge_even_when_the_official_list_has_the_course(catalogue):
    evidence = ge_evidence.get_course_ge_evidence("COMPSCI 161", TERM)
    assert evidence["status"] == "verified" and evidence["is_ge"] is False
    assert evidence["categories"] == []
    check = evidence["source"]["official_check"]
    assert check["status"] == "differs" and check["official_categories"] == ["II"]


def test_a_different_official_label_is_reported_but_anteater_stands(catalogue):
    fields = ge_evidence.enrich_course_ge_evidence({"course_id": "ECON 20B"}, TERM)
    assert fields["ge_labels_verified"] is True and fields["ge_categories"] == ["III"]
    check = fields["ge_provenance"]["official_check"]
    assert check == {"status": "differs", "catalogue_year": "2026-27", "source_url": OFFICIAL_URL,
                     "official_categories": ["III", "VA"], "anteater_categories": ["III"]}


def test_writing_labels_are_not_a_difference(catalogue):
    # The official page's course tables start at category II.
    evidence = ge_evidence.get_course_ge_evidence("WRITING 39C", TERM)
    assert evidence["categories"] == ["IA"] and evidence["is_ge"] is True
    assert evidence["source"]["official_check"]["status"] == "agrees"


def test_unreachable_official_list_does_not_change_the_answer(catalogue, monkeypatch):
    monkeypatch.setattr(ge_evidence, "_annual_evidence", lambda year, allow_network: {
        "status": "unknown", "reason": "official_ge_evidence_unavailable"})
    evidence = ge_evidence.get_course_ge_evidence("ANTHRO 2A", TERM)
    assert evidence["status"] == "verified" and evidence["categories"] == ["III", "VIII"]
    assert evidence["source"]["official_check"]["status"] == "not_checked"


def test_course_outside_the_catalogue_stays_unknown(catalogue):
    # Listed officially, absent from the synced catalogue: the old rule applies.
    monkeypatch_courses = ge_evidence._annual_evidence("2026-27", True)["courses"]
    assert "ANATOMY 500A" not in monkeypatch_courses
    evidence = ge_evidence.get_course_ge_evidence("ANATOMY 500A", TERM)
    assert evidence["status"] == "unknown" and evidence["categories"] == []
    assert evidence.get("is_ge") is None


def test_without_a_synced_catalogue_the_official_list_still_decides(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOG_DB_PATH", str(tmp_path / "empty.db"))
    monkeypatch.setattr(ge_evidence, "_anteater_index_cache", (None, {}, None))
    monkeypatch.setattr(ge_evidence, "_annual_evidence", lambda year, allow_network: _official({
        "ECON 20B": ["III"]}))
    listed = ge_evidence.get_course_ge_evidence("ECON 20B", TERM)
    assert listed["status"] == "verified" and listed["source"]["source"] == ge_evidence.SOURCE
    assert ge_evidence.get_course_ge_evidence("COMPSCI 161", TERM)["status"] == "unknown"


def test_anteater_provenance_is_accepted_and_tampering_is_not():
    good = ge_evidence.anteater_provenance(SYNCED)
    assert ge_evidence.verified_ge_provenance(good, TERM) is True
    assert ge_evidence.verified_ge_provenance({**good, "source_url": "https://example.com"}, TERM) is False
    assert ge_evidence.verified_ge_provenance({**good, "retrieved_at": ""}, TERM) is False
    assert ge_evidence.verified_ge_provenance({**good, "status": "unknown"}, TERM) is False
    assert ge_evidence.verified_ge_provenance(good, "not a term") is False


def _card(course_id, catalogue_fields):
    return {"course_id": course_id, "term": TERM, **catalogue_fields}


def test_card_shows_non_ge_and_the_difference(catalogue):
    ge_card = normalize_ge_evidence(_card("ECON 20B", ge_evidence.enrich_course_ge_evidence(
        {"course_id": "ECON 20B"}, TERM)))
    assert ge_card["status"] == "verified" and ge_card["is_ge"] is True
    assert ge_card["categories"] == ["III"]
    assert ge_card["official_check"]["status"] == "differs"
    assert ge_card["official_check"]["official_categories"] == ["III", "VA"]

    plain = normalize_recommendation_card(_card("COMPSCI 161", ge_evidence.enrich_course_ge_evidence(
        {"course_id": "COMPSCI 161"}, TERM)), "zh")
    assert plain["ge_evidence"]["is_ge"] is False and plain["ge_category"] is None
    assert plain["ge_labels_verified"] is True
    assert "不是 GE 课程" in plain["reason"]


def test_loader_marks_catalogue_courses_and_leaves_timetable_only_courses_unknown(catalogue):
    from app.catalog.loaders.catalog_store import CatalogStoreLoader
    from app.catalog.term import Term
    from app.catalog.types import CourseRef

    from app.catalog import store
    store.replace_term_sections("2027_Winter", [
        {"section_code": "1", "department": "ANATOMY", "course_number": "500A"}],
        source="test", path=catalogue)
    courses, _ = CatalogStoreLoader(db_path=catalogue).load(Term.parse(TERM))
    by_ref = {course.ref: course for course in courses}
    anthro = by_ref[CourseRef("ANTHRO", "2A")]
    assert anthro.ge_categories == ("III", "VIII") and anthro.ge_categories_verified is True
    assert anthro.ge_provenance["source"] == ge_evidence.ANTEATER_SOURCE
    algorithms = by_ref[CourseRef("COMPSCI", "161")]
    assert algorithms.ge_categories == () and algorithms.ge_categories_verified is True
    timetable_only = by_ref[CourseRef("ANATOMY", "500A")]
    assert timetable_only.ge_categories_verified is False


_BADGE_SCRIPT = r"""
const fs = require('node:fs'); const vm = require('node:vm');
const escHTML = v => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const escAttr = v => escHTML(v).replace(/"/g, '&quot;');
const context = vm.createContext({console, escHTML, escAttr, currentResponseLanguage: 'zh'});
vm.runInContext(fs.readFileSync(process.argv[1] + '/static/js/cards.js', 'utf8'), context);
const source = {source: 'anteater_catalogue', retrieved_at: '2026-11-12T04:09:28+00:00'};
const card = (is_ge, check) => ({ge_evidence: {status: 'verified', is_ge, source, official_check: check}});
console.log(JSON.stringify({
  ge: context._renderCardSourceBadges(card(true, {status: 'agrees'})),
  differs: context._renderCardSourceBadges(card(true, {status: 'differs', catalogue_year: '2026-27',
                                                       official_categories: ['III', 'VA']})),
  not_ge: context._renderCardSourceBadges(card(false, {status: 'agrees'})),
}));
"""


def test_card_badges_name_anteater_and_flag_a_difference():
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parents[1]
    out = json.loads(subprocess.run([node, "-e", _BADGE_SCRIPT, str(root)], check=True,
                                    capture_output=True, text=True).stdout)
    assert "GE · Anteater" in out["ge"] and "同步于 2026-11-12" in out["ge"]
    assert "GE 与官方列表不一致" not in out["ge"]
    assert "GE 与官方列表不一致" in out["differs"] and "标注为 III / VA" in out["differs"]
    assert out["not_ge"] == ""
