"""Actual public WebSoc responses produce evidence, never injected contracts."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

import pytest

from app.data import websoc_workflow as workflow
from app.data.websoc_pairing import resolve_pairing_references


FIXTURES = Path(__file__).parent / "fixtures" / "websoc" / "pairing"


def _fixture(stem="sociol_1_2026_fall"):
    meta = json.loads((FIXTURES / f"{stem}.json").read_text())
    body = (FIXTURES / f"{stem}.html").read_bytes()
    return body.decode(meta["response"]["encoding"]), meta


def _parse(stem="sociol_1_2026_fall", *, change=None, params=None, **overrides):
    html, meta = _fixture(stem)
    if change is not None:
        html = change(html)
    return workflow.parse_websoc_course_html(
        html,
        **{**{"term": meta["scope"]["term"], "department": meta["request"]["form"]["Dept"],
               "course_number": meta["request"]["form"]["CourseNum"], "source_url": workflow.WEBSOC_URL,
               "retrieved_at": meta["captured_at"], "request_form": params or meta["request"]["form"],
               "html_sha256": (hashlib.sha256(html.encode("utf-8")).hexdigest() if change is not None
                                 else meta["response"]["sha256"])}, **overrides},
    )


@pytest.mark.parametrize("stem", ["sociol_1_2026_fall", "philos_3_2026_fall"])
def test_raw_public_fixtures_have_reproducible_request_timestamp_and_byte_hash(stem):
    _, meta = _fixture(stem)
    data = (FIXTURES / f"{stem}.html").read_bytes()
    assert meta["response"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert meta["response"]["byte_count"] == len(data)
    assert meta["response"]["status_code"] == 200
    assert meta["captured_at"].startswith("2026-10-20")
    assert meta["request"]["form"]["CancelledCourses"] == "Include"
    assert meta["request"]["form"]["ClassType"] == "ALL"
    assert meta["request"]["form"]["ShowComments"] == "on"


def test_real_sociology_department_rule_produces_b_c_relationships_but_not_a_negation():
    result = _parse()
    assert result["ok"] is True and result["source"] == "registrar_websoc"
    assert len(result["sections"]) == 21
    pairing = result["pairing"]
    assert pairing["version"] == 2
    assert set(pairing["per_primary"]) == {"69000", "69020", "69040"}
    for code, first in [("69020", 69021), ("69040", 69041)]:
        primary = pairing["per_primary"][code]
        assert primary["requirement"] == "required"
        assert primary["required_components"] == [{"types": ["Dis"], "min": 1, "max": 1}]
        assert primary["relationship"] == "verified" and primary["relationship_complete"] is True
        assert primary["allowed_bundles"] == [[str(n)] for n in range(first, first + 9)]
        evidence = primary["evidence"]
        assert evidence["status"] == "verified" and evidence["scope"] == "department"
        assert evidence["course_id"] == "SOCIOL 1" and evidence["term"] == "2026 Fall"
        assert "directly below lecture" in evidence["quote"]
        assert evidence["html_sha256"] == result["source_proof"]["html_sha256"]
    missing = pairing["per_primary"]["69000"]
    assert missing["requirement"] == "required"
    assert missing["required_components"] == [{"types": ["Dis"], "min": 1, "max": 1}]
    assert missing["allowed_bundles"] == [] and missing["relationship_complete"] is False
    assert "secondary_group_missing" in missing["evidence"]["reason_codes"]


def test_real_17_column_table_preserves_capacity_enrollment_status_and_row_order():
    result = _parse()
    a, b = result["sections"][:2]
    assert (a["section_code"], a["max_capacity"], a["status"]) == ("69000", 400, "FULL")
    assert (b["section_code"], b["max_capacity"], b["status"]) == ("69020", 300, "OPEN")
    assert isinstance(b["enrolled"], int)
    assert b["seats_open"] == max(0, 300 - b["enrolled"])
    assert [row["section_code"] for row in result["source_proof"]["section_order"]] == [
        "69000", "69020", *map(str, range(69021, 69030)), "69040", *map(str, range(69041, 69050))]
    assert result["source_proof"]["full_unfiltered_request"] is True
    assert result["source_proof"]["response_document_complete"] is True


def test_real_single_primary_numeric_discussions_follow_the_confirmed_product_rule():
    result = _parse("philos_3_2026_fall")
    assert result["ok"] is True and len(result["sections"]) == 7
    assert [section["section_num"] for section in result["sections"]] == ["A", "1", "2", "3", "4", "5", "6"]
    primary = result["pairing"]["per_primary"]["30220"]
    assert primary["requirement"] == "required"
    assert primary["allowed_bundles"] == [[str(code)] for code in range(30221, 30227)]
    assert primary["relationship_complete"] is True
    assert primary["evidence"]["policy_source"] == "user_confirmed_product_policy"
    assert "quote" not in primary["evidence"]  # No fabricated school mandate.


def test_letters_colors_and_zero_units_are_not_used_as_proof():
    def change(html):
        html = re.sub(r'bgcolor="[^"]+"', 'bgcolor="#123456"', html, flags=re.I)
        html = re.sub(r'>B([1-9])</td>', lambda m: ">" + str(100 + int(m[1])) + "</td>", html)
        html = re.sub(r'>C([1-9])</td>', lambda m: ">" + str(200 + int(m[1])) + "</td>", html)
        html = html.replace('>0</td>', '>2</td>')
        return html
    original = _parse()["pairing"]["per_primary"]
    modified = _parse(change=change)["pairing"]["per_primary"]
    assert modified["69020"]["allowed_bundles"] == original["69020"]["allowed_bundles"]
    assert modified["69040"]["allowed_bundles"] == original["69040"]["allowed_bundles"]
    # Presence proves the product requirement; multiple-primary relationships
    # still need source semantics, even if letters look compatible.
    missing = _parse(change=lambda html: html.replace("Choose discussion or labs directly below lecture.", "Check the schedule."))
    assert all(item["requirement"] == "required" and item["relationship"] == "unknown"
               for item in missing["pairing"]["per_primary"].values())


@pytest.mark.parametrize("change", [
    {"ClassType": "DIS"}, {"CancelledCourses": "Exclude"}, {"ShowComments": ""},
    {"InstrName": "CARIAN"}, {"CourseCodes": "69040"}, {"Days": "MWF"},
    {"FullCourses": "SkipFull"}, {"Dept": "ECON"}, {"YearTerm": "2025-92"},
])
def test_filtered_or_wrong_identity_requests_never_claim_complete_relationships(change):
    _, meta = _fixture()
    params = {**meta["request"]["form"], **change}
    result = _parse(params=params)
    assert all(primary["relationship"] == "unknown" and not primary["relationship_complete"]
               for primary in result["pairing"]["per_primary"].values())


@pytest.mark.parametrize("overrides", [{"term": "2025 Fall"}, {"course_number": "2"}, {"department": "PHILOS"}])
def test_response_course_department_or_term_mismatch_does_not_establish_pairing(overrides):
    result = _parse(**overrides)
    assert result["ok"] is False and result["authoritative"] is False
    assert all(primary["requirement"] == "unknown" for primary in result["pairing"]["per_primary"].values())


def test_non_official_url_and_truncated_document_are_not_verified():
    wrong_url = _parse(source_url="https://example.org/perl/WebSoc")
    assert wrong_url["pairing"]["per_primary"]["69020"]["requirement"] == "unknown"
    partial = _parse(change=lambda html: html.split('69049</td>')[0])
    assert partial["source_proof"]["response_document_complete"] is False
    assert all(row["requirement"] == "unknown" for row in partial["pairing"]["per_primary"].values())


def test_unrelated_department_comment_and_mixed_component_cardinality_are_not_promoted():
    wrong_scope = _parse(change=lambda html: html.replace(
        'class="dept-title"><td colspan="17">Sociology', 'class="dept-title"><td colspan="17">Economics'))
    assert wrong_scope["pairing"]["per_primary"]["69020"]["requirement"] == "required"
    assert wrong_scope["pairing"]["per_primary"]["69020"]["relationship"] == "unknown"
    mixed = _parse(change=lambda html: html.replace('>69021</td><td  nowrap="nowrap">Dis', '>69021</td><td  nowrap="nowrap">Lab'))
    b = mixed["pairing"]["per_primary"]["69020"]
    assert b["requirement"] == "required" and "component_cardinality_unverified" in b["evidence"]["reason_codes"]
    assert b["required_components"] == [{"types": ["Dis"], "min": 1, "max": 1}]
    assert b["relationship"] == "unknown"


def test_course_optional_exception_is_preserved_and_blocks_department_generalization():
    def change(html):
        marker = '<tr valign="top" bgcolor="#FFFFCC"><td bgcolor="#D5E5FF"  nowrap="nowrap">69000'
        assert marker in html
        return html.replace(marker, '<tr class="course-comment"><td>Discussion is optional for this course.</td></tr>' + marker)
    result = _parse(change=change)
    assert all(row["requirement"] == "not_required" and row["evidence"]["status"] == "verified"
               for row in result["pairing"]["per_primary"].values())
    assert any(block["scope"] == "course" and "optional" in block["text"] for block in result["source_proof"]["comments"])


def test_exact_course_comment_can_supply_policy_without_a_department_generalization():
    policy = "Enrollment in lecture and discussion or lab (where offered), is required. Choose discussion or labs directly below lecture."

    def change(html):
        html = html.replace(policy, "Department course information.")
        marker = '<tr bgcolor="#E7E7E7" align="left">'
        assert marker in html
        return html.replace(marker, f'<tr class="course-comment"><td>{policy}</td></tr>' + marker, 1)

    result = _parse(change=change)
    b = result["pairing"]["per_primary"]["69020"]
    assert b["requirement"] == "required" and b["evidence"]["scope"] == "course"


def test_rule_and_rows_under_a_different_course_title_cannot_be_claimed_for_requested_course():
    result = _parse(change=lambda html: html.replace("Sociol &nbsp; 1 &nbsp;", "Sociol &nbsp; 2 &nbsp;"))
    assert result["pairing"]["per_primary"] == {}


def test_real_fetch_pipeline_produces_contract_from_http_html_not_a_test_injected_pairing():
    html, meta = _fixture()
    form, _ = _fixture("search_form_2026_10_20")
    raw = (FIXTURES / "sociol_1_2026_fall.html").read_bytes()
    calls = []

    class Response:
        status_code = 200
        url = workflow.WEBSOC_URL
        headers = {"content-type": "text/html; charset=ISO-8859-1"}

        def __init__(self, text, content=None):
            self.text = text
            self.content = content if content is not None else text.encode()

        def raise_for_status(self):
            return None

    class Session:
        def get(self, url, **_kwargs):
            return Response(form)

        def post(self, url, *, data, **_kwargs):
            calls.append(deepcopy(data))
            return Response(html, raw)

    result = workflow.fetch_websoc_course_offering(term="2026 Fall", department="SOCIOL", course_number="1", session=Session())
    assert calls[0]["ShowComments"] == "on" and calls[0]["CancelledCourses"] == "Include"
    assert calls[0]["ClassType"] == "ALL" and not calls[0]["FullCourses"] == "SkipFull"
    assert result["pairing"]["per_primary"]["69020"]["requirement"] == "required"
    assert result["source_proof"]["html_sha256"] == meta["response"]["sha256"]
    assert result["source_proof"]["request_form"] == calls[0]


def _course_comment(html, code, text):
    position = html.index(f">{code}</td>")
    row = html.rfind("<tr", 0, position)
    assert row >= 0
    return html[:row] + f'<tr class="course-comment"><td>{text}</td></tr>' + html[row:]


def _referenced_drama(text="Co-class PHILOS 3 is required."):
    return _parse("drama_10_2026_fall", change=lambda html: _course_comment(html, "03005", text))


def test_real_drama_lecture_only_is_not_required_without_claiming_a_school_mandate():
    result = _parse("drama_10_2026_fall")
    assert [section["section_code"] for section in result["sections"]] == ["03005"]
    rule = result["pairing"]["per_primary"]["03005"]
    assert rule["requirement"] == "not_required" and rule["evidence"]["status"] == "verified"
    assert rule["evidence"]["no_required_discussion"] is True
    for evidence in (rule["evidence"], result["pairing"]["evidence"]):
        assert evidence["policy_id"] == "user_confirmed_discussion_presence"
        assert evidence["policy_source"] == "user_confirmed_product_policy"
        assert evidence["policy_version"] == 1 and evidence["user_confirmed"] is True
    assert "quote" not in rule["evidence"]


def test_real_econ_single_primary_discussions_are_required_without_an_official_mandatory_sentence():
    result = _parse("econ_13_2026_fall")
    rule = result["pairing"]["per_primary"]["62040"]
    discussions = [section["section_code"] for section in result["sections"] if section["section_type"] == "Dis"]
    assert len(discussions) == 7
    assert rule["requirement"] == "required" and rule["relationship_complete"] is True
    assert rule["allowed_bundles"] == [[code] for code in discussions]
    assert "quote" not in rule["evidence"]


@pytest.mark.parametrize("kind", ["Sem", "Lab", "Stu"])
def test_complete_standalone_instructional_primary_is_not_automatically_unknown(kind):
    result = _parse("drama_10_2026_fall", change=lambda html: html.replace(">Lec</td>", f">{kind}</td>"))
    assert result["sections"][0]["section_type"] == kind
    assert result["pairing"]["per_primary"]["03005"]["requirement"] == "not_required"


@pytest.mark.parametrize("request_change", [{"ClassType": "LEC"}, {"CancelledCourses": "Exclude"},
    {"ShowComments": ""}, {"YearTerm": "2025-92"}])
def test_a_lecture_only_filtered_or_wrong_term_response_never_proves_absence(request_change):
    _, meta = _fixture("drama_10_2026_fall")
    result = _parse("drama_10_2026_fall", params={**meta["request"]["form"], **request_change})
    rule = result["pairing"]["per_primary"]["03005"]
    assert rule["requirement"] == "unknown"
    assert rule["evidence"]["no_required_discussion"] is None


def test_no_discussion_does_not_clear_a_lab_requirement_or_an_unknown_lab():
    lab = lambda html: html.replace(">Dis</td>", ">Lab</td>")
    unknown = _parse("philos_3_2026_fall", change=lab)
    rule = unknown["pairing"]["per_primary"]["30220"]
    assert rule["requirement"] == "unknown" and rule["evidence"]["no_required_discussion"] is True
    policy = "Enrollment in lecture and discussion or lab (where offered), is required. Choose discussion or labs directly below lecture."
    verified = _parse("philos_3_2026_fall", change=lambda html: _course_comment(lab(html), "30220", policy))
    rule = verified["pairing"]["per_primary"]["30220"]
    assert rule["requirement"] == "required" and rule["relationship_complete"] is True
    assert rule["required_components"] == [{"types": ["Lab"], "min": 1, "max": 1}]


def test_a_conditional_course_exception_is_not_an_unconditional_negative_or_positive():
    result = _parse("philos_3_2026_fall", change=lambda html: _course_comment(html, "30220",
        "Discussion is optional if the student has already completed an equivalent class."))
    assert result["pairing"]["per_primary"]["30220"]["requirement"] == "unknown"


def test_named_co_class_requires_lookup_and_external_discussions_never_become_same_course_bundles():
    source = _referenced_drama()
    original = deepcopy(source)
    pair, proof = source["pairing"], source["source_proof"]
    assert pair["evidence"]["needs_lookup"] is True
    assert proof["co_class_references"][0]["course_id"] == "PHILOS 3"
    assert pair["per_primary"]["03005"]["requirement"] == "unknown"
    result, audit = resolve_pairing_references(pair, proof, {"PHILOS 3": _parse("philos_3_2026_fall")})
    rule = result["per_primary"]["03005"]
    assert rule["requirement"] == "required" and rule["relationship"] == "unknown"
    assert rule["allowed_bundles"] == [] and rule["relationship_complete"] is False
    related = rule["required_course_components"][0]
    assert related["course_id"] == "PHILOS 3" and related["types"] == ["Dis"]
    assert related["source"]["html_sha256"] == audit["co_class_lookups"]["PHILOS 3"]["html_sha256"]
    assert result["evidence"]["needs_lookup"] is False
    assert source == original


@pytest.mark.parametrize("mutation", ["missing", "wrong_term", "partial", "wrong_course", "nested"])
def test_failed_partial_wrong_scope_or_nested_reference_cannot_prove_no_discussion(mutation):
    source = _referenced_drama()
    related = _parse("philos_3_2026_fall")
    if mutation == "missing":
        related = None
    elif mutation == "wrong_term":
        related["term"] = "2025 Fall"
    elif mutation == "partial":
        related["source_proof"]["full_unfiltered_request"] = False
    elif mutation == "wrong_course":
        related = _parse("econ_13_2026_fall")
    else:
        related["pairing"]["evidence"]["needs_lookup"] = True
    pair, _ = resolve_pairing_references(source["pairing"], source["source_proof"], {"PHILOS 3": related})
    rule = pair["per_primary"]["03005"]
    assert rule["requirement"] != "not_required"
    assert rule["relationship"] == "unknown" and pair["evidence"]["needs_lookup"] is True


def test_resolved_lecture_only_reference_can_clear_secondary_but_unknown_lab_cannot():
    source = _referenced_drama()
    without_dis = lambda html: re.sub(r'<tr\b[^>]*>(?:(?!<tr\b).)*>Dis</td>.*?</tr>', '', html, flags=re.S | re.I)
    related = _parse("philos_3_2026_fall", change=without_dis)
    assert len(related["sections"]) == 1
    pair, _ = resolve_pairing_references(source["pairing"], source["source_proof"], {"PHILOS 3": related})
    assert pair["per_primary"]["03005"]["requirement"] == "not_required"
    assert pair["per_primary"]["03005"]["evidence"]["status"] == "verified"
    lab = _parse("philos_3_2026_fall", change=lambda html: html.replace(">Dis</td>", ">Lab</td>"))
    pair, _ = resolve_pairing_references(source["pairing"], source["source_proof"], {"PHILOS 3": lab})
    assert pair["per_primary"]["03005"]["requirement"] == "unknown"


def test_unparseable_co_class_is_pending_but_an_ordinary_prerequisite_is_not_a_co_class():
    unresolved = _referenced_drama("See co-class schedule.")
    assert unresolved["pairing"]["per_primary"]["03005"]["requirement"] == "unknown"
    pair, _ = resolve_pairing_references(unresolved["pairing"], unresolved["source_proof"], {})
    assert pair["evidence"]["needs_lookup"] is True
    ordinary = _referenced_drama("Prerequisite: PHILOS 3.")
    assert ordinary["pairing"]["per_primary"]["03005"]["requirement"] == "not_required"
    assert ordinary["source_proof"]["co_class_references"] == []


def test_section_code_reference_needs_verified_code_to_course_resolution():
    source = _referenced_drama("Co-class code 30220 is required.")
    assert source["source_proof"]["co_class_references"][0]["section_code"] == "30220"
    related = _parse("philos_3_2026_fall")
    failed, _ = resolve_pairing_references(source["pairing"], source["source_proof"], {"code:30220": related})
    assert failed["per_primary"]["03005"]["requirement"] == "unknown"
    related["reference_section_code"] = "30220"
    result, _ = resolve_pairing_references(source["pairing"], source["source_proof"], {"code:30220": related})
    assert result["per_primary"]["03005"]["required_course_components"][0]["course_id"] == "PHILOS 3"
    related["reference_section_code"] = "30299"
    failed, _ = resolve_pairing_references(source["pairing"], source["source_proof"], {"code:30220": related})
    assert failed["per_primary"]["03005"]["requirement"] == "unknown"


def test_mixed_code_and_named_references_are_safe_and_deduplicate_required_course():
    source = _referenced_drama("Co-class code 30220 is required; co-class PHILOS 3 is required.")
    assert len(source["source_proof"]["co_class_references"]) == 2
    related = _parse("philos_3_2026_fall")
    related["reference_section_code"] = "30220"
    pair, proof = resolve_pairing_references(source["pairing"], source["source_proof"], {
        "code:30220": related, "PHILOS 3": related})
    assert len(pair["per_primary"]["03005"]["required_course_components"]) == 1
    assert len(proof["co_class_lookups"]) == 2
    failed, _ = resolve_pairing_references(pair, proof, {})
    assert failed["per_primary"]["03005"]["requirement"] == "unknown"
    assert "required_course_components" not in failed["per_primary"]["03005"]


def test_explicit_course_optional_comment_after_a_primary_still_applies_to_course():
    result = _parse(change=lambda html: _course_comment(html, "69021", "Discussion is optional for this course."))
    assert all(rule["requirement"] == "not_required" for rule in result["pairing"]["per_primary"].values())


@pytest.mark.parametrize("field", ["response_identity_validated", "response_document_complete",
    "full_unfiltered_request", "source_rows_fully_parsed"])
def test_valid_related_source_cannot_promote_an_untrusted_requesting_course(field):
    source = _referenced_drama()
    source["source_proof"][field] = False
    result, _ = resolve_pairing_references(source["pairing"], source["source_proof"], {
        "PHILOS 3": _parse("philos_3_2026_fall")})
    rule = result["per_primary"]["03005"]
    assert rule["requirement"] == "unknown" and rule["evidence"]["status"] == "unverified"
    assert "required_course_components" not in rule
    assert rule["allowed_bundles"] == []


def test_real_form_fixture_supports_both_public_production_queries_without_options_injection():
    html, meta = _fixture("search_form_2026_10_20")
    data = (FIXTURES / "search_form_2026_10_20.html").read_bytes()
    assert hashlib.sha256(data).hexdigest() == meta["response"]["sha256"]
    parsed = workflow._parse_websoc_form(html)
    assert "2026-92" in parsed.terms
    assert "SOCIOL" in parsed.departments and "PHILOS" in parsed.departments
