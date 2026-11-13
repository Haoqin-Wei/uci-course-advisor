"""Per-primary official evidence remains separate from observed rows and selections."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.recommendations.filter_plan import empty_filter_plan
from app.recommendations.pairing import (
    available_pairing_sections,
    normalize_pairing_evidence,
    pairing_for_primary,
    verify_selection,
)
from app.recommendations.prefilter import apply_dynamic_prefilter
from app.scheduling.service import build_pending_schedule_bundle_items, validate_schedule_bundle


TERM = "2026 Fall"
COURSE = "SOCIOL 1"


def section(code, kind="Lec", number="A", days="MW", **updates):
    return {"section_code": code, "section_type": kind, "section_num": number,
            "course_id": COURSE, "term": TERM, "days": days,
            "start_time": "11:00" if kind == "Lec" else "13:00",
            "end_time": "11:50" if kind == "Lec" else "13:50", **updates}


def source(**updates):
    return {"status": "verified", "source": "registrar_websoc", "course_id": COURSE,
            "term": TERM, "source_url": "https://www.reg.uci.edu/perl/WebSoc?YearTerm=2026-92&Dept=SOCIOL&CourseNum=1",
            "retrieved_at": "2026-10-20T12:00:00Z", "html_sha256": "fixture-sha",
            "scope": "department", "rule_id": "sociology_required_directly_below_v1",
            "quote": "Choose discussion or labs directly below lecture.", **updates}


def rule(bundles=None, *, requirement="required", complete=True, components=None, **updates):
    return {"requirement": requirement,
            "required_components": ([{"types": ["Dis"], "min": 1, "max": 1}]
                                    if components is None and requirement == "required" else components or []),
            "relationship": "verified" if complete else "partial", "relationship_complete": complete,
            "allowed_bundles": bundles or [], "evidence": source(), **updates}


def raw_pairing():
    return {"version": 2, "evidence": source(), "per_primary": {
        "69000": rule(requirement="unknown", complete=False),
        "69020": rule([["69021"], ["69022"]]),
        "69040": rule([["69041"]]),
    }}


def normalize(raw=None, **updates):
    return normalize_pairing_evidence({"course_id": COURSE, "term": TERM,
                                      "pairing": raw_pairing() if raw is None else raw, **updates})


def sections():
    # Numeric secondary labels deliberately have no common letter with the Lec.
    return [section("69000"), section("69020", number="B"),
            section("69021", "Dis", "20", "F"), section("69022", "Dis", "21", "F"),
            section("69040", number="C"), section("69041", "Dis", "30", "Tu")]


def codes(rows):
    return [row["section_code"] for row in rows]


def candidate(raw=None):
    return {"course_id": COURSE, "term": TERM, "pairing": raw_pairing() if raw is None else raw,
            "sections": sections(), "offering_status": "offered", "prerequisite_status": "met"}


def test_projection_retains_all_primary_rules_source_and_is_idempotent():
    raw = raw_pairing()
    before = deepcopy(raw)
    pair = normalize(raw)
    b = pairing_for_primary(pair, "69020")
    assert b["requirement"] == "required"
    assert b["allowed_bundles"] == [{"primary_code": "69020", "secondary_codes": ["69021"]},
                                    {"primary_code": "69020", "secondary_codes": ["69022"]}]
    assert set(b["per_primary"]) == {"69000", "69020", "69040"}
    assert b["evidence"]["source_url"] == source()["source_url"]
    assert b["evidence"]["html_sha256"] == "fixture-sha"
    assert pairing_for_primary(b, "69040")["allowed_bundles"][0]["secondary_codes"] == ["69041"]
    assert pairing_for_primary(b, "69000")["requirement"] == "unknown"
    assert pairing_for_primary(b, "not-observed")["requirement"] == "unknown"
    assert pairing_for_primary(b, "69020") == b
    assert normalize(pair) == pair
    assert raw == before


@pytest.mark.parametrize("updates", [{"source": "model"}, {"source": []}, {"status": "unverified"},
    {"term": "2025 Fall"}, {"course_id": "SOCIOL 2"}, {"primary_code": "69040"}])
def test_per_primary_evidence_cannot_cross_source_course_term_or_primary(updates):
    raw = raw_pairing()
    raw["per_primary"]["69020"]["evidence"].update(updates)
    pair = normalize(raw)
    assert pairing_for_primary(pair, "69020")["requirement"] == "unknown"
    assert pairing_for_primary(pair, "69040")["requirement"] == "required"
    assert verify_selection(pair, [sections()[1], sections()[2]])["status"] == "unknown"


@pytest.mark.parametrize("scope", ["evidence", "top"])
def test_mismatched_envelope_scope_does_not_verify_any_primary(scope):
    raw = raw_pairing()
    if scope == "evidence":
        raw["evidence"]["term"] = "2027 Winter"
    else:
        raw["course_id"] = "SOCIOL 2"
    assert pairing_for_primary(normalize(raw), "69020")["requirement"] == "unknown"


def test_one_unknown_primary_does_not_obscure_other_verified_positive_bundles():
    pair = normalize()
    available, status = available_pairing_sections(pair, sections())
    assert status == "partial" and codes(available) == codes(sections())
    formal, status = available_pairing_sections(pair, sections(), verified_only=True)
    assert status == "satisfied"
    assert codes(formal) == ["69020", "69021", "69022", "69040", "69041"]
    assert verify_selection(pair, [sections()[1], sections()[2]])["status"] == "satisfied"
    assert verify_selection(pair, [sections()[0]])["status"] == "unknown"


def test_hard_friday_filter_keeps_unknown_a_and_only_c_is_formally_proven():
    original = candidate()
    before = deepcopy(original)
    plan = empty_filter_plan()
    plan["hard_filters"]["excluded_weekdays"] = ["Fri"]
    result = apply_dynamic_prefilter([original], plan)
    kept = result["kept"][0]
    assert codes(kept["sections"]) == ["69000", "69040", "69041"]
    assert kept["prefilter"]["status"] == "needs_verification"
    assert "secondary_pairing_unknown" in kept["prefilter"]["reasons"]
    assert {row["section_code"] for row in kept["prefilter"]["excluded_sections"]} == {"69020", "69021", "69022"}
    formal, status = available_pairing_sections(normalize_pairing_evidence(kept), kept["sections"], verified_only=True)
    assert status == "satisfied" and codes(formal) == ["69040", "69041"]
    assert kept["pairing"]["per_primary"]["69020"]["allowed_bundles"] == [["69021"], ["69022"]]
    assert original == before


def test_filter_never_turns_missing_secondary_into_not_required_or_guesses_replacement():
    raw = raw_pairing()
    raw["per_primary"].pop("69000")
    item = candidate(raw)
    item["sections"] = [sections()[1], sections()[2], section("69999", "Dis", "B99", "Tu")]
    plan = empty_filter_plan()
    plan["hard_filters"]["excluded_weekdays"] = ["Fri"]
    result = apply_dynamic_prefilter([item], plan)
    assert not result["kept"]
    assert result["excluded"][0]["reasons"] == ["no_compatible_section_bundle"]


def test_partial_relationship_supports_only_explicit_positives_not_absence_claims():
    raw = raw_pairing()
    raw["per_primary"]["69020"] = rule([["69021"]], complete=False)
    pair = normalize(raw)
    b = sections()[1]
    assert verify_selection(pair, [b, sections()[2]])["status"] == "satisfied"
    assert verify_selection(pair, [b, sections()[3]])["status"] == "unknown"
    assert verify_selection(pair, [b])["status"] == "unsatisfied"  # Requirement itself is known.
    available, status = available_pairing_sections(pair, [b, sections()[3]])
    assert status == "unknown" and codes(available) == ["69020", "69022"]
    assert available_pairing_sections(pair, [b, sections()[3]], verified_only=True) == ([], "unknown")
    formal, status = available_pairing_sections(pair, [b, sections()[2]], verified_only=True)
    assert status == "satisfied" and codes(formal) == ["69020", "69021"]


def test_complete_relationship_can_reject_a_known_wrong_secondary_without_letter_guess():
    pair = normalize()
    wrong = section("69999", "Dis", "B1", "Tu")
    assert verify_selection(pair, [sections()[1], wrong])["status"] == "unsatisfied"
    assert available_pairing_sections(pair, [sections()[1], wrong]) == ([], "unsatisfied")


@pytest.mark.parametrize("complete", [False, True])
def test_empty_relationship_list_is_missing_evidence_not_negative(complete):
    raw = raw_pairing()
    raw["per_primary"]["69020"] = rule([], complete=complete)
    pair = normalize(raw)
    assert pairing_for_primary(pair, "69020")["relationship"] == "unknown"
    assert pairing_for_primary(pair, "69020")["relationship_complete"] is False
    assert verify_selection(pair, [sections()[1], sections()[2]])["status"] == "unknown"
    assert available_pairing_sections(pair, [sections()[1], sections()[2]])[1] == "unknown"


def test_malformed_partial_list_cannot_supply_negative_relationship_coverage():
    raw = raw_pairing()
    raw["per_primary"]["69020"]["allowed_bundles"] = [["69021"], ["69022", " 69022"]]
    pair = normalize(raw)
    assert pairing_for_primary(pair, "69020")["relationship_complete"] is False
    assert verify_selection(pair, [sections()[1], sections()[2]])["status"] == "satisfied"
    assert verify_selection(pair, [sections()[1], sections()[3]])["status"] == "unknown"


def test_required_components_respect_or_within_groups_and_and_between_groups():
    raw = raw_pairing()
    raw["per_primary"]["69020"] = rule([["69021"], ["69023"]], components=[{"types": ["Dis", "Lab"], "min": 1, "max": 1}])
    pair = normalize(raw)
    b, dis, lab = sections()[1], sections()[2], section("69023", "Lab", "7", "Tu")
    assert verify_selection(pair, [b, dis])["status"] == "satisfied"
    assert verify_selection(pair, [b, lab])["status"] == "satisfied"
    assert verify_selection(pair, [b, dis, lab])["reason_codes"] == ["component_count_exceeded"]
    assert pairing_for_primary(pair, "69020")["required_types"] == []  # Never flatten OR into AND.
    raw["per_primary"]["69020"] = rule([["69021", "69023"]], components=[
        {"types": ["Dis"], "min": 1, "max": 1}, {"types": ["Lab"], "min": 1, "max": 1}])
    pair = normalize(raw)
    assert verify_selection(pair, [b, dis])["status"] == "unsatisfied"
    assert verify_selection(pair, [b, dis, lab])["status"] == "satisfied"


@pytest.mark.parametrize("components", [None, [], [{"types": ["Dis"], "min": True, "max": 1}],
    [{"types": ["Dis"], "min": 1, "max": False}], [{"types": ["?"], "min": 1, "max": 1}],
    [{"types": ["Dis"], "min": 2, "max": 1}], [{"types": ["Dis"], "min": 0, "max": 0}],
    [{"types": ["Dis"], "min": 1, "max": 1}, {"types": ["Dis", "Lab"], "min": 1, "max": 1}]])
def test_malformed_component_contract_is_unknown(components):
    raw = raw_pairing()
    raw["per_primary"]["69020"]["required_components"] = components
    assert pairing_for_primary(normalize(raw), "69020")["requirement"] == "unknown"


@pytest.mark.parametrize("updates", [{"term": "2025 Fall"}, {"course_id": "SOCIOL 2"}])
def test_selected_section_scope_must_match_proof(updates):
    selected = [sections()[1], {**sections()[2], **updates}]
    assert verify_selection(normalize(), selected)["reason_codes"] == ["section_scope_mismatch"]


def test_no_secondary_requires_positive_evidence_not_an_empty_observed_group():
    raw = raw_pairing()
    a = sections()[0]
    assert verify_selection(normalize(raw), [a])["status"] == "unknown"
    raw["per_primary"]["69000"] = rule(requirement="not_required", complete=False)
    pair = normalize(raw)
    assert verify_selection(pair, [a])["status"] == "satisfied"
    assert verify_selection(pair, [a, sections()[2]])["status"] == "unknown"
    assert codes(available_pairing_sections(pair, [a], verified_only=True)[0]) == ["69000"]


@pytest.mark.parametrize("field,value", [("requirement", []), ("requirement", {}),
    ("relationship", []), ("relationship", {})])
def test_malformed_state_is_unknown_instead_of_an_exception(field, value):
    raw = raw_pairing()
    raw["per_primary"]["69020"][field] = value
    pair = normalize(raw)
    assert verify_selection(pair, [sections()[1], sections()[2]])["status"] == "unknown"


def test_no_secondary_proof_conflicting_with_explicit_bundles_is_unknown():
    raw = raw_pairing()
    raw["per_primary"]["69000"] = rule([["69021"]], requirement="not_required")
    assert verify_selection(normalize(raw), [sections()[0]])["status"] == "unknown"


def test_schedule_infers_actual_primary_projects_it_and_never_autoadds_secondary(monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    all_sections = sections()
    lookup = lambda *_: {"found": True, "pairing": raw_pairing(), "sections": all_sections}
    entries = [{"course_id": COURSE, "term": TERM, "section": "69020"},
               {"course_id": COURSE, "term": TERM, "section": "69021"}]
    items = build_pending_schedule_bundle_items(entries, term=None, section_lookup=lookup)
    assert items[0]["selection_verification"]["status"] == "satisfied"
    assert items[0]["pairing_selected"]["selected_primary_code"] == "69020"
    assert set(items[0]["pairing"]["per_primary"]) == {"69000", "69020", "69040"}
    validated = validate_schedule_bundle(items)
    assert validated["pairing"][0]["selection_verification"]["status"] == "satisfied"
    assert validated["pairing"][0]["pairing_selected"]["selected_primary_code"] == "69020"
    incomplete = build_pending_schedule_bundle_items(entries[:1], term=None, section_lookup=lookup)
    assert codes(incomplete[0]["selected_sections"]) == ["69020"]
    assert incomplete[0]["selection_verification"]["status"] == "unsatisfied"
    assert any(issue["type"] == "incomplete_pairing" for issue in validate_schedule_bundle(incomplete)["conflicts"])
    unknown = build_pending_schedule_bundle_items(
        [{"course_id": COURSE, "term": TERM, "section": "69000"}], term=None, section_lookup=lookup)
    assert unknown[0]["pairing_selected"]["requirement"] == "unknown"
    assert any(issue["type"] == "pairing_unknown" for issue in validate_schedule_bundle(unknown)["unknowns"])


def test_no_contract_never_becomes_formal_from_observed_sections_alone():
    assert available_pairing_sections(None, sections())[1] == "unknown"
    assert available_pairing_sections(None, sections(), verified_only=True) == ([], "unknown")


def official_response(stem):
    from app.data.websoc_workflow import parse_websoc_course_html
    fixture = Path(__file__).parent / "fixtures" / "websoc" / "pairing"
    meta = json.loads((fixture / f"{stem}.json").read_text())
    html = (fixture / f"{stem}.html").read_bytes().decode(meta["response"]["encoding"])
    return parse_websoc_course_html(html, term=meta["scope"]["term"],
        department=meta["request"]["form"]["Dept"], course_number=meta["request"]["form"]["CourseNum"],
        source_url=meta["request"]["url"], retrieved_at=meta["captured_at"],
        request_form=meta["request"]["form"], html_sha256=meta["response"]["sha256"])


def test_actual_official_html_to_friday_filter_to_formal_bundle_without_injected_proof():
    response = official_response("sociol_1_2026_fall")
    plan = empty_filter_plan()
    plan["hard_filters"]["excluded_weekdays"] = ["Fri"]
    result = apply_dynamic_prefilter([{**response, "course_id": COURSE,
                                      "prerequisite_status": "met"}], plan)
    kept = result["kept"][0]
    pair = normalize_pairing_evidence(kept)
    formal, status = available_pairing_sections(pair, kept["sections"], verified_only=True)
    assert status == "satisfied"
    assert codes(formal) == ["69020", *map(str, range(69021, 69027))]
    assert pairing_for_primary(pair, "69020")["evidence"]["html_sha256"] == response["source_proof"]["html_sha256"]
    selected = [formal[0], formal[1]]
    assert verify_selection(pair, selected)["status"] == "satisfied"
    assert all("F" not in row["days"] for row in selected)
    assert pairing_for_primary(pair, "69000")["requirement"] == "required"
    assert pairing_for_primary(pair, "69000")["relationship"] == "unknown"
    assert "secondary_pairing_unknown" in kept["prefilter"]["reasons"]


def test_actual_numeric_discussion_html_is_required_under_the_confirmed_product_policy():
    response = official_response("philos_3_2026_fall")
    pair = normalize_pairing_evidence({**response, "course_id": "PHILOS 3"})
    assert pair["per_primary"]["30220"]["requirement"] == "required"
    assert verify_selection(pair, response["sections"][:2])["status"] == "satisfied"
    assert verify_selection(pair, response["sections"][:1])["status"] == "unsatisfied"
    assert codes(available_pairing_sections(pair, response["sections"], verified_only=True)[0]) == codes(response["sections"])
