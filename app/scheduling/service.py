"""
Canonical scheduling domain service.

This module owns section day/time parsing and overlap semantics for
agent tools, recommendation cards, and the schedule API. Higher-level
bundle validation will build on this surface in M3.2.
"""
from __future__ import annotations

from typing import Iterable, Literal, Optional


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
SectionTimeStatus = Literal["conflict", "clear", "unknown"]


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
