"""
Canonical scheduling domain service.

This module owns section day/time parsing and overlap semantics for
agent tools, recommendation cards, and the schedule API. Higher-level
bundle validation will build on this surface in M3.2.
"""
from __future__ import annotations

from typing import Callable, Iterable, Literal, Optional


DAY_CODES: tuple[str, ...] = ("M", "Tu", "W", "Th", "F", "Sa", "Su")
DAY_CODE_TO_NAME: dict[str, str] = {
    "M": "Mon",
    "Tu": "Tue",
    "W": "Wed",
    "Th": "Thu",
    "F": "Fri",
    "Sa": "Sat",
    "Su": "Sun",
}
_DAY_TOKENS: tuple[str, ...] = ("Tu", "Th", "Sa", "Su", "M", "W", "F")
_PRIMARY_SECTION_PREFIXES: tuple[str, ...] = ("lec", "sem")
_SECONDARY_SECTION_PREFIXES: tuple[str, ...] = ("dis", "lab", "stu", "act", "tut", "fld")
SectionTimeStatus = Literal["conflict", "clear", "unknown"]
FinalExamStatus = Literal["conflict", "clear", "unknown"]
SectionLookup = Callable[[str, str], dict]


def parse_day_codes(value: str) -> tuple[str, ...]:
    """
    Tokenize a UCI day string into ordered canonical day codes.

    Examples:
        "TuTh"       -> ("Tu", "Th")
        "MWF"        -> ("M", "W", "F")
        "MTuWThFSa"  -> ("M", "Tu", "W", "Th", "F", "Sa")
    """
    if not value:
        return ()

    out: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(value):
        matched = False
        for token in _DAY_TOKENS:
            if value.startswith(token, i):
                if token not in seen:
                    out.append(token)
                    seen.add(token)
                i += len(token)
                matched = True
                break
        if not matched:
            i += 1
    return tuple(out)


def parse_days(value: str) -> set[str]:
    """Compatibility helper returning canonical day codes as a set."""
    return set(parse_day_codes(value))


def calendar_day_names(value: str) -> tuple[str, ...]:
    """Return UI calendar day names for a UCI day string."""
    return tuple(DAY_CODE_TO_NAME[code] for code in parse_day_codes(value))


def time_to_minutes(value: str) -> Optional[int]:
    """Convert 24-hour ``HH:MM`` text to minutes from midnight."""
    if not value or ":" not in value:
        return None
    try:
        hour, minute = value.strip().split(":", 1)
        parsed_hour = int(hour)
        parsed_minute = int(minute)
    except (AttributeError, ValueError):
        return None
    if not (0 <= parsed_hour <= 23 and 0 <= parsed_minute <= 59):
        return None
    return parsed_hour * 60 + parsed_minute


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _section_time_is_tba(section: dict) -> bool:
    if _truthy(section.get("time_is_tba")):
        return True
    fields = (
        section.get("days"),
        section.get("start_time"),
        section.get("end_time"),
        section.get("time_display"),
    )
    return any(str(value or "").strip().upper() == "TBA" for value in fields)


def section_time_status(a: dict, b: dict) -> SectionTimeStatus:
    """
    Compare two sections' meeting windows.

    Returns:
        "conflict"  the sections share a day and overlapping clock interval
        "clear"     both schedules are known and do not overlap
        "unknown"   at least one side has TBA/missing/unparseable time data
    """
    term_a = str(a.get("term") or "").strip()
    term_b = str(b.get("term") or "").strip()
    if term_a and term_b and term_a != term_b:
        return "clear"
    if _section_time_is_tba(a) or _section_time_is_tba(b):
        return "unknown"

    days_a = parse_days(a.get("days") or "")
    days_b = parse_days(b.get("days") or "")
    if not days_a or not days_b:
        return "unknown"
    if not (days_a & days_b):
        return "clear"

    start_a = time_to_minutes(a.get("start_time") or "")
    end_a = time_to_minutes(a.get("end_time") or "")
    start_b = time_to_minutes(b.get("start_time") or "")
    end_b = time_to_minutes(b.get("end_time") or "")
    if None in (start_a, end_a, start_b, end_b):
        return "unknown"

    return "conflict" if start_a < end_b and start_b < end_a else "clear"


def sections_overlap(a: dict, b: dict) -> bool:
    """Backward-compatible boolean overlap check."""
    return section_time_status(a, b) == "conflict"


def resolve_pending_schedule_sections(
    pending_schedule: Iterable[dict],
    *,
    term: Optional[str],
    section_lookup: SectionLookup,
) -> list[dict]:
    """
    Resolve persisted pending schedule entries into concrete section dicts.

    Session state stores compact entries such as
    ``{"course_id": "COMPSCI161", "section": "A"}``. Bundle validation
    needs the actual section payload with days/times/status/finals, so callers
    provide the term-scoped ``section_lookup(course_id, term)`` function.
    """
    resolved: list[dict] = []
    for entry in pending_schedule or []:
        if not isinstance(entry, dict):
            continue
        course_id = str(entry.get("course_id") or "").strip()
        section_ref = str(
            entry.get("section")
            or entry.get("section_num")
            or entry.get("section_code")
            or ""
        ).strip()
        if not course_id or not section_ref:
            continue

        entry_term = str(entry.get("term") or term or "").strip()
        if not entry_term or entry_term == "unknown":
            continue

        try:
            envelope = section_lookup(course_id, entry_term)
        except Exception:
            continue
        sections = envelope.get("sections", []) if envelope.get("found") else []
        match = next(
            (
                section
                for section in sections
                if section.get("section_num") == section_ref
                or section.get("section_code") == section_ref
            ),
            None,
        )
        if match:
            resolved.append(_with_course_id(match, course_id, term=entry_term))
    return resolved


def build_pending_schedule_bundle_items(
    pending_schedule: Iterable[dict],
    *,
    term: Optional[str],
    section_lookup: SectionLookup,
) -> list[dict]:
    """
    Convert compact pending schedule entries into bundle-validation items.

    Unlike ``resolve_pending_schedule_sections()``, this preserves course
    grouping and infers whether a course requires primary + secondary
    sections from the term catalog. That lets callers validate a whole
    pending schedule for incomplete Lec/Dis/Lab pairings.
    """
    entries_by_course: dict[tuple[str, str], list[dict]] = {}
    sections_by_course: dict[tuple[str, str], list[dict]] = {}

    for entry in pending_schedule or []:
        if not isinstance(entry, dict):
            continue
        course_id = str(entry.get("course_id") or "").strip()
        section_ref = str(
            entry.get("section")
            or entry.get("section_num")
            or entry.get("section_code")
            or ""
        ).strip()
        if not course_id or not section_ref:
            continue

        entry_term = str(entry.get("term") or term or "").strip()
        if not entry_term or entry_term == "unknown":
            continue
        course_key = (entry_term, course_id)

        if course_key not in sections_by_course:
            try:
                envelope = section_lookup(course_id, entry_term)
            except Exception:
                envelope = {}
            raw_sections = envelope.get("sections", []) if envelope.get("found") else []
            sections_by_course[course_key] = [
                _with_course_id(section, course_id, term=entry_term)
                for section in raw_sections
                if isinstance(section, dict)
            ]

        match = next(
            (
                section
                for section in sections_by_course[course_key]
                if _section_num(section) == section_ref
                or _section_code(section) == section_ref
            ),
            None,
        )
        if match:
            entries_by_course.setdefault(course_key, []).append(match)

    bundle_items: list[dict] = []
    for (entry_term, course_id), selected_sections in entries_by_course.items():
        catalog_sections = sections_by_course.get((entry_term, course_id), [])
        bookable_sections = [
            section
            for section in catalog_sections
            if _section_code(section) and not _section_is_cancelled(section)
        ]
        primaries = [
            section for section in bookable_sections
            if _is_primary_section(section)
        ]
        secondaries = [
            section for section in bookable_sections
            if _is_secondary_section(section)
        ]

        item = {
            "course_id": course_id,
            "term": entry_term,
            "selected_sections": selected_sections,
        }
        if primaries and secondaries:
            item["requires_secondary"] = True
            item["secondary_type"] = _secondary_type_label(
                {"course_id": course_id},
                secondaries,
            )
        bundle_items.append(item)

    return bundle_items


def _section_id(section: dict) -> str:
    return str(section.get("section_id") or section.get("section_code") or "")


def _section_code(section: dict) -> str:
    return str(
        section.get("section_code")
        or section.get("sectionCode")
        or section.get("section_num")
        or ""
    )


def _format_meeting_window(section: dict) -> str:
    days = section.get("days") or "TBA"
    start = section.get("start_time") or "?"
    end = section.get("end_time") or "?"
    return f"{days} {start}\u2013{end}"


def _section_num(section: dict) -> str:
    return str(section.get("section_num") or section.get("sectionNum") or "")


def _section_type(section: dict) -> str:
    return str(section.get("section_type") or section.get("sectionType") or "").strip()


def _course_id(section: dict) -> str:
    return str(section.get("course_id") or section.get("courseId") or "")


def _section_label(section: dict) -> str:
    course_id = _course_id(section) or "unknown course"
    section_ref = _section_num(section) or _section_code(section) or "unknown section"
    return f"{course_id} {section_ref}"


def _section_summary(section: dict) -> dict:
    summary = {
        "course_id": _course_id(section),
        "section_code": _section_code(section),
        "section_num": _section_num(section),
        "window": _format_meeting_window(section),
    }
    final_exam = _final_exam(section)
    if isinstance(final_exam, dict):
        exam = _parse_final_exam(final_exam)
        if exam["status"] != "none":
            summary["final_exam"] = exam["label"]
    return summary


def _section_has_unknown_time(section: dict) -> bool:
    return section_time_status(section, section) == "unknown"


def _with_course_id(
    section: dict,
    course_id: str,
    *,
    term: Optional[str] = None,
) -> dict:
    copied = dict(section)
    if course_id and not _course_id(copied):
        copied["course_id"] = course_id
    if term and not copied.get("term"):
        copied["term"] = term
    return copied


def _is_primary_section(section: dict, item: dict | None = None) -> bool:
    section_type = _section_type(section).lower()
    if section_type.startswith(_PRIMARY_SECTION_PREFIXES):
        return True
    if item:
        primary_code = str(item.get("primary_code") or "").strip()
        if primary_code and (
            _section_code(section) == primary_code
            or _section_num(section) == primary_code
        ):
            return True
    return False


def _is_secondary_section(section: dict) -> bool:
    return _section_type(section).lower().startswith(_SECONDARY_SECTION_PREFIXES)


def _secondary_type_label(item: dict, selected_sections: list[dict]) -> str:
    explicit = str(item.get("secondary_type") or "").strip()
    if explicit:
        return explicit

    secondary_codes = item.get("secondary_codes")
    if isinstance(secondary_codes, list):
        for secondary in secondary_codes:
            if isinstance(secondary, dict):
                section_type = str(secondary.get("type") or "").strip()
                if section_type:
                    return section_type

    for section in selected_sections:
        if _is_secondary_section(section):
            section_type = _section_type(section)
            if section_type:
                return section_type
    return "secondary"


def _item_requires_secondary(item: dict) -> bool:
    if item.get("requires_secondary") is True:
        return True
    if item.get("secondary_type"):
        return True
    secondary_codes = item.get("secondary_codes")
    return isinstance(secondary_codes, list) and bool(secondary_codes)


def _incomplete_pairing_conflict(item: dict, selected_sections: list[dict]) -> dict | None:
    if not _item_requires_secondary(item):
        return None

    course_id = str(item.get("course_id") or "")
    secondary_label = _secondary_type_label(item, selected_sections)
    has_primary = any(_is_primary_section(section, item) for section in selected_sections)
    has_secondary = any(_is_secondary_section(section) for section in selected_sections)

    if has_primary and has_secondary:
        return None

    if has_primary:
        message = (
            f"{course_id} requires Lec plus {secondary_label}, "
            f"but no {secondary_label} section is selected"
        )
    elif has_secondary:
        message = (
            f"{course_id} requires a primary section plus {secondary_label}, "
            "but no primary section is selected"
        )
    else:
        message = (
            f"{course_id or 'This course'} requires a primary section plus "
            f"{secondary_label}, but the selected section type is unclear"
        )

    return {
        "type": "incomplete_pairing",
        "scope": "bundle",
        "message": message,
        "sections": [_section_summary(section) for section in selected_sections],
    }


def _selected_sections_for_item(item: dict) -> list[dict]:
    """
    Extract the concrete sections this bundle item proposes.

    Current recommendation cards carry a full ``sections`` list plus a
    ``primary_code``. Tests and future callers may pass ``selected_sections``
    directly. If an item itself looks like a section, accept it as one.
    """
    if not isinstance(item, dict):
        return []

    course_id = str(item.get("course_id") or "")
    term = str(item.get("term") or "").strip() or None

    selected = item.get("selected_sections")
    if isinstance(selected, list):
        return [
            _with_course_id(section, course_id, term=term)
            for section in selected
            if isinstance(section, dict)
        ]

    sections = item.get("sections")
    if isinstance(sections, list):
        normalized = [
            _with_course_id(section, course_id, term=term)
            for section in sections
            if isinstance(section, dict)
        ]
        primary_code = str(item.get("primary_code") or "").strip()
        if primary_code:
            primary = [
                section
                for section in normalized
                if _section_code(section) == primary_code
                or _section_num(section) == primary_code
            ]
            if primary:
                return primary
        return normalized[:1] if len(normalized) == 1 else normalized

    if any(item.get(field) for field in ("days", "start_time", "end_time")):
        return [_with_course_id(item, course_id, term=term)]
    return []


def _missing_section_unknown(course_id: str) -> dict:
    label = course_id or "Unknown course"
    return {
        "type": "missing_sections",
        "scope": "bundle",
        "message": f"{label} has no selected section to validate",
        "sections": [],
    }


def _time_unknown(scope: str, message: str, sections: list[dict]) -> dict:
    return {
        "type": "time_unknown",
        "scope": scope,
        "message": message,
        "sections": [_section_summary(section) for section in sections],
    }


def _time_conflict(scope: str, message: str, sections: list[dict]) -> dict:
    return {
        "type": "time_conflict",
        "scope": scope,
        "message": message,
        "sections": [_section_summary(section) for section in sections],
    }


def _final_exam(section: dict):
    return section.get("final_exam") or section.get("finalExam")


def _exam_time_to_minutes(value) -> Optional[int]:
    if not isinstance(value, dict):
        return None
    hour = value.get("hour")
    minute = value.get("minute", 0)
    if not isinstance(hour, int) or not isinstance(minute, int):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _format_exam_time(minutes: int) -> str:
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}"


def _parse_final_exam(final_exam) -> dict:
    """
    Normalize Anteater/CSV finalExam payloads.

    Missing final exam data is treated as "none" for M3.2b compatibility;
    explicit TBA or malformed scheduled payloads are treated as unknown.
    """
    if not isinstance(final_exam, dict):
        return {"status": "none", "label": None}

    status = str(final_exam.get("examStatus") or "").upper()
    if status == "NO_FINAL":
        return {"status": "none", "label": "No final exam"}
    if status == "TBA_FINAL":
        return {"status": "unknown", "label": "Final TBA"}
    if status != "SCHEDULED_FINAL":
        return {"status": "unknown", "label": "Final exam status unknown"}

    month = final_exam.get("month")
    day = final_exam.get("day")
    start = _exam_time_to_minutes(final_exam.get("startTime"))
    end = _exam_time_to_minutes(final_exam.get("endTime"))
    if not isinstance(month, int) or not isinstance(day, int) or None in (start, end):
        return {"status": "unknown", "label": "Final exam time incomplete"}

    day_of_week = str(final_exam.get("dayOfWeek") or "").strip()
    date_label = f"{month}/{day}"
    if day_of_week:
        date_label = f"{day_of_week} {date_label}"

    return {
        "status": "scheduled",
        "label": f"{date_label} {_format_exam_time(start)}\u2013{_format_exam_time(end)}",
        "date": (month, day),
        "start": start,
        "end": end,
    }


def _section_has_unknown_final_exam(section: dict) -> bool:
    final_exam = _final_exam(section)
    return isinstance(final_exam, dict) and _parse_final_exam(final_exam)["status"] == "unknown"


def final_exam_status(a: dict, b: dict) -> FinalExamStatus:
    term_a = str(a.get("term") or "").strip()
    term_b = str(b.get("term") or "").strip()
    if term_a and term_b and term_a != term_b:
        return "clear"
    exam_a = _parse_final_exam(_final_exam(a))
    exam_b = _parse_final_exam(_final_exam(b))

    if exam_a["status"] == "none" or exam_b["status"] == "none":
        return "clear"
    if exam_a["status"] == "unknown" or exam_b["status"] == "unknown":
        return "unknown"
    if exam_a["date"] != exam_b["date"]:
        return "clear"
    return (
        "conflict"
        if exam_a["start"] < exam_b["end"] and exam_b["start"] < exam_a["end"]
        else "clear"
    )


def _final_exam_unknown(scope: str, message: str, sections: list[dict]) -> dict:
    return {
        "type": "final_exam_unknown",
        "scope": scope,
        "message": message,
        "sections": [_section_summary(section) for section in sections],
    }


def _final_exam_conflict(scope: str, message: str, sections: list[dict]) -> dict:
    return {
        "type": "final_exam_conflict",
        "scope": scope,
        "message": message,
        "sections": [_section_summary(section) for section in sections],
    }


def _section_status(section: dict) -> str:
    return str(section.get("status") or "").strip()


def _section_status_key(section: dict) -> str:
    return _section_status(section).upper()


def _section_is_cancelled(section: dict) -> bool:
    return (
        _truthy(section.get("is_cancelled"))
        or _truthy(section.get("cancelled"))
        or _section_status_key(section) in {"CANCELLED", "CANCELED"}
    )


def _section_is_full(section: dict) -> bool:
    return _section_status_key(section) == "FULL"


def _section_is_waitlisted(section: dict) -> bool:
    return "WAIT" in _section_status_key(section)


def _section_restrictions(section: dict) -> str:
    restrictions = section.get("restrictions")
    if restrictions is None:
        return ""
    if isinstance(restrictions, list):
        return ", ".join(str(value).strip() for value in restrictions if str(value).strip())
    return str(restrictions).strip()


def _section_issue_subject(section: dict, scope: str) -> str:
    label = _section_label(section)
    return f"pending {label}" if scope == "pending_schedule" else label


def _single_section_issue(
    issue_type: str,
    scope: str,
    message: str,
    section: dict,
) -> dict:
    return {
        "type": issue_type,
        "scope": scope,
        "message": message,
        "sections": [_section_summary(section)],
    }


def _section_availability_issues(section: dict, scope: str) -> tuple[list[dict], list[dict]]:
    conflicts: list[dict] = []
    warnings: list[dict] = []
    subject = _section_issue_subject(section, scope)

    if _section_is_cancelled(section):
        conflicts.append(
            _single_section_issue(
                "section_unavailable",
                scope,
                f"{subject} is cancelled",
                section,
            )
        )
        return conflicts, warnings

    if _section_is_full(section):
        conflicts.append(
            _single_section_issue(
                "section_unavailable",
                scope,
                f"{subject} is FULL",
                section,
            )
        )
        return conflicts, warnings

    if _section_is_waitlisted(section):
        warnings.append(
            _single_section_issue(
                "section_waitlist",
                scope,
                f"{subject} is waitlist-only",
                section,
            )
        )

    restrictions = _section_restrictions(section)
    if restrictions:
        warnings.append(
            _single_section_issue(
                "section_restriction",
                scope,
                f"{subject} has enrollment restrictions: {restrictions}",
                section,
            )
        )

    return conflicts, warnings


def validate_schedule_bundle(
    recommended_items: Iterable[dict],
    *,
    pending_sections: Iterable[dict] = (),
) -> dict:
    """
    Validate a proposed set of course sections against known time rules.

    M3.2 scope:
      - proposed recommendation sections vs each other
      - proposed recommendation sections vs already-resolved pending sections
      - TBA/missing time is reported as ``unknown`` instead of treated as clear
      - scheduled final-exam conflicts are reported alongside time conflicts
      - required primary/secondary pairings must include both halves
      - cancelled/FULL sections are hard conflicts
      - waitlist and enrollment restrictions are warnings

    The function is intentionally pure: callers resolve persisted
    ``pending_schedule`` entries into concrete section dicts before calling.
    """
    warnings: list[dict] = []
    conflicts: list[dict] = []
    unknowns: list[dict] = []

    bundle_sections: list[dict] = []
    for item in recommended_items or []:
        if not isinstance(item, dict):
            continue
        course_id = str(item.get("course_id") or "")
        selected_sections = _selected_sections_for_item(item)
        if not selected_sections:
            unknowns.append(_missing_section_unknown(course_id))
            continue
        bundle_sections.extend(selected_sections)
        pairing_conflict = _incomplete_pairing_conflict(item, selected_sections)
        if pairing_conflict:
            conflicts.append(pairing_conflict)
        for section in selected_sections:
            section_conflicts, section_warnings = _section_availability_issues(
                section,
                "bundle",
            )
            conflicts.extend(section_conflicts)
            warnings.extend(section_warnings)
            if _section_has_unknown_time(section):
                unknowns.append(
                    _time_unknown(
                        "bundle",
                        f"{_section_label(section)} has unknown meeting time",
                        [section],
                    )
                )
            if _section_has_unknown_final_exam(section):
                unknowns.append(
                    _final_exam_unknown(
                        "bundle",
                        f"{_section_label(section)} final exam time is unknown",
                        [section],
                    )
                )

    pending_list = [
        section for section in pending_sections or []
        if isinstance(section, dict)
    ]
    for section in pending_list:
        section_conflicts, section_warnings = _section_availability_issues(
            section,
            "pending_schedule",
        )
        conflicts.extend(section_conflicts)
        warnings.extend(section_warnings)

    for left_index, left in enumerate(bundle_sections):
        for right in bundle_sections[left_index + 1:]:
            if _course_id(left) == _course_id(right):
                continue
            status = section_time_status(left, right)
            if status == "conflict":
                conflicts.append(
                    _time_conflict(
                        "bundle",
                        f"{_section_label(left)} conflicts with {_section_label(right)}",
                        [left, right],
                    )
                )
            elif status == "unknown":
                unknowns.append(
                    _time_unknown(
                        "bundle",
                        (
                            f"Cannot determine whether {_section_label(left)} "
                            f"conflicts with {_section_label(right)}"
                        ),
                        [left, right],
                    )
                )
            exam_status = final_exam_status(left, right)
            if exam_status == "conflict":
                conflicts.append(
                    _final_exam_conflict(
                        "bundle",
                        (
                            f"{_section_label(left)} final exam conflicts with "
                            f"{_section_label(right)} final exam"
                        ),
                        [left, right],
                    )
                )
            elif exam_status == "unknown":
                unknowns.append(
                    _final_exam_unknown(
                        "bundle",
                        (
                            f"Cannot determine whether {_section_label(left)} final exam "
                            f"conflicts with {_section_label(right)} final exam"
                        ),
                        [left, right],
                    )
                )

    for bundle_section in bundle_sections:
        for pending_section in pending_list:
            status = section_time_status(bundle_section, pending_section)
            if status == "conflict":
                conflicts.append(
                    _time_conflict(
                        "pending_schedule",
                        (
                            f"{_section_label(bundle_section)} conflicts with "
                            f"pending {_section_label(pending_section)}"
                        ),
                        [bundle_section, pending_section],
                    )
                )
            elif status == "unknown":
                unknowns.append(
                    _time_unknown(
                        "pending_schedule",
                        (
                            f"Cannot determine whether {_section_label(bundle_section)} "
                            f"conflicts with pending {_section_label(pending_section)}"
                        ),
                        [bundle_section, pending_section],
                    )
                )
            exam_status = final_exam_status(bundle_section, pending_section)
            if exam_status == "conflict":
                conflicts.append(
                    _final_exam_conflict(
                        "pending_schedule",
                        (
                            f"{_section_label(bundle_section)} final exam conflicts with "
                            f"pending {_section_label(pending_section)} final exam"
                        ),
                        [bundle_section, pending_section],
                    )
                )
            elif exam_status == "unknown":
                unknowns.append(
                    _final_exam_unknown(
                        "pending_schedule",
                        (
                            f"Cannot determine whether {_section_label(bundle_section)} "
                            f"final exam conflicts with pending "
                            f"{_section_label(pending_section)} final exam"
                        ),
                        [bundle_section, pending_section],
                    )
                )

    return {
        "valid": not conflicts and not unknowns,
        "warnings": warnings,
        "conflicts": conflicts,
        "unknowns": unknowns,
    }


def find_conflicts(
    candidate_sections: Iterable[dict],
    student_sections: Iterable[dict],
) -> dict[str, list[dict]]:
    """
    For each candidate section, list known student-section conflicts.

    Unknown/TBA comparisons are intentionally not reported as conflicts
    here; M3.2 bundle validation will expose them separately as unknowns.
    """
    student_list = list(student_sections)
    out: dict[str, list[dict]] = {}
    for candidate in candidate_sections:
        candidate_id = _section_id(candidate)
        if not candidate_id:
            continue
        conflicts: list[dict] = []
        for student in student_list:
            if _section_id(student) == candidate_id:
                continue
            if sections_overlap(candidate, student):
                conflicts.append(
                    {
                        "course_id": student.get("course_id", ""),
                        "section_code": _section_code(student),
                        "window": _format_meeting_window(student),
                    }
                )
        if conflicts:
            out[candidate_id] = conflicts
    return out


def summarize_for_card(
    candidate_sections: list[dict],
    section_conflicts: dict[str, list[dict]],
) -> dict:
    """
    Roll per-section conflicts up to course-level display metadata.
    """
    total = len(candidate_sections)
    if total == 0:
        return {
            "status": "none",
            "summary": "",
            "conflicting_count": 0,
            "total_sections": 0,
        }

    conflicting = [
        section
        for section in candidate_sections
        if section_conflicts.get(_section_id(section))
    ]
    conflicting_count = len(conflicting)

    if conflicting_count == 0:
        return {
            "status": "none",
            "summary": "",
            "conflicting_count": 0,
            "total_sections": total,
        }

    if conflicting_count == total:
        first_conflicts = section_conflicts.get(_section_id(conflicting[0]), [])
        if first_conflicts:
            first = first_conflicts[0]
            summary = f"Conflicts with {first['course_id']} ({first['window']})"
        else:
            summary = "All sections conflict with current schedule"
        return {
            "status": "all",
            "summary": summary,
            "conflicting_count": conflicting_count,
            "total_sections": total,
        }

    return {
        "status": "some",
        "summary": f"{conflicting_count} of {total} sections conflict with current schedule",
        "conflicting_count": conflicting_count,
        "total_sections": total,
    }
