"""Explicit pairing evidence, complete selections and safe draft UI."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from app.recommendations.pairing import normalize_pairing_evidence, verify_selection
from app.recommendations.prefilter import apply_dynamic_prefilter
from app.recommendations.filter_plan import empty_filter_plan
from app.scheduling.service import build_pending_schedule_bundle_items, validate_schedule_bundle
from tests.test_zotistics_cards import render

ROOT = Path(__file__).resolve().parents[1]


def section(code="10001", kind="Lec", number="A", days="MW"):
    return {"section_code": code, "section_num": number, "section_type": kind,
            "days": days, "start_time": "11:00", "end_time": "11:50"}


def evidence(requirement="required", types=None, bundles=None, **updates):
    pair = {"version": 1, "requirement": requirement,
            "required_types": (["Dis", "Lab"] if requirement == "required" else []) if types is None else types,
            "allowed_bundles": [{"primary_code": "10001", "secondary_codes": ["10002", "10003"]}] if bundles is None else bundles,
            "evidence": {"status": "verified", "source": "verified_course_pairing", "term": "2026 Fall", "course_id": "ICS 6D"}}
    pair["evidence"].update(updates)
    return pair


def normalize(pair):
    return normalize_pairing_evidence({"course_id": "I&C SCI 6D", "term": "2026 Fall", "pairing": pair})


@pytest.mark.parametrize("legacy", [{}, {"requires_secondary": False}, {"requires_secondary": True},
    {"sections": [section(), section("10002", "Dis", "A1")]},
    {"required_secondary_types": ["Dis"], "requires_secondary": True}])
def test_legacy_bools_absence_coexistence_and_letters_never_verify(legacy):
    pair = normalize_pairing_evidence({"course_id": "ICS6D", "term": "2026 Fall", **legacy})
    assert pair["requirement"] == "unknown"
    assert verify_selection(pair, [section(), section("10002", "Dis", "A1")])["status"] == "unknown"


@pytest.mark.parametrize("updates", [{"status": "unverified"}, {"source": "model"},
    {"source": {}}, {"term": "2025 Fall"}, {"course_id": "ICS45C"}, {"course_id": None}])
def test_verified_evidence_is_bound_to_course_term_and_trusted_source(updates):
    assert normalize(evidence(**updates))["requirement"] == "unknown"


@pytest.mark.parametrize("types", [["Dis", 3], ["Dis", None], ["bad"], "Dis", [], ["Dis", {}]])
def test_invalid_required_component_does_not_get_silently_dropped(types):
    assert normalize(evidence(types=types))["requirement"] == "unknown"


def test_all_required_components_and_explicit_relationship_are_checked():
    pair = normalize(evidence())
    lec, dis, lab = section(), section("10002", "Dis", "A1"), section("10003", "Lab", "A2")
    assert pair["requirement"] == "required"
    assert verify_selection(pair, [lec])["status"] == "unsatisfied"
    assert verify_selection(pair, [lec, dis])["status"] == "unsatisfied"
    assert verify_selection(pair, [lec, dis, lab])["status"] == "satisfied"
    assert verify_selection(pair, [dis, lab])["status"] == "unsatisfied"
    wrong = section("19999", "Lab", "A3")  # Same letter is not a verified relation.
    assert verify_selection(pair, [lec, dis, wrong])["status"] == "unsatisfied"
    assert verify_selection(normalize(evidence(bundles=[])), [lec, dis, lab])["status"] == "unknown"
    unknown_type = {**lab, "section_type": "?"}
    assert verify_selection(pair, [lec, dis, unknown_type])["status"] == "unknown"


def test_not_required_is_explicit_and_extra_components_are_unverified():
    pair = normalize(evidence("not_required"))
    assert verify_selection(pair, [section()])["status"] == "satisfied"
    assert verify_selection(pair, [section(), section("10002", "Dis", "A1")])["status"] == "unknown"
    assert verify_selection(pair, [section(), section("10004", "Lec", "B")])["status"] == "unsatisfied"


def candidate(pair):
    return {"course_id": "ICS6D", "term": "2026 Fall", "pairing": pair,
            "sections": [section(), section("10002", "Dis", "A1", "F"), section("10003", "Lab", "A2")],
            "offering_status": "offered", "prerequisite_status": "met"}


def test_filtering_cannot_turn_disappearance_into_no_pairing_required():
    plan = empty_filter_plan()
    plan["hard_filters"]["excluded_weekdays"] = ["Fri"]
    unknown = normalize_pairing_evidence({"term": "2026 Fall", "requires_secondary": False})
    retained = apply_dynamic_prefilter([candidate(unknown)], plan)
    assert retained["kept"][0]["pairing"]["requirement"] == "unknown"
    assert "secondary_pairing_unknown" in retained["kept"][0]["prefilter"]["reasons"]
    required = apply_dynamic_prefilter([candidate(normalize(evidence()))], plan)
    assert required["kept"] == []
    assert "no_compatible_section_bundle" in required["excluded"][0]["reasons"]


def test_schedule_uses_contract_and_keeps_unknown_separate_from_saved(monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    pair = normalize(evidence())
    all_sections = [section(), section("10002", "Dis", "A1"), section("10003", "Lab", "A2")]
    entries = [{"course_id": "ICS6D", "term": "2026 Fall", "section": "A"}]
    lookup = lambda *_: {"found": True, "pairing": pair, "sections": all_sections}
    items = build_pending_schedule_bundle_items(entries, term=None, section_lookup=lookup)
    assert items[0]["selection_verification"]["status"] == "unsatisfied"
    result = validate_schedule_bundle(items)
    assert result["pairing"][0]["selection_verification"]["status"] == "unsatisfied"
    assert any(issue["type"] == "incomplete_pairing" for issue in result["conflicts"])
    unknown_items = build_pending_schedule_bundle_items(entries, term=None,
        section_lookup=lambda *_: {"found": True, "sections": all_sections})
    result = validate_schedule_bundle(unknown_items)
    assert result["pairing"][0]["selection_verification"]["status"] == "unknown"
    assert any(issue["type"] == "pairing_unknown" for issue in result["unknowns"])
    assert result["valid"] is False


def test_both_flags_disabled_preserve_legacy_schedule_contract(monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "false")
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "false")
    items = build_pending_schedule_bundle_items(
        [{"course_id": "ICS6D", "term": "2026 Fall", "section": "A"}], term=None,
        section_lookup=lambda *_: {"found": True, "sections": [section(), section("10002", "Dis", "A1")]})
    assert "pairing" not in items[0] and items[0]["requires_secondary"] is True
    assert "pairing" not in validate_schedule_bundle(items)


def test_modern_card_uses_contract_and_does_not_upgrade_legacy_bool():
    payload = {"course_id": "ICS6D", "enrollment_codes": [{"code": "10001"}],
               "requires_secondary": True, "pairing": normalize_pairing_evidence({"term": "2026 Fall"})}
    html = render(f"_renderCodeRow({json.dumps(payload)})")
    assert "Pairing unverified" in html and "+ Dis required" not in html
    payload.pop("pairing")
    assert "+ Dis required" in render(f"_renderCodeRow({json.dumps(payload)})")


def test_modern_plan_keeps_single_added_section_as_draft_not_complete():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required")
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const added=new Set(), validation={pairing:[]};
const ctx=vm.createContext({addedCourses:added,scheduleValidation:validation,
 canonicalTerm:x=>x,scheduleCourseKey:(t,c)=>t+'|'+c,displayTerm:x=>x});
vm.runInContext(fs.readFileSync('static/js/plan.js','utf8'),ctx);
const c={course_id:'ICS6D',term:'2026 Fall',primary_code:'10001',sections:[{section_code:'10001',section_type:'Lec'}],
 pairing:{version:1,requirement:'unknown',evidence:{status:'unverified'}},selection_verification:{status:'unknown'}};
assert.equal(ctx.planActionLabel([c]),'Review pairing');
added.add('2026 Fall|ICS6D');assert.equal(ctx.planActionLabel([c]),'Review pairing');
c.pairing={version:1,requirement:'not_required',evidence:{status:'verified'}};
c.selection_verification={status:'satisfied'};
assert.equal(ctx.planNeedsPairingReview(c),true);
validation.pairing.push({course_id:'ICS6D',term:'2026 Fall',selection_verification:{status:'satisfied'}});
assert.equal(ctx.planNeedsPairingReview(c),false);assert.match(ctx.planActionLabel([c]),/Saved/);
delete c.pairing;assert.match(ctx.planActionLabel([c]),/Added/);
"""
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
