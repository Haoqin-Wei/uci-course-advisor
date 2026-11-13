"""Pure, conservative filtering of supplied lightweight course candidates.

The caller supplies a validated Filter Plan and evidence snapshots. This
module reads no profile, catalog, network, cache, environment or clock. It
does not infer a graduation audit or repeat/GPA eligibility from course tags.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy

from app.catalog.departments import DEPARTMENT_ALIASES
from app.recommendations.filter_plan import normalize_ge_category, trusted_ge_audit
from app.recommendations.pairing import normalize_pairing_evidence, available_pairing_sections
from app.scheduling.service import (
    DAY_CODE_TO_NAME,
    meeting_time_minutes,
    parse_day_codes,
    section_cancellation_status,
    section_meetings,
    time_to_minutes,
)

_PRIMARY = ("lec", "sem")
_SECONDARY = ("dis", "lab", "stu", "act", "tut", "fld")
_DAY_NAMES = {name: code for code, name in DAY_CODE_TO_NAME.items()}
_COURSE_PREFIXES = sorted(
    (("".join(alias.upper().split()), "".join(department.split()))
     for department, aliases in DEPARTMENT_ALIASES.items()
     for alias in (department, *aliases)),
    key=lambda pair: len(pair[0]), reverse=True,
)


def _unique(values):
    return list(dict.fromkeys(values))


def _course_key(value):
    if not isinstance(value, str):
        return None
    # Compare supplied IDs with the static alias table. The general course
    # parser can lazily read the catalog for unknown departments, so it is
    # intentionally not used inside this I/O-free engine.
    compact = "".join(value.upper().replace("_", " ").split())
    if not compact:
        return None
    for prefix, canonical in _COURSE_PREFIXES:
        if compact.startswith(prefix):
            rest = compact[len(prefix):]
            if rest and (rest[0].isdigit() or len(rest) > 1 and rest[0].isalpha() and rest[1].isdigit()):
                return canonical + rest
    return compact


def _completed_keys(profile: Mapping) -> set:
    keys = set()
    for field in ("completed_courses", "completed_course_attempts"):
        values = profile.get(field)
        if not isinstance(values, (list, tuple)):
            continue
        for item in values:
            value = item.get("course_id") if isinstance(item, Mapping) else item
            key = _course_key(value)
            if key:
                keys.add(key)
    return keys


def _kind(section: Mapping) -> str | None:
    value = str(section.get("section_type") or section.get("type") or "").lower()
    return next((kind for kind in (*_PRIMARY, *_SECONDARY) if value.startswith(kind)), None)


def _letter(section: Mapping) -> str | None:
    number = section.get("section_num") or section.get("num")
    if not isinstance(number, str) or not number or not number[0].isalpha():
        return None
    prefix = []
    for char in number:
        if not char.isalpha():
            break
        prefix.append(char.upper())
    return "".join(prefix) or None


def _term_key(value):
    return "".join(str(value or "").replace("_", " ").upper().split())


def _historical_section(section: Mapping, candidate: Mapping) -> bool:
    provenance = section.get("provenance") or candidate.get("provenance") or {}
    if isinstance(provenance, Mapping):
        if provenance.get("is_historical_proxy") is True:
            return True
        source, target = provenance.get("source_term"), provenance.get("target_term")
        if source and target and _term_key(source) != _term_key(target):
            return True
    if candidate.get("source_term") and candidate.get("term") and _term_key(candidate["source_term"]) != _term_key(candidate["term"]):
        return True
    return bool(section.get("term") and candidate.get("term")
                and _term_key(section["term"]) != _term_key(candidate["term"]))


def _day_codes(value):
    if not isinstance(value, str) or value.upper().strip() in {"", "TBA", "TBD", "UNKNOWN"}:
        return None
    parsed = parse_day_codes(value)
    # The shared tokenizer tolerates junk. A partial token in an unknown
    # label is not enough to hard-exclude a section on that weekday.
    compact = "".join(char for char in value if char not in " ,/-\t")
    if not parsed or "".join(parsed) != compact:
        return None
    return set(parsed)


def _section_decision(section: Mapping, candidate: Mapping, hard: Mapping):
    reasons, warnings = [], []
    has_hard_time = hard.get("earliest_start_is_hard") is True and bool(hard.get("earliest_start"))
    excluded_days = {_DAY_NAMES[day] for day in hard.get("excluded_weekdays", []) if day in _DAY_NAMES}
    historical = _historical_section(section, candidate)
    cancellation = section_cancellation_status(section)
    if cancellation == "cancelled":
        if historical:
            warnings.append("section_status_unknown")
        else:
            reasons.append("section_cancelled")
    elif cancellation == "unknown":
        warnings.append("section_status_unknown")
    if historical and (has_hard_time or excluded_days):
        return reasons, [*warnings, "time_unknown"]
    for meeting in section_meetings(section):
        if (has_hard_time or excluded_days) and meeting["time_is_tba"]:
            warnings.append("time_unknown")
        if has_hard_time:
            start, end = meeting_time_minutes(meeting["start_time"], meeting["end_time"])
            limit = time_to_minutes(hard.get("earliest_start"))
            if meeting["time_is_tba"] or start is None or end is None or limit is None:
                warnings.append("time_unknown")
            elif start < limit:
                reasons.append("earliest_start_violation")
        if excluded_days:
            days = _day_codes(meeting["days"])
            if days is None:
                warnings.append("time_unknown")
            elif days & excluded_days:
                reasons.append("excluded_weekday")
    return _unique(reasons), _unique(warnings)


def _filter_sections(candidate: Mapping, hard: Mapping):
    raw = candidate.get("sections")
    if not isinstance(raw, (list, tuple)) or not raw:
        return [], [], ["section_schedule_unknown"], False
    sections = deepcopy(list(raw))
    kept, excluded, warnings = [], [], []
    for section in sections:
        if not isinstance(section, Mapping):
            kept.append(section)
            warnings.append("section_schedule_unknown")
            continue
        reasons, section_warnings = _section_decision(section, candidate, hard)
        if reasons:
            excluded.append({"section_code": section.get("section_code") or section.get("code"),
                             "section_num": section.get("section_num") or section.get("num"), "reasons": reasons})
        else:
            kept.append(section)
            warnings.extend(section_warnings)
    if not kept:
        return [], excluded, _unique(warnings), True
    original_primary = [section for section in sections if isinstance(section, Mapping) and _kind(section) in _PRIMARY]
    original_secondary = [section for section in sections if isinstance(section, Mapping) and _kind(section) in _SECONDARY]
    if (original_primary and not any(isinstance(section, Mapping) and _kind(section) in _PRIMARY for section in kept)
            and all(isinstance(section, Mapping) and _kind(section) in _SECONDARY for section in kept)):
        return [], excluded, _unique(warnings), True
    if "pairing" in candidate:
        pairing = normalize_pairing_evidence(candidate, term=candidate.get("term"))
        compatible, status = available_pairing_sections(pairing, kept)
        if status in {"unknown", "partial"}:
            warnings.append("secondary_pairing_unknown")
        elif status == "unsatisfied":
            return [], excluded, _unique(warnings), True
        removed = {str(section.get("section_code") or section.get("code")) for section in kept
                   if isinstance(section, Mapping)} - {
                       str(section.get("section_code") or section.get("code")) for section in compatible
                       if isinstance(section, Mapping)}
        excluded.extend({"section_code": code, "reasons": ["no_compatible_section_bundle"]} for code in sorted(removed))
        return compatible, excluded, _unique(warnings), False
    requires = candidate.get("requires_secondary")
    if requires is None and original_primary and original_secondary:
        warnings.append("secondary_pairing_unknown")
    if requires is True:
        # Complete section letters are the existing Registrar pairing signal.
        # Without that evidence, retain candidates rather than invent a bundle.
        if (not original_primary or not original_secondary
                or any(_letter(section) is None for section in (*original_primary, *original_secondary))
                or any(not isinstance(section, Mapping) or _kind(section) is None for section in sections)):
            warnings.append("secondary_pairing_unknown")
        else:
            declared = candidate.get("required_secondary_types")
            if declared is not None:
                required_types = {_kind({"section_type": value}) for value in declared} if isinstance(declared, (list, tuple)) else set()
                valid_types = bool(required_types) and None not in required_types and required_types.issubset(set(_SECONDARY))
            else:
                observed = {_kind(section) for section in original_secondary}
                # Multiple components require explicit requirements; do not
                # assume an optional lab is compulsory merely from its presence.
                required_types, valid_types = observed, len(observed) == 1
            if not valid_types:
                warnings.append("secondary_pairing_unknown")
            else:
                surviving_primary = [section for section in kept if _kind(section) in _PRIMARY]
                surviving_secondary = [section for section in kept if _kind(section) in _SECONDARY]
                bundle_primary = []
                for primary in surviving_primary:
                    matched = [secondary for secondary in surviving_secondary if _letter(secondary) == _letter(primary)]
                    if all(any(_kind(secondary) == kind for secondary in matched) for kind in required_types):
                        bundle_primary.append(primary)
                    else:
                        excluded.append({"section_code": primary.get("section_code") or primary.get("code"),
                                         "section_num": primary.get("section_num") or primary.get("num"),
                                         "reasons": ["no_compatible_section_bundle"]})
                letters = {_letter(section) for section in bundle_primary}
                compatible = []
                for section in kept:
                    if section in bundle_primary or _kind(section) in _SECONDARY and _letter(section) in letters:
                        compatible.append(section)
                    elif _kind(section) in _SECONDARY:
                        excluded.append({"section_code": section.get("section_code") or section.get("code"),
                                         "section_num": section.get("section_num") or section.get("num"),
                                         "reasons": ["no_compatible_section_bundle"]})
                kept = compatible
                if not bundle_primary:
                    return [], excluded, _unique(warnings), True
    if any(not isinstance(section, Mapping) or _kind(section) is None for section in kept):
        warnings.append("section_type_unknown")
    return kept, excluded, _unique(warnings), False


def _ge_decision(candidate: Mapping, profile: Mapping):
    audit = trusted_ge_audit(profile)
    if audit is None:
        return None, ["ge_audit_unverified"], None
    raw = candidate.get("ge_categories", candidate.get("ge_labels"))
    verified = candidate.get("ge_labels_verified") is True or candidate.get("ge_categories_verified") is True
    if not isinstance(raw, (list, tuple)) or not verified:
        return None, ["ge_labels_unknown"], None
    labels = [normalize_ge_category(value) for value in raw]
    if any(value is None for value in labels):
        return None, ["ge_labels_unknown"], None
    unmet = set(audit["unmet_categories"])
    projected = set(audit.get("projected_categories") or []) & unmet
    remaining = unmet - projected
    contribution = {"unmet_categories": sorted(set(labels) & unmet),
                    "projected_categories": sorted(set(labels) & projected),
                    "remaining_categories": sorted(set(labels) & remaining)}
    if contribution["remaining_categories"]:
        return True, [], contribution
    return False, [], contribution


def apply_dynamic_prefilter(candidates, plan, profile=None) -> dict:
    """Return retained payloads and explicit exclusions/verification reasons.

    Authoritative negative school evidence may exclude a course. Missing
    records, uncertain GE/repeat eligibility and unknown schedules remain
    candidates. Soft preferences and goal labels never hard-exclude.
    """
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise TypeError("candidates must be a sequence of mappings")
    if not isinstance(plan, Mapping):
        raise TypeError("plan must be the validator's JSON mapping")
    applied = deepcopy(dict(plan))
    hard = applied.get("hard_filters") or {}
    if not isinstance(hard, Mapping):
        raise TypeError("hard_filters must be a mapping")
    profile = profile if isinstance(profile, Mapping) else {}
    completed = _completed_keys(profile)
    kept, excluded, verification = [], [], []
    input_sections = kept_sections = removed_sections = 0
    for original in candidates:
        if not isinstance(original, Mapping):
            raise TypeError("candidate payloads must be mappings")
        candidate = deepcopy(dict(original))
        course_id = candidate.get("course_id") or candidate.get("id")
        reasons, warnings = [], []
        raw_sections = candidate.get("sections")
        original_section_count = len(raw_sections) if isinstance(raw_sections, (list, tuple)) else 0
        input_sections += original_section_count
        offering = candidate.get("offering_status")
        if offering in {"not_offered", "not_offered_this_term"}:
            if candidate.get("authoritative") is True and not _historical_section({}, candidate):
                reasons.append("not_offered_this_term")
            else:
                warnings.append("offering_status_unknown")
        prereq_result = candidate.get("prerequisite_result") or {}
        prerequisite = candidate.get("prerequisite_status", prereq_result.get("status", "unknown") if isinstance(prereq_result, Mapping) else "unknown")
        prereq_authoritative = candidate.get("prerequisite_authoritative") is True or (
            isinstance(prereq_result, Mapping) and prereq_result.get("authoritative") is True)
        if isinstance(prereq_result, Mapping) and prereq_result.get("status") not in {None, prerequisite}:
            prereq_authoritative = False
        if prerequisite == "not_met" and prereq_authoritative:
            reasons.append("prerequisite_not_met")
        elif prerequisite != "met":
            warnings.append("prerequisite_status_unknown")
        eligibility = candidate.get("eligibility_status")
        if eligibility == "not_eligible" and candidate.get("eligibility_authoritative") is True:
            reasons.append("not_eligible")
        elif eligibility not in {None, "eligible", "not_restricted"}:
            warnings.append("eligibility_status_unknown")
        if candidate.get("repeat_seen") is True or _course_key(course_id) in completed:
            warnings.append("repeat_eligibility_unknown")
        contribution = None
        if hard.get("only_unmet_ge") is True:
            contributes, ge_warnings, contribution = _ge_decision(candidate, profile)
            warnings.extend(ge_warnings)
            if contributes is False:
                reasons.append("no_unmet_ge_contribution")
        section_exclusions = []
        if not reasons:
            sections, section_exclusions, section_warnings, no_bundle = _filter_sections(candidate, hard)
            warnings.extend(section_warnings)
            candidate["sections"] = sections
            if no_bundle:
                reasons.append("no_compatible_section_bundle")
        if reasons:
            removed_sections += original_section_count
            record = {"id": course_id, "term": candidate.get("term"), "reasons": _unique(reasons),
                      "excluded_sections": section_exclusions}
            if contribution is not None:
                record["ge_contribution"] = contribution
            excluded.append(record)
            continue
        removed_sections += original_section_count - len(candidate["sections"])
        warnings = _unique(warnings)
        candidate["prefilter"] = {"status": "needs_verification" if warnings else "keep",
                                  "reasons": warnings, "excluded_sections": section_exclusions}
        if contribution is not None:
            candidate["prefilter"]["ge_contribution"] = contribution
        kept.append(candidate)
        kept_sections += len(candidate["sections"])
        if warnings:
            verification.append({"id": course_id, "term": candidate.get("term"), "reasons": warnings})
    return {"kept": kept, "excluded": excluded, "needs_verification": verification, "applied_plan": applied,
            "counts": {"input_candidates": len(candidates), "kept_candidates": len(kept),
                       "excluded_candidates": len(excluded), "needs_verification": len(verification),
                       "input_sections": input_sections, "kept_sections": kept_sections,
                       "excluded_sections": removed_sections}}
