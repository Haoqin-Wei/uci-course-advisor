"""Discussion-policy consumers use actual public timetable parser output.

The Lab and co-class cases explicitly modify a public page as counterexamples;
they are not represented as captured official course requirements.
"""
from copy import deepcopy
import json

import pytest

from app.data.websoc_pairing import resolve_pairing_references
from app.recommendations.pairing import normalize_pairing_evidence, pairing_for_primary, verify_selection
from app.recommendations.presentation import normalize_recommendation_card, render_recommendation_answer
from tests.test_websoc_pairing_producer import _course_comment, _parse, _referenced_drama
from tests.test_zotistics_cards import render


def _card(envelope, primary_code):
    pair = envelope["pairing"]
    sections = envelope["sections"]
    primary = next(row for row in sections if row["section_code"] == primary_code)
    course = pair["evidence"]["course_id"]
    current = pairing_for_primary(pair, primary_code)
    summary = {**primary, "code": primary_code, "num": primary.get("section_num"),
               "type": primary.get("section_type"), "pairing_selected": current}
    return {"course_id": course, "term": "2026 Fall", "primary_code": primary_code,
            "pairing": pair, "pairing_source_proof": envelope["source_proof"], "sections": sections,
            "selection_verification": verify_selection(pair, [primary], primary_code=primary_code),
            "section_groups": [{"letter": summary["num"], "primary": summary, "secondaries": []}]}


def _html(card, language):
    return render(f"(() => {{ globalThis.currentResponseLanguage = {json.dumps(language)}; "
                  f"return renderCard({json.dumps(card)}); }})()")


@pytest.mark.parametrize("language", ["zh", "en"])
def test_real_complete_drama_no_discussion_does_not_claim_no_other_components(language):
    card = normalize_recommendation_card(_card(_parse("drama_10_2026_fall"), "03005"), language)
    assert card["pairing_selected"]["requirement"] == "not_required"
    html = _html(card, language)
    assert ("无需选 Dis" if language == "zh" else "No discussion required") in html
    assert "无需附加配对" not in html and "No secondary required" not in html
    answer = render_recommendation_answer([card], language)
    assert ("此主班无需选 Dis" if language == "zh" else "do not require discussion for this primary") in answer
    assert "Registrar requires" not in answer and "校方统一" not in answer


@pytest.mark.parametrize("language", ["zh", "en"])
def test_real_econ_discussion_requirement_does_not_certify_an_unselected_bundle(language):
    card = normalize_recommendation_card(_card(_parse("econ_13_2026_fall"), "62040"), language)
    assert card["pairing_selected"]["requirement"] == "required"
    assert card["selection_verification"]["status"] == "unsatisfied"
    html = _html(card, language)
    assert ("需配 Dis" if language == "zh" else "+ Dis required") in html
    assert ("尚未选择完整配套班次" if language == "zh" else "Complete secondary selection needed") in html
    assert "No discussion required" not in html and "无需选 Dis" not in html


@pytest.mark.parametrize("language", ["zh", "en"])
def test_required_discussion_with_unknown_relationship_keeps_linked_course_visible(language):
    # Explicitly derived co-class counterexample, using both courses' real rows.
    source = _referenced_drama()
    pair, proof = resolve_pairing_references(source["pairing"], source["source_proof"],
                                            {"PHILOS 3": _parse("philos_3_2026_fall")})
    envelope = {**source, "pairing": pair, "source_proof": proof}
    card = normalize_recommendation_card(_card(envelope, "03005"), language)
    selected = card["pairing_selected"]
    assert selected["requirement"] == "required" and selected["relationship"] == "unknown"
    assert card["selection_verification"]["status"] != "satisfied"
    html = _html(card, language)
    assert ("需配 Dis" if language == "zh" else "+ Dis required") in html
    assert ("可配班次待核实" if language == "zh" else "Compatible sections remain unverified") in html
    assert "co-class" in html and "PHILOS 3" in html
    assert "No discussion required" not in html and "无需选 Dis" not in html
    answer = render_recommendation_answer([card], language)
    assert "PHILOS 3" in answer and "co-class" in answer
    assert ("可配的具体班次仍待核实" if language == "zh" else "compatible section relationship remains unverified") in answer
    assert ("当前选择满足已核实的配对规则" if language == "zh" else "current selection satisfies the verified pairing rules") not in answer


def test_linked_course_types_codes_scope_and_source_survive_normalization_and_projection():
    source = _referenced_drama()
    resolved, _ = resolve_pairing_references(source["pairing"], source["source_proof"],
                                           {"PHILOS 3": _parse("philos_3_2026_fall")})
    expected = deepcopy(resolved["per_primary"]["03005"]["required_course_components"])
    normalized = normalize_pairing_evidence({"course_id": "DRAMA 10", "term": "2026 Fall", "pairing": resolved})
    selected = pairing_for_primary(normalized, "03005")
    assert normalized["per_primary"]["03005"]["required_course_components"] == expected
    assert selected["required_course_components"] == expected
    item = selected["required_course_components"][0]
    assert item["course_id"] == "PHILOS 3" and item["term"] == "2026 Fall" and item["types"] == ["Dis"]
    assert item["section_codes"] and item["source"]["html_sha256"]
    assert selected["allowed_bundles"] == []  # Cross-course codes do not become same-course pairs.
    assert pairing_for_primary(selected, "03005")["required_course_components"] == expected


@pytest.mark.parametrize("language", ["zh", "en"])
def test_explicit_lab_requirement_is_not_renamed_discussion(language):
    instruction = "Enrollment in lecture and discussion or lab (where offered), is required. Choose discussion or labs directly below lecture."
    envelope = _parse("philos_3_2026_fall", change=lambda html: _course_comment(
        html.replace(">Dis</td>", ">Lab</td>"), "30220", instruction))
    card = normalize_recommendation_card(_card(envelope, "30220"), language)
    assert card["pairing_selected"]["required_components"] == [{"types": ["Lab"], "min": 1, "max": 1}]
    html = _html(card, language)
    assert ("需配 Lab" if language == "zh" else "+ Lab required") in html
    assert "需配 Dis" not in html and "+ Dis required" not in html
    assert "No secondary required" not in html and "无需附加配对" not in html
    answer = render_recommendation_answer([card], language)
    assert "Lab" in answer
    assert "需选 Dis" not in answer and "require Dis" not in answer
