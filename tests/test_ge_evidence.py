"""Annual GE boundaries using an unmodified official page and real CSV rows."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
import requests

from app.catalog.ge import normalize_ge_category, parse_ge_categories
from app.catalog.loaders.uci_relational import UCIRelationalLoader
from app.catalog.term import Term
from app.data import ge_evidence as ge
from app.recommendations.presentation import normalize_ge_evidence, normalize_recommendation_card

FIXTURES = Path(__file__).parent / "fixtures/ge_evidence"


@pytest.fixture
def official_html():
    return (FIXTURES / "uci_ge_2026-27.html").read_text()


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(ge, "CACHE_DIR", tmp_path / "annual")
    monkeypatch.setattr(ge, "_cache", {})


@pytest.mark.parametrize("value,expected", [
    ("GE III: Social & Behavioral Sciences|GE VII: Multicultural Studies", ["III", "VII"]),
    ('["GE III: Social & Behavioral Sciences", "GE VII: Multicultural Studies"]', ["III", "VII"]),
    (["III", "GE-III", "VII"], ["III", "VII"]),
    ("GE Va: Quantitative Literacy", ["VA"]), ("GE-II", ["II"]), ("", []),
])
def test_real_storage_formats_normalize_without_authenticating(value, expected):
    assert parse_ge_categories(value) == {"categories": expected, "unrecognized": []}


@pytest.mark.parametrize("value", ["III and VII", "GE IX: Unknown", "GE III|GE VII", 3, None])
def test_a_single_tag_parser_never_silently_chooses_one_label(value):
    assert normalize_ge_category(value) is None


def test_actual_local_sociol1_csv_has_two_labels_but_no_verified_academic_year():
    courses, sections = UCIRelationalLoader(FIXTURES / "local_sociol1").load(Term(2026, "Spring"))
    course = next(c for c in courses if c.ref.display() == "SOCIOL 1")
    assert course.ge_categories == ("III", "VII")
    assert course.ge_categories_verified is False
    assert course.ge_provenance["status"] == "unknown"
    assert course.ge_provenance["catalogue_year"] is None
    assert "|" in course.ge_provenance["raw_labels"]
    assert sections[0].ge_categories == ("GE III: Social & Behavioral Sciences", "GE VII: Multicultural Studies")


def test_official_snapshot_integrity_and_actual_sociol1_multilabel(official_html):
    metadata = json.loads((FIXTURES / "uci_ge_2026-27.metadata.json").read_text())
    assert hashlib.sha256(official_html.encode()).hexdigest() == metadata["sha256"]
    result = ge.parse_official_ge_html(official_html, source_url=metadata["source_url"], retrieved_at=metadata["retrieved_at"])
    assert result["status"] == "verified"
    assert result["catalogue_year"] == "2026-27"
    assert result["courses"]["SOCIOL 1"] == ["III", "VII"]
    assert result["courses"]["SOCIOL 10A"] == ["VA"]
    assert result["source"]["content_sha256"] == metadata["sha256"]


@pytest.mark.parametrize("term,year", [("2026 Fall", "2026-27"), ("2027 Winter", "2026-27"),
                                      ("2027 Spring", "2026-27"), ("2027 Summer1", "2026-27"),
                                      ("2026 Spring", "2025-26"), ("2027 Fall", "2027-28")])
def test_evidence_academic_year_follows_target_term_without_changing_it(term, year):
    assert ge.academic_year_for_term(term) == year


def test_one_official_get_serves_multiple_courses_and_terms_and_survives_reload(monkeypatch, official_html):
    calls = []
    monkeypatch.setattr(ge, "_get_html", lambda url: calls.append(url) or official_html)
    a = ge.get_course_ge_evidence("SOCIOL1", "2026 Fall")
    b = ge.get_course_ge_evidence("SOCIOL 2", "2027 Winter")
    assert a["categories"] == ["III", "VII"] and b["categories"] == ["III", "VIII"]
    assert calls == [ge.CURRENT_URL]
    cached = json.loads((ge.CACHE_DIR / "2026-27.json").read_text())
    assert cached["source_html"] == official_html
    ge._cache.clear()
    assert ge.get_course_ge_evidence("SOCIOL 1", "2027 Spring", allow_network=False)["status"] == "verified"
    assert calls == [ge.CURRENT_URL]


def test_current_page_cannot_verify_another_year_and_failures_are_short_cached(monkeypatch, official_html):
    calls = []
    monkeypatch.setattr(ge, "_get_html", lambda url: calls.append(url) or official_html)
    assert ge.get_course_ge_evidence("SOCIOL1", "2026 Spring")["status"] == "unknown"
    assert len(calls) == 2 and "/previouseditions/2025-26/" in calls[1]
    assert ge.get_course_ge_evidence("SOCIOL2", "2025 Fall")["status"] == "unknown"
    assert len(calls) == 2
    assert ge.get_course_ge_evidence("SOCIOL1", "2027 Fall")["status"] == "unknown"
    assert len(calls) == 3  # Future editions never trigger archive fan-out.


def test_transport_failure_and_absent_course_do_not_mean_non_ge(monkeypatch, official_html):
    def unavailable(_url):
        raise requests.Timeout("offline fixture")
    monkeypatch.setattr(ge, "_get_html", unavailable)
    assert ge.get_course_ge_evidence("SOCIOL1", "2026 Fall")["status"] == "unknown"
    ge._cache.clear()
    monkeypatch.setattr(ge, "CACHE_DIR", ge.CACHE_DIR / "other")
    monkeypatch.setattr(ge, "_get_html", lambda _url: official_html)
    missing = ge.get_course_ge_evidence("COMPSCI 999", "2026 Fall")
    assert missing["status"] == "unknown" and missing["categories"] == []


def test_cache_snapshot_tampering_cannot_upgrade_unknown_without_network(monkeypatch, official_html):
    monkeypatch.setattr(ge, "_get_html", lambda _url: official_html)
    ge.get_course_ge_evidence("SOCIOL1", "2026 Fall")
    path = ge.CACHE_DIR / "2026-27.json"
    cached = json.loads(path.read_text())
    cached["result"]["courses"]["SOCIOL 1"] = ["II"]
    path.write_text(json.dumps(cached))
    ge._cache.clear()
    assert ge.get_course_ge_evidence("SOCIOL1", "2026 Fall", allow_network=False)["status"] == "unknown"


def test_enrichment_keeps_local_unknown_and_separates_course_from_personal_credit(monkeypatch, official_html):
    local = {"course_id": "SOCIOL 1", "ge_categories": ["GE III: Social & Behavioral Sciences", "GE VII: Multicultural Studies"]}
    before = deepcopy(local)
    unknown = ge.enrich_course_ge_evidence(local, "2026 Fall", allow_network=False)
    assert unknown["ge_categories"] == ["III", "VII"] and unknown["ge_labels_verified"] is False
    assert local == before
    monkeypatch.setattr(ge, "_get_html", lambda _url: official_html)
    fields = ge.enrich_course_ge_evidence(local, "2026 Fall")
    card = {**local, **fields, "term": "2026 Fall"}
    evidence = normalize_ge_evidence(card)
    assert evidence["status"] == "verified" and evidence["categories"] == ["III", "VII"]
    assert evidence["personal_contribution"]["status"] == "unknown"
    normalized = normalize_recommendation_card(card)
    assert normalized["ge_category"] == "GE-III / GE-VII"
    assert normalize_recommendation_card(normalized) == normalized
    assert normalize_ge_evidence({**card, "term": "2027 Fall", "ge_evidence_term": "2027 Fall"})["status"] == "unknown"


def test_old_verified_flag_or_complete_json_syntax_is_not_annual_verification():
    card = {"course_id": "SOCIOL1", "term": "2026 Fall", "ge_categories": ["III", "VII"],
            "ge_labels_verified": True, "ge_evidence_course_id": "SOCIOL1", "ge_evidence_term": "2026 Fall",
            "ge_provenance": {"loader": "uci_relational", "source_file": "local.csv"}}
    assert normalize_ge_evidence(card)["status"] == "unknown"


def test_reused_annual_envelope_must_still_bind_the_same_course(monkeypatch, official_html):
    monkeypatch.setattr(ge, "_get_html", lambda _url: official_html)
    fields = ge.enrich_course_ge_evidence({"course_id": "SOCIOL 1"}, "2026 Fall")
    assert fields["ge_labels_verified"] is True
    # This presentation boundary must not carry SOCIOL 1 tags onto a new ID.
    ge._cache.clear()
    monkeypatch.setattr(ge, "CACHE_DIR", ge.CACHE_DIR / "empty")
    foreign = ge.enrich_course_ge_evidence({**fields, "course_id": "SOCIOL 31"}, "2026 Fall", allow_network=False)
    assert foreign["ge_labels_verified"] is False


def test_incomplete_or_unapproved_official_html_stays_unknown(official_html):
    assert ge.parse_official_ge_html("<h1>2026-27 Edition</h1>", source_url=ge.CURRENT_URL)["status"] == "unknown"
    assert ge.parse_official_ge_html(official_html, source_url="https://example.com/" + ge.GE_PATH)["status"] == "unknown"
