"""Apply the confirmed discussion policy to complete official course snapshots.

Official rows establish presence, absence and identities. The requirement from
discussion presence is a user-confirmed product rule, not an attributed school
mandate. Multiple-primary relationships still need explicit source evidence.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
import re
from urllib.parse import urlsplit

from app.catalog.term import Term
from app.catalog.departments import resolve_department
from app.catalog.normalization import iter_course_mentions, parse_course_mention


RULE_ID = "registrar_required_directly_below_v1"
POLICY_ID = "user_confirmed_discussion_presence"
POLICY_VERSION = 1
POLICY_MARKER = {"policy_id": POLICY_ID, "policy_version": POLICY_VERSION,
                 "policy_source": "user_confirmed_product_policy", "user_confirmed": True}
_PRIMARY = {"lec", "lecture", "sem", "seminar"}
_STANDALONE = {"lab", "stu", "act", "tut", "fld"}
_POLICY = re.compile(
    r"Enrollment\s+in\s+lecture\s+and\s+discussion\s+or\s+lab\s*"
    r"\(\s*where\s+offered\s*\)\s*,?\s*is\s+required\s*\.\s*"
    r"Choose\s+discussion\s+or\s+labs\s+directly\s+below\s+lecture\s*\.", re.I
)
_EXCEPTION = re.compile(
    r"(?:discussion|labs?|co[- ]?classes?).{0,80}(?:optional|not\s+required|unless|except)"
    r"|(?:optional|not\s+required|unless|except).{0,80}(?:discussion|labs?|co[- ]?classes?)", re.I
)
_REFERENCE_TRIGGER = re.compile(
    r"\b(?:co[- ]?class(?:es)?|co[- ]?requisites?|concurrent(?:ly)?|same\s+as|"
    r"cross[- ]listed|also\s+listed\s+as)\b", re.I)
_OPTIONAL_DISCUSSION = re.compile(
    r"^\s*discussions?\s+(?:is|are)\s+(?:optional|not\s+required)"
    r"(?:\s+for\s+(?:this|the)\s+course)?\s*[.!]?\s*$", re.I)


def _co_class_references(comments, course_id, term):
    references, unresolved = [], False
    for block in comments:
        if not block.get("scope_confirmed"):
            continue
        text = block.get("text") or ""
        if block.get("scope") != "course" and not any(
                ref.display() == course_id for ref, _, _ in iter_course_mentions(text)):
            continue
        for clause in re.split(r"(?<=[.!?])\s+|;\s*", text):
            if not _REFERENCE_TRIGGER.search(clause):
                continue
            mentions = iter_course_mentions(clause)
            # A prerequisite mixed into the same clause is not proof that all
            # named courses must be taken concurrently.
            if re.search(r"\bprerequisites?\b|previously|already\s+(?:passed|completed)", clause, re.I):
                unresolved = True
                continue
            external = [ref.display() for ref, _, _ in mentions if ref.display() != course_id]
            codes = re.findall(r"(?<![A-Za-z0-9])\d{5}(?![A-Za-z0-9])", clause)
            if not external and not mentions and not codes:
                unresolved = True
            for cid in external:
                if not any(ref.get("course_id") == cid for ref in references):
                    references.append({"course_id": cid, "term": term, "quote": clause.strip(), "relation": "co_class"})
            for code in codes:
                if not any(ref.get("section_code") == code for ref in references):
                    references.append({"section_code": code, "term": term, "quote": clause.strip(), "relation": "co_class"})
    return references, unresolved


def _optional_discussion(comments):
    return next((block for block in comments if block.get("scope") == "course" and block.get("scope_confirmed")
                 and any(_OPTIONAL_DISCUSSION.fullmatch(sentence) for sentence in
                         re.split(r"(?<=[.!?])\s+|;\s*", block.get("text") or ""))), None)


def _key(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _course_title(cells, classes):
    for text, css in zip(cells, classes):
        if "coursetitle" not in str(css).lower():
            continue
        match = re.match(r"^\s*(.*?)\s+([A-Z]?\d+[A-Z]{0,3})\s+", text.upper())
        if match:
            return _key(match.group(1)), match.group(2)
    return None


def _full_request(request, department, number, term):
    if not isinstance(request, Mapping):
        return False
    parsed = Term.parse(term)
    quarters = {"Winter": "03", "Spring": "14", "Summer": "76", "Fall": "92"}
    expected_term = f"{parsed.year}-{quarters[parsed.quarter]}" if parsed else None
    if (request.get("YearTerm") != expected_term or _key(request.get("Dept")) != _key(department)
            or str(request.get("CourseNum") or "").strip().upper() != number
            or request.get("ClassType") != "ALL" or request.get("CancelledCourses") != "Include"
            or str(request.get("ShowComments") or "").lower() not in {"on", "1", "true"}):
        return False
    if request.get("Breadth", "ANY") not in {"", "ANY"} or request.get("FullCourses", "ANY") not in {"", "ANY"}:
        return False
    return not any(request.get(field) for field in (
        "CourseCodes", "InstrName", "CourseTitle", "Units", "Days", "StartTime", "EndTime",
        "MaxCap", "Bldg", "Room", "Online", "Modality",
    )) and request.get("Division", "ANY") in {"", "ANY"}


def produce_websoc_pairing(rows, *, department, course_number, term, source_url,
                            retrieved_at, html_sha256, request_form=None, validation_ok=True,
                            parsed_section_codes=(), document_complete=False):
    """Return a v2 contract and audit proof from original ordered table rows.

    Each source row has ``index``, ``class``, ``cells`` and ``cell_classes``.
    Presence/absence requires an unfiltered ALL/Include request, validated
    identity, complete document and fully parsed rows. A lone primary maps
    all same-course discussions; multiple primaries need relationship proof.
    """
    number = str(course_number or "").strip().upper()
    parsed = Term.parse(term)
    canonical_term = f"{parsed.year} {parsed.quarter}" if parsed else term
    course_id = f"{department} {number}"
    source = {"source": "registrar_websoc", "course_id": course_id, "term": canonical_term,
              "source_url": source_url, "retrieved_at": retrieved_at, "html_sha256": html_sha256, **POLICY_MARKER}
    full_request = _full_request(request_form, department, number, term)
    try:
        url = urlsplit(source_url)
        official_source = (url.scheme == "https" and url.hostname in {
            "www.reg.uci.edu", "reg.uci.edu", "websoc.reg.uci.edu"} and url.path == "/perl/WebSoc")
    except (TypeError, ValueError):
        official_source = False
    comments, ordered, groups = [], [], []
    department_comments = []
    course_comments = []
    active_course = None
    current = None
    malformed = False
    department_scope_verified = False
    for row in rows:
        css = str(row.get("class") or "").lower()
        cells = row.get("cells") or []
        classes = row.get("cell_classes") or []
        text = " ".join(str(cell) for cell in cells).strip()
        if "dept-title" in css or "college-title" in css:
            department_comments = []
            course_comments = []
            active_course = current = None
            department_scope_verified = ("dept-title" in css
                and _key(resolve_department(text) or text) == _key(department))
        if "dept-comment" in css:
            block = {"scope": "department", "scope_confirmed": department_scope_verified,
                     "row_index": row["index"], "text": text}
            department_comments.append(block)
            comments.append(block)
        title = _course_title(cells, classes)
        if title:
            if current is not None:
                current["boundary_row_index"] = row["index"]
            active_course = title == (_key(department), number)
            current = None
            course_comments = []
            continue
        if "blue-bar" in css:
            if current is not None:
                current["boundary_row_index"] = row["index"]
            active_course = current = None
            continue
        if not active_course:
            continue
        if cells and re.fullmatch(r"\d{5}", str(cells[0]).strip()):
            if len(cells) < 4:
                malformed = True
                continue
            code, kind, section, units = (str(cell).strip() for cell in cells[:4])
            kind = kind.lower()
            item = {"row_index": row["index"], "course_id": course_id, "section_code": code,
                    "section_type": kind, "section_num": section, "units": units}
            ordered.append(item)
            if kind in _PRIMARY:
                current = {"primary": item, "secondaries": [], "comments": deepcopy(department_comments + course_comments),
                           "boundary_row_index": None}
                if groups:
                    groups[-1]["boundary_row_index"] = row["index"]
                groups.append(current)
            elif current is not None:
                current["secondaries"].append(item)
            continue
        is_header = cells and str(cells[0]).strip().lower() == "code"
        if text and not is_header and ("comment" in css or len(cells) <= 3):
            # Same-course comments may narrow or contradict a department rule.
            block = {"scope": "course", "scope_confirmed": True, "row_index": row["index"], "text": text}
            comments.append(block)
            if current is not None:
                current["comments"].append(block)
            else:
                # A pre-section course comment applies to every following group.
                course_comments.append(block)
    if not groups:
        standalone = [row for row in ordered if row["section_type"] in _STANDALONE]
        for index, primary in enumerate(standalone):
            groups.append({"primary": primary, "secondaries": [],
                "comments": deepcopy([block for block in comments if block.get("scope_confirmed")]),
                "boundary_row_index": standalone[index + 1]["row_index"] if index + 1 < len(standalone) else
                    (rows[-1]["index"] if document_complete and rows else None)})
    codes = [section["section_code"] for section in ordered]
    duplicate = any(count > 1 for count in Counter(codes).values())
    parsed_codes = set(str(code) for code in parsed_section_codes)
    fully_parsed = not malformed and not duplicate and all(code in parsed_codes for code in codes)
    proof = {"version": 1, **source, "request_method": "POST" if request_form is not None else None,
             "request_form": deepcopy(dict(request_form)) if isinstance(request_form, Mapping) else None,
             "response_identity_validated": validation_ok is True,
             "response_document_complete": document_complete is True,
             "full_unfiltered_request": full_request, "source_rows_fully_parsed": fully_parsed,
             "comments": comments, "section_order": ordered}
    discussions = [row for row in ordered if row["section_type"] in {"dis", "discussion"}]
    all_secondary = [row for row in ordered if row["section_type"] not in _PRIMARY
                     and row["section_code"] not in {group["primary"]["section_code"] for group in groups}]
    base_reasons = []
    if not validation_ok:
        base_reasons.append("response_identity_unverified")
    if not official_source:
        base_reasons.append("official_source_unverified")
    if not document_complete:
        base_reasons.append("source_document_incomplete")
    if not full_request:
        base_reasons.append("source_request_incomplete")
    if not fully_parsed:
        base_reasons.append("source_rows_incomplete")
    per_primary, base_rules, all_references = {}, {}, []
    unresolved_any = False
    for group in groups:
        primary = group["primary"]
        # Explicit course comments apply throughout this course, including a
        # note printed after its first teaching row. Other courses were never
        # collected while active_course was false.
        scoped_comments = group["comments"] + [block for block in comments
            if block.get("scope") == "course" and block not in group["comments"]]
        reasons = list(base_reasons)
        policy = next((block for block in reversed(scoped_comments)
                       if block["scope_confirmed"] and _POLICY.search(block["text"])), None)
        contradictory = any(_EXCEPTION.search(block["text"]) for block in scoped_comments)
        optional = _optional_discussion(scoped_comments)
        references, unresolved_reference = _co_class_references(scoped_comments, course_id, canonical_term)
        for ref in references:
            if ref not in all_references:
                all_references.append(ref)
        unresolved_any |= unresolved_reference
        secondaries = group["secondaries"]
        kinds = {row["section_type"] for row in secondaries}
        requirement, components, relationship, bundles = "unknown", [], "unknown", []
        if not base_reasons:
            if optional is not None:
                if any(row["section_type"] not in {"dis", "discussion"} for row in all_secondary):
                    reasons.append("other_component_requirement_unverified")
                else:
                    requirement = "not_required"
            elif any(block.get("scope") == "course" and _EXCEPTION.search(block.get("text") or "")
                     for block in scoped_comments):
                reasons.append("scoped_pairing_exception_unresolved")
            elif discussions:
                requirement, components = "required", [{"types": ["Dis"], "min": 1, "max": 1}]
                if contradictory:
                    reasons.append("scoped_pairing_exception_unresolved")
                elif any(row["section_type"] not in {"dis", "discussion"} for row in all_secondary):
                    reasons.append("component_cardinality_unverified")
                elif len(groups) == 1:
                    relationship = "verified"
                    bundles = [[row["section_code"]] for row in discussions]
                elif policy is not None and group["boundary_row_index"] is not None and kinds in ({"dis"}, {"discussion"}):
                    relationship = "verified"
                    bundles = [[row["section_code"]] for row in secondaries]
                else:
                    reasons.append("pairing_relationship_unknown")
                    if not secondaries:
                        reasons.append("secondary_group_missing")
            elif all_secondary:
                if (policy is not None and not contradictory and group["boundary_row_index"] is not None
                        and kinds == {"lab"}):
                    requirement, components, relationship = "required", [{"types": ["Lab"], "min": 1, "max": 1}], "verified"
                    bundles = [[row["section_code"]] for row in secondaries]
                else:
                    reasons.append("other_component_requirement_unverified")
            elif contradictory:
                reasons.append("scoped_pairing_exception_unresolved")
            else:
                requirement = "not_required"
        base = {"requirement": requirement, "required_components": components, "relationship": relationship,
                "relationship_complete": relationship == "verified", "allowed_bundles": bundles}
        if references or unresolved_reference:
            base_rules[primary["section_code"]] = deepcopy(base)
            if requirement == "not_required":
                requirement = "unknown"
            relationship, bundles = "unknown", []
            reasons.append("co_class_lookup_required" if references else "co_class_reference_unresolved")
        evidence = {**source, "status": "verified" if not base_reasons and requirement != "unknown" else "unverified",
                    "reason_codes": reasons, "source_row_index": primary["row_index"],
                    "boundary_row_index": group["boundary_row_index"],
                    "discussion_present": bool(discussions),
                    "no_required_discussion": (not discussions or optional is not None) if not base_reasons else None,
                    "co_class_references": references, "needs_lookup": bool(references) or unresolved_reference}
        if policy is not None:
            evidence.update(rule_id=RULE_ID, scope=policy["scope"], policy_row_index=policy["row_index"],
                            quote=_POLICY.search(policy["text"]).group(0))
        if optional is not None:
            evidence.update(scope="course", optional_discussion_quote=optional["text"])
        per_primary[primary["section_code"]] = {
            "requirement": requirement, "required_components": components,
            "relationship": relationship, "relationship_complete": relationship == "verified",
            "allowed_bundles": bundles,
            "evidence": evidence,
        }
    reference_state = {"co_class_references": all_references, "needs_lookup": bool(all_references) or unresolved_any}
    proof.update(reference_state, co_class_base_rules=base_rules, **POLICY_MARKER)
    return {"version": 2, "per_primary": per_primary, "evidence": {**source, **reference_state}}, proof


def _reference_key(reference):
    return f"code:{reference['section_code']}" if reference.get("section_code") else reference.get("course_id")


def _verified_reference(reference, result, term):
    if not isinstance(result, Mapping):
        return None
    proof = result.get("source_proof")
    if not isinstance(proof, Mapping):
        return None
    ref = parse_course_mention(str(proof.get("course_id") or ""))
    target = Term.parse(term)
    if (not ref or not target or Term.parse(reference.get("term")) != target
            or not proof.get("html_sha256") or result.get("ok") is not True or result.get("authoritative") is not True
            or result.get("source") != "registrar_websoc" or result.get("offering_status") != "offered"
            or Term.parse(result.get("term")) != target or Term.parse(proof.get("term")) != target
            or proof.get("source") != "registrar_websoc"
            or result.get("department") != ref.department or result.get("course_number") != ref.course_number
            or not all(proof.get(field) is True for field in ("response_identity_validated",
                "response_document_complete", "full_unfiltered_request", "source_rows_fully_parsed"))):
        return None
    try:
        url = urlsplit(proof.get("source_url") or "")
    except (TypeError, ValueError):
        return None
    if (url.scheme != "https" or url.hostname not in {"www.reg.uci.edu", "reg.uci.edu", "websoc.reg.uci.edu"}
            or url.path != "/perl/WebSoc"):
        return None
    rows = proof.get("section_order")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, Mapping) for row in rows):
        return None
    if reference.get("section_code"):
        code = reference["section_code"]
        if result.get("reference_section_code") != code or not any(row.get("section_code") == code for row in rows):
            return None
    elif reference.get("course_id") != ref.display():
        return None
    return ref.display(), proof, rows


def resolve_pairing_references(pairing, source_proof, lookups_by_course):
    """Merge same-term official reference snapshots; perform no lookup or guessing."""
    out, proof = deepcopy(pairing), deepcopy(source_proof)
    lookups = lookups_by_course if isinstance(lookups_by_course, Mapping) else {}
    target = (out.get("evidence") or {}).get("term")
    audit, pending_any = {}, False
    bases = proof.get("co_class_base_rules") or {}
    try:
        own_url = urlsplit(proof.get("source_url") or "")
        own_source_valid = (own_url.scheme == "https" and own_url.hostname in {
            "www.reg.uci.edu", "reg.uci.edu", "websoc.reg.uci.edu"} and own_url.path == "/perl/WebSoc"
            and proof.get("source") == "registrar_websoc" and bool(proof.get("html_sha256"))
            and proof.get("course_id") == (out.get("evidence") or {}).get("course_id")
            and Term.parse(proof.get("term")) == Term.parse(target) and Term.parse(target) is not None
            and all(proof.get(field) is True for field in ("response_identity_validated",
                "response_document_complete", "full_unfiltered_request", "source_rows_fully_parsed")))
    except (TypeError, ValueError):
        own_source_valid = False
    if not own_source_valid:
        for rule in out.get("per_primary", {}).values():
            rule.pop("required_course_components", None)
            rule.update(requirement="unknown", required_components=[], relationship="unknown",
                        relationship_complete=False, allowed_bundles=[])
            rule["evidence"].update(status="unverified", reason_codes=list(dict.fromkeys(
                [*rule["evidence"].get("reason_codes", []), "co_class_source_unverified"])))
        return out, proof
    for code, rule in out.get("per_primary", {}).items():
        evidence = rule["evidence"]
        references = evidence.get("co_class_references") or []
        if not references and not evidence.get("needs_lookup"):
            continue
        base = deepcopy(bases.get(code) or {key: rule[key] for key in (
            "requirement", "required_components", "relationship", "relationship_complete", "allowed_bundles")})
        pending = not references or "co_class_reference_unresolved" in evidence.get("reason_codes", [])
        related = []
        for reference in references:
            key = _reference_key(reference)
            result = lookups.get(key)
            verified = _verified_reference(reference, result, target)
            if verified is None:
                audit[key] = {"status": "unavailable", "reference": deepcopy(reference)}
                pending = True
                continue
            cid, ref_proof, rows = verified
            nested = (result.get("pairing") or {}).get("evidence", {}).get("needs_lookup") is True
            dis_codes = [row["section_code"] for row in rows if row.get("section_type") in {"dis", "discussion"}]
            audit[key] = {"status": "verified", "course_id": cid, "term": target,
                "source_url": ref_proof.get("source_url"), "html_sha256": ref_proof.get("html_sha256"),
                "retrieved_at": ref_proof.get("retrieved_at"), "discussion_codes": dis_codes,
                "reference": deepcopy(reference)}
            if cid == (out.get("evidence") or {}).get("course_id"):
                continue
            if nested:
                pending = True
            if dis_codes and not any(component["course_id"] == cid for component in related):
                related.append({"course_id": cid, "term": target, "types": ["Dis"], "min": 1, "max": 1,
                    "section_codes": dis_codes, "source": {key: ref_proof.get(key) for key in
                        ("source", "course_id", "term", "source_url", "retrieved_at", "html_sha256")}})
            elif any(row.get("section_type") not in _PRIMARY for row in rows):
                # Absence of Dis does not clear unresolved Lab/other components.
                pending = True
        rule.pop("required_course_components", None)
        rule.update(base)
        reasons = [reason for reason in evidence.get("reason_codes", [])
                   if reason not in {"co_class_lookup_required", "co_class_lookup_unverified", "cross_course_relationship_unknown"}]
        if pending:
            if rule["requirement"] == "not_required":
                rule["requirement"] = "unknown"
            rule.update(relationship="unknown", relationship_complete=False, allowed_bundles=[])
            reasons.append("co_class_lookup_unverified")
        if related:
            rule.update(requirement="required", relationship="unknown", relationship_complete=False, allowed_bundles=[],
                        required_course_components=related)
            if not rule["required_components"]:
                rule["required_components"] = [{"types": ["Dis"], "min": 1, "max": 1}]
            reasons.append("cross_course_relationship_unknown")
        evidence.update(needs_lookup=pending, co_class_lookups=[deepcopy(audit[_reference_key(ref)])
                        for ref in references if _reference_key(ref) in audit],
                        reason_codes=list(dict.fromkeys(reasons)),
                        status="verified" if rule["requirement"] != "unknown" and evidence.get("status") == "verified" else "unverified")
        if (rule["requirement"] != "unknown" and not any(reason in evidence["reason_codes"] for reason in (
                "response_identity_unverified", "official_source_unverified", "source_document_incomplete",
                "source_request_incomplete", "source_rows_incomplete"))):
            evidence["status"] = "verified"
        pending_any |= pending
    out["evidence"]["needs_lookup"] = pending_any
    proof.update(needs_lookup=pending_any, co_class_lookups=audit)
    return out, proof
