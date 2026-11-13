"""Known code spellings become evidence, without inventing course relations."""
from copy import deepcopy
import json

import pytest

from app.data import anteater, db
from app.data.websoc_payload import compact_comment_contexts, websoc_comment_metadata
from tests.test_anteater_payload_preservation import _swe43_response


def _resolve_pointer(value, pointer):
    for raw in pointer.split("/")[1:]:
        key = raw.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def test_public_swe43_comment_gains_inf_spelling_evidence_without_changing_policy_or_meetings():
    raw = _swe43_response()
    rows = [db._api_section_to_dict(section) for section in anteater._flatten_websoc_sections(raw["data"])]
    source = {"source": "api", "term": "2026 Fall", "sections": rows}
    before = deepcopy(source)
    wire = json.loads(json.dumps(compact_comment_contexts(source)))

    assert len(wire["course_code_normalizations"]) == 1  # repeated INF mentions share evidence
    note = wire["course_code_normalizations"][0]
    assert note["source_text"] == "INF 43"
    assert note["normalized_course_id"] == "IN4MATX 43"
    assert note["basis"] == "existing_department_alias_rules"
    assert note["source_path"].endswith("/course/comment")
    assert "Previously offered as INF 43" in _resolve_pointer(wire, note["source_path"])
    for original, section in zip(rows, wire["sections"]):
        assert wire["comment_contexts"][section["comment_context_id"]] == original["comments"]
        assert section["section_comment"] == original["section_comment"]
        assert section["meetings"] == original["meetings"]
    assert source == before
    assert compact_comment_contexts(wire) == wire
    assert not any(key in note for key in ("former_course_id", "equivalent_to", "may_take", "effective_term"))


def test_comment_metadata_without_sections_can_explain_existing_alias():
    raw = _swe43_response()["data"]
    raw["schools"][0]["departments"][0]["courses"][0]["sections"] = []
    source = {"term": "2026 Fall", "sections": [], **websoc_comment_metadata(raw)}
    wire = compact_comment_contexts(source)

    assert wire["sections"] == []
    assert wire["course_notices"] == source["course_notices"]
    assert wire["comment_contexts"] == source["comment_contexts"]
    assert wire["course_code_normalizations"][0]["normalized_course_id"] == "IN4MATX 43"


@pytest.mark.parametrize("comment", [None, "", "Previously offered as UNKNOWNDEPT 43.",
                                     "Previously offered as IN4MATX43.", "No policy supplied."])
def test_empty_unknown_or_already_canonical_comments_do_not_invent_aliases(comment):
    source = {"comment_contexts": {"one": {"course": {"comment": comment}}},
              "sections": [{"section_comment": None}]}
    assert "course_code_normalizations" not in compact_comment_contexts(source)


def test_each_comment_scope_and_excluded_nested_section_keeps_its_own_source_path():
    source = {
        "comment_contexts": {"school/key~one": {
            "school": {"comment": "Consult CS 161."},
            "department": {"comment": "INF43 students only.",
                           "course_number_range_comments": ["200-299: see INF 299."],
                           "section_code_range_comments": ["37500-37599: see ICS 33."]},
            "course": {"comment": "Formerly INF 43."},
        }},
        "courses": [{"sections": [], "excluded_section_evidence": [
            {"section_code": "37513", "selectable": False, "section_comment": "Co-class with INF 43.",
             "meetings": [{"days": "TBA", "timeIsTBA": True}]},
        ]}],
    }
    before = deepcopy(source)
    wire = compact_comment_contexts(source)
    notes = wire["course_code_normalizations"]

    assert len(notes) == 6
    assert all(note["source_text"] in _resolve_pointer(wire, note["source_path"]) for note in notes)
    assert any("school~1key~0one" in note["source_path"] for note in notes)
    assert any(note["source_path"].endswith("/excluded_section_evidence/0/section_comment") for note in notes)
    assert source == before
    assert wire["courses"] == source["courses"]
    assert wire["comment_contexts"] == source["comment_contexts"]
    assert compact_comment_contexts(wire) == wire


def test_alias_notes_are_rebuilt_from_comment_fields_not_arbitrary_text_or_supplied_metadata():
    forged = {"source_text": "FAKE43", "normalized_course_id": "SWE 43", "basis": "official"}
    source = {"title": "INF43", "description": "Previously offered as INF43.",
              "instructions": "INF43 equals SWE43", "course_code_normalizations": [forged],
              "sections": [{"section_comment": "Unknown department XYZ43.",
                            "course_code_normalizations": [forged]}]}
    wire = compact_comment_contexts(source)

    assert "course_code_normalizations" not in wire
    assert "course_code_normalizations" not in wire["sections"][0]
    assert wire["description"] == source["description"]
    assert source["course_code_normalizations"] == [forged]


@pytest.mark.parametrize("comment", [
    "ICS COURSES 5", "ICS enrollment 33", "INF courses43", "CS classes161",
    "INF offerings 43", "CS section 161", "I&C SCI COURSES 33",
])
def test_department_heading_and_intervening_words_are_not_course_spelling_evidence(comment):
    source = {"comment_contexts": {"one": {"school": {"comment": comment}}}}
    wire = compact_comment_contexts(source)
    assert "course_code_normalizations" not in wire
    assert wire["comment_contexts"] == source["comment_contexts"]


@pytest.mark.parametrize("comment, spelling, normalized", [
    ("INF43", "INF43", "IN4MATX 43"),
    ("INF 43", "INF 43", "IN4MATX 43"),
    ("for INF 43", "INF 43", "IN4MATX 43"),
    ("Students with prior credit for INF43.", "INF43", "IN4MATX 43"),
    ("CS161", "CS161", "COMPSCI 161"),
    ("Co-class with ICS33.", "ICS33", "I&C SCI 33"),
    ("ICS COURSES 5. Previously offered as INF 43.", "INF 43", "IN4MATX 43"),
])
def test_exact_known_department_alias_directly_before_number_remains_supported(comment, spelling, normalized):
    source = {"comment_contexts": {"one": {"course": {"comment": comment}}}}
    wire = compact_comment_contexts(source)
    assert len(wire["course_code_normalizations"]) == 1
    note = wire["course_code_normalizations"][0]
    assert note["source_text"] == spelling
    assert note["normalized_course_id"] == normalized
    assert spelling in _resolve_pointer(wire, note["source_path"])


def test_canonical_multiword_department_reference_is_preserved_without_alias_note():
    source = {"sections": [{"section_comment": "Co-class with I&C SCI33."}]}
    assert compact_comment_contexts(source) == source
