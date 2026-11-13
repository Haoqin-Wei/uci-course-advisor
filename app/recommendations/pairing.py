"""Pure pairing evidence and selection checks; never infer rules from timetable shape."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from app.catalog.departments import DEPARTMENT_ALIASES

VERIFIED_SOURCES = frozenset({"uci_catalog", "uci_websoc", "registrar_websoc", "official_uci",
                              "verified_course_pairing", "registrar_course_pairing"})
_TYPES = {"lec": "Lec", "lecture": "Lec", "sem": "Sem", "seminar": "Sem",
          "dis": "Dis", "discussion": "Dis", "lab": "Lab", "laboratory": "Lab",
          "stu": "Stu", "studio": "Stu", "act": "Act", "activity": "Act",
          "tut": "Tut", "tutorial": "Tut", "fld": "Fld", "field": "Fld"}
_SECONDARY = frozenset({"Dis", "Lab", "Stu", "Act", "Tut", "Fld"})
DISCUSSION_POLICY_ID = "user_confirmed_discussion_presence"
DISCUSSION_POLICY_VERSION = 1
_COURSE_PREFIXES = sorted((("".join(alias.upper().split()), "".join(department.split()))
                          for department, aliases in DEPARTMENT_ALIASES.items()
                          for alias in (department, *aliases)), key=lambda row: len(row[0]), reverse=True)


def contract_enabled():
    from app import config
    return config.dynamic_prefilter_enabled() or config.recommendation_ranking_enabled()


def discussion_policy_current(pairing):
    """Unknown outcomes can still belong to the current product policy.

    This version check only expires old decisions; it does not authenticate a
    source, prove an absence, or promote an unavailable lookup to not_required.
    """
    evidence = pairing.get("evidence") if isinstance(pairing, Mapping) else None
    return (isinstance(evidence, Mapping)
            and evidence.get("policy_id") == DISCUSSION_POLICY_ID
            and evidence.get("policy_version") == DISCUSSION_POLICY_VERSION
            and evidence.get("user_confirmed") is True)


def _term(value):
    from app.terms import parse_term_key
    parsed = parse_term_key(value)
    return parsed.terms[0].canonical_name if parsed.kind == "single" else None


def _code(section):
    return str(section.get("section_code") or section.get("code") or "").strip()


def _kind(section):
    return _TYPES.get(str(section.get("section_type") or section.get("type") or "").strip().lower())


def _course(value):
    compact = "".join(str(value or "").upper().replace("_", " ").split())
    for prefix, canonical in _COURSE_PREFIXES:
        if compact.startswith(prefix):
            rest = compact[len(prefix):]
            if rest and (rest[0].isdigit() or len(rest) > 1 and rest[0].isalpha() and rest[1].isdigit()):
                return canonical + rest
    return compact or None


def _normalize_v1(item, *, term=None):
    """Read a server-owned v1 contract; legacy booleans and letters are not proof.

    Adapters must supply explicit course/term rules with a verified source. This
    function must not be fed client-authored evidence or tool arguments.
    allowed_bundles enumerates permitted complete combinations for each listed
    primary. An unlisted primary has an unknown relationship, not a denial.
    """
    item = item if isinstance(item, Mapping) else {}
    raw = item.get("pairing") if isinstance(item.get("pairing"), Mapping) else {}
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), Mapping) else {}
    target = _term(term or item.get("term"))
    source = evidence.get("source")
    course = _course(evidence.get("course_id"))
    reasons = []
    verified = (raw.get("version") == 1 and evidence.get("status") == "verified"
                and isinstance(source, str) and source in VERIFIED_SOURCES
                and course is not None and (not item.get("course_id") or _course(item["course_id"]) == course)
                and target is not None and _term(evidence.get("term")) == target)
    requirement = raw.get("requirement") if verified else "unknown"
    raw_types = raw.get("required_types", [])
    invalid_types = not isinstance(raw_types, list) or any(not isinstance(value, str) for value in raw_types)
    types = list(dict.fromkeys(_TYPES.get(value.lower()) for value in raw_types if isinstance(value, str))) if isinstance(raw_types, list) else []
    if requirement not in {"required", "not_required", "unknown"}:
        requirement = "unknown"
    if invalid_types:
        requirement = "unknown"
        reasons.append("required_components_unverified")
    if requirement == "required" and (not types or any(value not in _SECONDARY for value in types)):
        requirement = "unknown"
        reasons.append("required_components_unverified")
    if requirement == "not_required" and types:
        requirement = "unknown"
        reasons.append("pairing_evidence_conflict")
    if requirement == "unknown":
        reasons.append("secondary_pairing_unknown")
    bundles = []
    if verified and requirement == "required":
        for bundle in raw.get("allowed_bundles", []) if isinstance(raw.get("allowed_bundles"), list) else []:
            if not isinstance(bundle, Mapping) or not bundle.get("primary_code"):
                continue
            secondaries = bundle.get("secondary_codes")
            if not isinstance(secondaries, list) or not secondaries or any(not isinstance(code, str) or not code.strip() for code in secondaries):
                continue
            bundles.append({"primary_code": str(bundle["primary_code"]),
                            "secondary_codes": list(dict.fromkeys(code.strip() for code in secondaries))})
    return {"version": 1, "requirement": requirement,
            "required_types": types if requirement == "required" else [], "allowed_bundles": bundles,
            "evidence": {"status": "verified" if verified and requirement != "unknown" else "unverified",
                         "source": source if isinstance(source, str) else None, "term": target, "course_id": course,
                         "reason_codes": list(dict.fromkeys(reasons))}}


def _verify_v1(pairing, selected_sections, *, primary_code=None):
    """Check all compulsory components and explicit relationships, not registration."""
    pairing = pairing if isinstance(pairing, Mapping) else {}
    pairing = normalize_pairing_evidence({"pairing": pairing, "term": (pairing.get("evidence") or {}).get("term"),
                                         "course_id": (pairing.get("evidence") or {}).get("course_id")})
    sections = [section for section in selected_sections or [] if isinstance(section, Mapping)]
    declared_primaries = {row["primary_code"] for row in pairing["allowed_bundles"]}
    primaries = [section for section in sections if _kind(section) in {"Lec", "Sem"} or _code(section) in declared_primaries]
    if primary_code is None and len(primaries) == 1:
        primary_code = _code(primaries[0])
    if primary_code is None and pairing["requirement"] == "not_required" and len(sections) == 1:
        primary_code = _code(sections[0])
    primary_code = str(primary_code) if primary_code is not None else None
    secondaries = [section for section in sections if _code(section) != primary_code]
    result = {"status": "unknown", "primary_code": primary_code,
              "selected_secondary_codes": [_code(section) for section in secondaries if _code(section)], "reason_codes": []}
    if pairing["requirement"] == "unknown":
        result["reason_codes"] = ["secondary_pairing_unknown"]
        return result
    if not primary_code or not any(_code(section) == primary_code for section in sections):
        result.update(status="unsatisfied", reason_codes=["primary_not_selected"])
        return result
    if len(primaries) > 1:
        result.update(status="unsatisfied", reason_codes=["multiple_primary_sections"])
        return result
    if pairing["requirement"] == "not_required":
        if secondaries:
            result["reason_codes"] = ["additional_component_unverified"]
        else:
            result["status"] = "satisfied"
        return result
    present = {_kind(section) for section in secondaries}
    if None in present:
        result["reason_codes"] = ["component_type_unknown"]
        return result
    if not set(pairing["required_types"]).issubset(present):
        result.update(status="unsatisfied", reason_codes=["required_component_missing"])
        return result
    allowed = [set(row["secondary_codes"]) for row in pairing["allowed_bundles"] if row["primary_code"] == primary_code]
    if not allowed:
        result["reason_codes"] = ["pairing_relationship_unknown"]
    elif set(result["selected_secondary_codes"]) in allowed:
        result["status"] = "satisfied"
    else:
        result.update(status="unsatisfied", reason_codes=["incompatible_secondary_selection"])
    return result


def _available_v1(pairing, sections):
    """Return proven complete available bundles; unknown evidence never excludes."""
    if pairing["requirement"] == "unknown":
        return list(sections), "unknown"
    if pairing["requirement"] == "not_required":
        return list(sections), "satisfied"
    by_code = {_code(section): section for section in sections if isinstance(section, Mapping) and _code(section)}
    retained, unknown_primary = set(), set()
    known_primaries = {row["primary_code"] for row in pairing["allowed_bundles"]}
    for section in sections:
        if isinstance(section, Mapping) and _kind(section) in {"Lec", "Sem"} and _code(section) not in known_primaries:
            unknown_primary.add(_code(section))
    for bundle in pairing["allowed_bundles"]:
        codes = [bundle["primary_code"], *bundle["secondary_codes"]]
        if all(code in by_code for code in codes) and verify_selection(
                pairing, [by_code[code] for code in codes], primary_code=bundle["primary_code"])["status"] == "satisfied":
            retained.update(codes)
    if unknown_primary:
        return list(sections), "unknown"
    return [section for section in sections if _code(section) in retained], "satisfied" if retained else "unsatisfied"


def _components(raw):
    if not isinstance(raw, list) or not raw:
        return None
    normalized, used_types = [], set()
    for group in raw:
        if not isinstance(group, Mapping) or not isinstance(group.get("types"), list) or not group["types"]:
            return None
        types = [_TYPES.get(value.lower()) if isinstance(value, str) else None for value in group["types"]]
        minimum, maximum = group.get("min"), group.get("max")
        if (any(kind not in _SECONDARY for kind in types) or len(set(types)) != len(types)
                or used_types.intersection(types) or isinstance(minimum, bool) or isinstance(maximum, bool)
                or not isinstance(minimum, int) or not isinstance(maximum, int)
                or not 0 <= minimum <= maximum or maximum == 0):
            return None
        normalized.append({"types": types, "min": minimum, "max": maximum})
        used_types.update(types)
    return normalized if any(group["min"] > 0 for group in normalized) else None


def _normalize_v2(item, raw, *, term=None, primary_code=None):
    shared = raw.get("evidence") if isinstance(raw.get("evidence"), Mapping) else {}
    target = _term(term or item.get("term") or raw.get("term") or shared.get("term"))
    course = _course(item.get("course_id") or raw.get("course_id") or shared.get("course_id"))
    shared_scope_ok = ((not shared.get("course_id") or _course(shared["course_id"]) == course)
                       and (not shared.get("term") or _term(shared["term"]) == target)
                       and (not raw.get("course_id") or _course(raw["course_id"]) == course)
                       and (not raw.get("term") or _term(raw["term"]) == target))
    rules = {}
    for code, value in (raw.get("per_primary") or {}).items() if isinstance(raw.get("per_primary"), Mapping) else []:
        if not isinstance(code, str) or not code.strip() or not isinstance(value, Mapping):
            continue
        code = code.strip()
        evidence = {**deepcopy(dict(shared)), **deepcopy(dict(value.get("evidence") or {}))} if isinstance(
            value.get("evidence", {}), Mapping) else deepcopy(dict(shared))
        reasons = list(evidence.get("reason_codes") or []) if isinstance(evidence.get("reason_codes"), list) else []
        verified = (shared_scope_ok and course is not None and target is not None
                    and evidence.get("status") == "verified" and isinstance(evidence.get("source"), str)
                    and evidence["source"] in VERIFIED_SOURCES
                    and _course(evidence.get("course_id")) == course and _term(evidence.get("term")) == target
                    and (not evidence.get("primary_code") or str(evidence["primary_code"]) == code))
        requirement = value.get("requirement") if verified else "unknown"
        if not isinstance(requirement, str) or requirement not in {"required", "not_required", "unknown"}:
            requirement = "unknown"
        components = _components(value.get("required_components")) if requirement == "required" else []
        if requirement == "required" and components is None:
            requirement, components = "unknown", []
            reasons.append("required_components_unverified")
        if requirement == "not_required" and (value.get("required_components") or value.get("allowed_bundles")):
            requirement = "unknown"
            reasons.append("pairing_evidence_conflict")
        relationship = value.get("relationship") if verified and requirement != "unknown" else "unknown"
        if not isinstance(relationship, str) or relationship not in {"verified", "partial", "unknown"}:
            relationship = "unknown"
        complete = relationship == "verified" and value.get("relationship_complete") is True
        bundles, malformed = [], False
        rows = value.get("allowed_bundles", [])
        if not isinstance(rows, list):
            rows, malformed = [], True
        if requirement == "required" and relationship != "unknown":
            for bundle in rows:
                if isinstance(bundle, Mapping):
                    codes = bundle.get("secondary_codes")
                    scoped = str(bundle.get("primary_code", code)) == code
                else:
                    codes, scoped = bundle, True
                if (not scoped or not isinstance(codes, list) or not codes
                        or any(not isinstance(value, str) or not value.strip() for value in codes)):
                    malformed = True
                    continue
                codes = [value.strip() for value in codes]
                if code in codes or len(set(codes)) != len(codes):
                    malformed = True
                    continue
                entry = {"primary_code": code, "secondary_codes": codes}
                if entry not in bundles:
                    bundles.append(entry)
        if malformed:
            complete = False
            reasons.append("pairing_relationship_partial")
        if requirement == "required" and not bundles:
            relationship, complete = "unknown", False
        if requirement == "unknown":
            reasons.append("secondary_pairing_unknown")
        elif requirement == "required" and (relationship == "unknown" or not complete):
            reasons.append("pairing_relationship_unknown" if relationship == "unknown" else "pairing_relationship_partial")
        evidence.update(status="verified" if verified and requirement != "unknown" else "unverified",
                        course_id=course, term=target, primary_code=code,
                        reason_codes=list(dict.fromkeys(str(reason) for reason in reasons)))
        simple_types = ([group["types"][0] for group in components]
                        if components and all(len(group["types"]) == 1 and group["min"] == group["max"] == 1
                                              for group in components) else [])
        rules[code] = {"requirement": requirement, "required_components": components,
                       "required_types": simple_types, "relationship": relationship,
                       "relationship_complete": complete, "allowed_bundles": bundles, "evidence": evidence}
        if isinstance(value.get("required_course_components"), list):
            # Preserve the real linked-course identity. Such components do not
            # become same-course bundles or bypass selection scope checks.
            rules[code]["required_course_components"] = deepcopy(value["required_course_components"])
    result = {"version": 2, "course_id": course, "term": target, "per_primary": rules,
              "requirement": "unknown", "required_components": [], "required_types": [],
              "relationship": "unknown", "relationship_complete": False,
              "allowed_bundles": [deepcopy(bundle) for rule in rules.values() for bundle in rule["allowed_bundles"]],
              "evidence": {**deepcopy(dict(shared)), "course_id": course, "term": target,
                           "status": "partial" if any(rule["requirement"] != "unknown" for rule in rules.values()) else "unverified"}}
    return _project_v2(result, primary_code or item.get("primary_code") or raw.get("selected_primary_code"))


def _project_v2(pairing, primary_code):
    result = deepcopy(pairing)
    if primary_code is None:
        return result
    code = str(primary_code)
    rule = result["per_primary"].get(code)
    result["selected_primary_code"] = code
    if rule is not None:
        result.update(deepcopy(rule))
    else:
        result.update(requirement="unknown", required_components=[], required_types=[], allowed_bundles=[],
                      relationship="unknown", relationship_complete=False)
        result["evidence"] = {**result["evidence"], "status": "unverified", "primary_code": code,
                              "reason_codes": ["secondary_pairing_unknown"]}
    return result


def normalize_pairing_evidence(item, *, term=None, primary_code=None):
    """Normalize server-owned v1/v2 evidence without inferring missing rules."""
    item = item if isinstance(item, Mapping) else {}
    raw = item.get("pairing") if isinstance(item.get("pairing"), Mapping) else {}
    if raw.get("version") == 2:
        return _normalize_v2(item, raw, term=term, primary_code=primary_code)
    return _normalize_v1(item, term=term)


def pairing_for_primary(pairing, primary_code):
    """Project the selected primary while retaining the complete per-primary map."""
    pairing = pairing if isinstance(pairing, Mapping) else {}
    evidence = pairing.get("evidence") if isinstance(pairing.get("evidence"), Mapping) else {}
    return normalize_pairing_evidence({"pairing": pairing,
        "course_id": pairing.get("course_id") or evidence.get("course_id"),
        "term": pairing.get("term") or evidence.get("term")}, primary_code=primary_code)


def _verify_v2(pairing, selected_sections, primary_code):
    sections = [section for section in selected_sections or [] if isinstance(section, Mapping)]
    declared = set(pairing.get("per_primary") or {})
    primaries = [section for section in sections if _kind(section) in {"Lec", "Sem"} or _code(section) in declared]
    if primary_code is None and len(primaries) == 1:
        primary_code = _code(primaries[0])
    primary_code = str(primary_code) if primary_code is not None else None
    rule = pairing_for_primary(pairing, primary_code)
    secondaries = [section for section in sections if _code(section) != primary_code]
    result = {"status": "unknown", "primary_code": primary_code,
              "selected_secondary_codes": [_code(section) for section in secondaries if _code(section)],
              "reason_codes": [], "requirement": rule["requirement"], "relationship": rule["relationship"]}
    if rule["requirement"] == "unknown":
        result["reason_codes"] = ["secondary_pairing_unknown"]
    elif not primary_code or not any(_code(section) == primary_code for section in sections):
        result.update(status="unsatisfied", reason_codes=["primary_not_selected"])
    elif len(primaries) > 1:
        result.update(status="unsatisfied", reason_codes=["multiple_primary_sections"])
    elif any(section.get("term") and _term(section["term"]) != pairing.get("term") for section in sections):
        result["reason_codes"] = ["section_scope_mismatch"]
    elif any(section.get("course_id") and _course(section["course_id"]) != pairing.get("course_id") for section in sections):
        result["reason_codes"] = ["section_scope_mismatch"]
    elif len({_code(section) for section in sections}) != len(sections):
        result.update(status="unsatisfied", reason_codes=["duplicate_section_selection"])
    elif rule["requirement"] == "not_required":
        result.update(status="unknown" if secondaries else "satisfied",
                      reason_codes=["additional_component_unverified"] if secondaries else [])
    elif any(_kind(section) is None for section in secondaries):
        result["reason_codes"] = ["component_type_unknown"]
    elif any(_kind(section) not in {kind for group in rule["required_components"] for kind in group["types"]}
             for section in secondaries):
        result["reason_codes"] = ["additional_component_unverified"]
    else:
        counts = [sum(_kind(section) in group["types"] for section in secondaries)
                  for group in rule["required_components"]]
        if any(count < group["min"] for count, group in zip(counts, rule["required_components"])):
            result.update(status="unsatisfied", reason_codes=["required_component_missing"])
        elif any(count > group["max"] for count, group in zip(counts, rule["required_components"])):
            result.update(status="unsatisfied", reason_codes=["component_count_exceeded"])
        else:
            selected = set(result["selected_secondary_codes"])
            allowed = [set(bundle["secondary_codes"]) for bundle in rule["allowed_bundles"]]
            if selected in allowed:
                result["status"] = "satisfied"
            elif rule["relationship_complete"]:
                result.update(status="unsatisfied", reason_codes=["incompatible_secondary_selection"])
            else:
                result["reason_codes"] = ["pairing_relationship_unknown"]
    return result


def verify_selection(pairing, selected_sections, *, primary_code=None):
    """Verify one selection; partial relationship coverage supports positives only."""
    if isinstance(pairing, Mapping) and pairing.get("version") == 2:
        normalized = pairing_for_primary(pairing, primary_code)
        return _verify_v2(normalized, selected_sections, primary_code)
    return _verify_v1(pairing, selected_sections, primary_code=primary_code)


def available_pairing_sections(pairing, sections, *, verified_only=False):
    """Keep unknown candidates, or return only proven bundles for formal hard-time use."""
    sections = list(sections or [])
    if not isinstance(pairing, Mapping):
        return ([], "unknown") if verified_only else (sections, "unknown")
    if pairing.get("version") != 2:
        pairing = pairing_for_primary(pairing, None)
        if not verified_only:
            return _available_v1(pairing, sections)
        if pairing.get("requirement") == "unknown":
            return [], "unknown"
        if pairing.get("requirement") == "not_required":
            primaries = [section for section in sections if isinstance(section, Mapping) and _kind(section) in {"Lec", "Sem"}]
            return primaries, "satisfied" if primaries else "unknown"
        by_code = {_code(section): section for section in sections if isinstance(section, Mapping)}
        retained = set()
        for bundle in pairing.get("allowed_bundles", []):
            codes = [bundle["primary_code"], *bundle["secondary_codes"]]
            if all(code in by_code for code in codes) and verify_selection(pairing, [by_code[code] for code in codes],
                                                                         primary_code=bundle["primary_code"])["status"] == "satisfied":
                retained.update(codes)
        return [section for section in sections if _code(section) in retained], "satisfied" if retained else "unknown"
    pairing = pairing_for_primary(pairing, None)
    by_code = {_code(section): section for section in sections if isinstance(section, Mapping) and _code(section)}
    primaries = [section for section in sections if isinstance(section, Mapping)
                 and (_kind(section) in {"Lec", "Sem"} or _code(section) in pairing["per_primary"])]
    retained, uncertain = set(), set()
    for primary in primaries:
        code = _code(primary)
        rule = pairing_for_primary(pairing, code)
        if rule["requirement"] == "unknown":
            uncertain.add(code)
            continue
        if rule["requirement"] == "not_required":
            retained.add(code)
            continue
        matched = False
        for bundle in rule["allowed_bundles"]:
            codes = [code, *bundle["secondary_codes"]]
            if all(value in by_code for value in codes) and verify_selection(pairing, [by_code[value] for value in codes],
                                                                             primary_code=code)["status"] == "satisfied":
                retained.update(codes)
                matched = True
        if not rule["relationship_complete"]:
            uncertain.add(code)
        elif not matched:
            continue
    if verified_only:
        return [section for section in sections if isinstance(section, Mapping) and _code(section) in retained], (
            "satisfied" if retained else "unknown" if uncertain or not primaries else "unsatisfied")
    proven = bool(retained)
    if uncertain:
        retained.update(uncertain)
        retained.update(_code(section) for section in sections if isinstance(section, Mapping)
                        and _kind(section) not in {"Lec", "Sem"})
    status = "partial" if proven and uncertain else "unknown" if uncertain or not primaries else (
        "satisfied" if retained else "unsatisfied")
    return [section for section in sections if not isinstance(section, Mapping) or _code(section) in retained], status
