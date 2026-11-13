"""
Canonical scheduling domain service.

This module owns section day/time parsing and overlap semantics for
agent tools, recommendation cards, and the schedule API. Higher-level
bundle validation will build on this surface in M3.2.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
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
    """Accept canonical HH:MM and Registrar suffixes such as 1:50p."""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*([ap](?:m)?)?", str(value or "").strip(), re.I)
    if not match:
        return None
    hour, minute = int(match[1]), int(match[2])
    suffix = (match[3] or "").lower()
    if minute > 59 or hour > 23 or (suffix and not 1 <= hour <= 12):
        return None
    if suffix:
        hour = hour % 12 + (12 if suffix.startswith("p") else 0)
    return hour * 60 + minute


def meeting_time_minutes(start, end) -> tuple[Optional[int], Optional[int]]:
    """Resolve WebSoc's shared meridiem: 3:30-4:50p, 11:00-12:20p.

    When only the end has a suffix, the start is the closest earlier
    occurrence on the same day. Unmarked 24-hour data stays unchanged.
    Overnight/invalid windows remain unknown instead of fabricating a day.
    """
    start_min, end_min = time_to_minutes(start), time_to_minutes(end)
    if start_min is None or end_min is None:
        return None, None
    if re.search(r"[ap](?:m)?$", str(end).strip(), re.I) and not re.search(r"[ap](?:m)?$", str(start).strip(), re.I):
        hour = int(str(start).strip().split(":")[0])
        if 1 <= hour <= 12:
            base = start_min % (12 * 60)
            candidates = [value for value in (base, base + 12 * 60) if value < end_min]
            if not candidates:
                return None, None
            start_min = max(candidates)
    if start_min >= end_min:
        return None, None
    return start_min, end_min


def normalize_section_meeting(section: dict) -> dict:
    """Normalize copied source/legacy records, without changing their evidence text."""
    result = dict(section)
    first = section_meetings(section)[0]
    # Keep the original meeting array and its extra source fields intact. Flat
    # fields remain a compatibility view of the first authoritative meeting.
    if section.get("meetings") is not None:
        for field in ("days", "start_time", "end_time", "location", "time_is_tba"):
            result[field] = first[field]
    elif first["start_time"] is not None and first["end_time"] is not None:
        result["start_time"] = first["start_time"]
        result["end_time"] = first["end_time"]
    return result


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
    return any(str(value or "").strip().upper() in {"TBA", "TBD", "UNKNOWN"} for value in fields)


def _meeting_clock(value) -> str | None:
    """Read API clock objects without inventing missing hours or minutes."""
    if isinstance(value, Mapping):
        hour, minute = value.get("hour"), value.get("minute")
        if (isinstance(hour, bool) or isinstance(minute, bool)
                or not isinstance(hour, int) or not isinstance(minute, int)
                or not 0 <= hour <= 23 or not 0 <= minute <= 59):
            return None
        return f"{hour:02d}:{minute:02d}"
    return value if isinstance(value, str) else None


def _meeting_days(value) -> str | None:
    if not isinstance(value, str):
        return None
    days = parse_day_codes(value)
    compact = "".join(char for char in value if char not in " ,/-\t\r\n")
    return "".join(days) if days and "".join(days) == compact else None


def section_meetings(section: Mapping) -> list[dict]:
    """Return every meeting with canonical fields, preserving source extras.

    An explicit empty/malformed array is missing schedule evidence, not a
    license to reuse stale flat fields. Only absent/None arrays use the
    legacy single-meeting representation. Unknown rows remain explicit so
    callers cannot mistake an empty iteration for a verified clear schedule.
    """
    raw = section.get("meetings")
    if raw is None:
        rows = [section]
    elif isinstance(raw, (list, tuple)) and raw:
        rows = raw
    else:
        rows = [{}]
    meetings = []
    for row in rows:
        source = row if isinstance(row, Mapping) else {}
        days = _meeting_days(source.get("days"))
        start_raw = source.get("start_time") if "start_time" in source else source.get("startTime")
        end_raw = source.get("end_time") if "end_time" in source else source.get("endTime")
        start, end = meeting_time_minutes(_meeting_clock(start_raw), _meeting_clock(end_raw))
        location = source.get("location") if "location" in source else source.get("bldg")
        if isinstance(location, (list, tuple)):
            location = ", ".join(item for item in location if isinstance(item, str))
        elif not isinstance(location, str):
            location = None
        tba = (_section_time_is_tba(source) or _truthy(source.get("timeIsTBA"))
               or days is None or start is None or end is None)
        meetings.append({**dict(source), "days": days,
                         "start_time": f"{start // 60:02d}:{start % 60:02d}" if start is not None else None,
                         "end_time": f"{end // 60:02d}:{end % 60:02d}" if end is not None else None,
                         "location": location, "time_is_tba": tba})
    return meetings


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
    meetings_a, meetings_b = section_meetings(a), section_meetings(b)
    unknown = any(meeting["time_is_tba"] for meeting in (*meetings_a, *meetings_b))
    for left in meetings_a:
        for right in meetings_b:
            if left["time_is_tba"] or right["time_is_tba"]:
                continue
            if not (parse_days(left["days"]) & parse_days(right["days"])):
                continue
            start_a, end_a = meeting_time_minutes(left["start_time"], left["end_time"])
            start_b, end_b = meeting_time_minutes(right["start_time"], right["end_time"])
            if start_a < end_b and start_b < end_a:
                # A proven conflict is still a conflict if another meeting is TBA.
                return "conflict"
    return "unknown" if unknown else "clear"


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
    pairing_by_course: dict[tuple[str, str], dict] = {}

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
            from app.recommendations.pairing import contract_enabled, normalize_pairing_evidence
            if "pairing" in envelope or contract_enabled():
                pairing_by_course[course_key] = normalize_pairing_evidence({**envelope, "course_id": course_id}, term=entry_term)
            raw_sections = envelope.get("sections", []) if envelope.get("found") else []
            sections_by_course[course_key] = [
                _with_course_id(section, course_id, term=entry_term)
                for section in raw_sections
                if isinstance(section, dict)
            ]

        # Use the same exact, server-verified snapshot as the calendar. A
        # catalog record for this section may still have old/TBA times.
        snapshot = entry.get("materialized_section")
        if isinstance(snapshot, dict) and (
            _section_num(snapshot) == section_ref
            or _section_code(snapshot) == section_ref
        ):
            normalized_snapshot = _with_course_id(
                snapshot,
                course_id,
                term=entry_term,
            )
            sections_by_course[course_key] = [
                section for section in sections_by_course[course_key] if not (
                    (
                        _section_num(section)
                        and _section_num(section) == _section_num(normalized_snapshot)
                    )
                    or (
                        _section_code(section)
                        and _section_code(section) == _section_code(normalized_snapshot)
                    )
                )
            ]
            sections_by_course[course_key].append(normalized_snapshot)

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
        if (entry_term, course_id) in pairing_by_course:
            from app.recommendations.pairing import pairing_for_primary, verify_selection
            pairing = pairing_by_course[(entry_term, course_id)]
            item["selection_verification"] = verify_selection(pairing, selected_sections)
            item["pairing"] = pairing_for_primary(pairing, item["selection_verification"]["primary_code"])
            if item["pairing"]["version"] == 2:
                item["pairing_selected"] = item["pairing"]
        elif primaries and secondaries:
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
    if section.get("meetings") is not None:
        return " / ".join(
            (f"{meeting['days'] or 'TBA'} TBA" if meeting["time_is_tba"] else
             f"{meeting['days']} {meeting['start_time']}\u2013{meeting['end_time']}")
            for meeting in section_meetings(section)
        )
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
    return any(meeting["time_is_tba"] for meeting in section_meetings(section))


def _with_course_id(
    section: dict,
    course_id: str,
    *,
    term: Optional[str] = None,
) -> dict:
    copied = normalize_section_meeting(section)
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
                if "pairing" in item:
                    selected_codes = (item.get("selection_verification") or {}).get("selected_secondary_codes") or []
                    primary.extend(section for section in normalized
                                   if _section_code(section) in selected_codes and section not in primary)
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
        or _truthy(section.get("isCancelled"))
        or _truthy(section.get("cancelled"))
        or _section_status_key(section) in {"CANCELLED", "CANCELED"}
    )


def section_cancellation_status(section: Mapping) -> Literal["cancelled", "not_cancelled", "unknown"]:
    """Preserve explicit unknown API cancellation without changing old fixtures."""
    if _section_is_cancelled(section):
        return "cancelled"
    for field in ("is_cancelled", "isCancelled", "cancelled"):
        if field in section:
            value = section[field]
            if (value is False
                    or isinstance(value, str) and value.strip().lower() in {"false", "0", "no", "n"}
                    or type(value) is int and value == 0):
                return "not_cancelled"
            return "unknown"
    # Legacy records predate this field. An explicit None from a newer source
    # is materially different from that absent compatibility field.
    return "not_cancelled"


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

    if section_cancellation_status(section) == "unknown":
        warnings.append(
            _single_section_issue(
                "section_status_unknown",
                scope,
                f"{subject} cancellation status is unknown",
                section,
            )
        )

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
    pairing_results: list[dict] = []

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
        if "pairing" in item:
            from app.recommendations.pairing import normalize_pairing_evidence, pairing_for_primary, verify_selection
            pairing = normalize_pairing_evidence(item, term=item.get("term"), primary_code=item.get("primary_code"))
            selection = verify_selection(pairing, selected_sections, primary_code=item.get("primary_code"))
            pairing = pairing_for_primary(pairing, selection["primary_code"])
            pairing_result = {"course_id": course_id, "term": item.get("term"),
                              "pairing": pairing, "selection_verification": selection}
            if pairing["version"] == 2:
                pairing_result["pairing_selected"] = pairing
            pairing_results.append(pairing_result)
            issue = {"type": "pairing_unknown" if selection["status"] == "unknown" else "incomplete_pairing",
                     "scope": "bundle", "message": f"{course_id}: pairing {selection['status']}",
                     "reason_codes": selection["reason_codes"],
                     "sections": [_section_summary(section) for section in selected_sections]}
            if selection["status"] == "unknown":
                unknowns.append(issue)
            elif selection["status"] == "unsatisfied":
                conflicts.append(issue)
        else:
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
        **({"pairing": pairing_results} if pairing_results else {}),
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
