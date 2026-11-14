"""Restriction dates written in the WebSoc notices themselves.

Most schools state when a School/Major restriction ends right in the school
or department comments of the Schedule of Classes, each in its own words:

    ... will be restricted to School or majors only until MONDAY, DECEMBER 7th by noon.
    School/Major restrictions for some Arts courses will be removed beginning at noon on December 10, 2026.
    Major restrictions for Chemistry courses will be lifted on Monday, December 21, by 12:00pm.

This module reads those sentences. A sentence whose date can be read with
certainty becomes a ``RestrictionEvent``; every sentence about a restriction,
dated or not ("Major restrictions will be removed AFTER the fee deadline."),
is also kept word for word so an answer can quote the notice instead of
saying nothing was found.

Dates are only accepted when they check out: a notice that omits the year is
dated from the term it belongs to, and a weekday, when given, must agree.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

from app.data.restriction_timeline import (
    RestrictionEvent,
    RestrictionQuery,
    RestrictionType,
    _TIME,
    _iso_los_angeles,
    _parse_time_match,
)

NOTICE_SOURCE_ROLE = "registrar_websoc_comments"
MAX_STATEMENTS = 10
MAX_STATEMENT_CHARS = 420

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DATE = re.compile(
    r"(?:(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?,?\s+)?"
    r"(?P<month>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(?P<year>\d{4})(?!\d))?",
    re.I,
)
_NUMERIC_DATE = re.compile(r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4}|\d{2})\b")

# Where a sentence turns from "what is restricted" to "until when".
_UNTIL = re.compile(
    r"\b(?:restricted\b[^.]*?\buntil|not\s+be\s+able\s+to\s+enroll\b[^.]*?\buntil)\b\s*:?",
    re.I,
)
_LIFTED = re.compile(r"\b(?:restrictions?|status|caps?)\b[^.]*?\b(?:removed|lifted|lifts?)\b", re.I)
# How far after that turn the date may start ("beginning at noon on December 10").
_DATE_WINDOW = 70

_REPEAT = re.compile(r"\brepeat", re.I)
_NEW_ONLY = re.compile(r"new\s*-?\s*only|\bNORS?\b", re.I)
_SCHOOL_MAJOR = re.compile(r"\bmajors?\b|school\s*/\s*major|school\s+and\s+major", re.I)


def _sentences(text: str) -> list[str]:
    flat = re.sub(r"\s+([.,;:])", r"\1", " ".join((text or "").split()))
    parts = re.split(r"(?<=[.!?])\s+(?=[\"'`(]?[A-Z])", flat)
    return [part.strip() for part in parts if part.strip()]


def _statement_types(head: str) -> list[RestrictionType]:
    """Every kind of restriction a sentence speaks of.

    Usually one; "The major and repeat restrictions will lift December 18"
    is about two.
    """
    kinds = []
    if _SCHOOL_MAJOR.search(head):
        kinds.append(RestrictionType.SCHOOL_MAJOR)
    if _NEW_ONLY.search(head):
        kinds.append(RestrictionType.NEW_ONLY)
    if _REPEAT.search(head):
        kinds.append(RestrictionType.REPEAT)
    return kinds


def _term_parts(term: str) -> tuple[Optional[int], str]:
    year = re.search(r"\b(20\d{2})\b", term or "")
    season = re.search(r"winter|spring|summer|fall", term or "", re.I)
    return (int(year.group(1)) if year else None, season.group(0).lower() if season else "")


def enrollment_year(term: str, month: int) -> Optional[int]:
    """The calendar year a month falls in while people enroll for ``term``.

    Winter enrollment runs through the previous autumn, so "December 7" in a
    Winter 2027 notice is December 2026; spring enrollment starts in winter.
    """
    year, season = _term_parts(term)
    if year is None:
        return None
    if season == "winter" and month >= 8:
        return year - 1
    if season == "spring" and month >= 10:
        return year - 1
    return year


def _read_date(tail: str, term: str) -> Optional[tuple[int, int, int]]:
    """A date from the start of ``tail`` that survives every check, else None."""
    window = tail[:_DATE_WINDOW + 40]
    match = _DATE.search(window)
    numeric = _NUMERIC_DATE.search(window)
    if numeric and (not match or numeric.start() < match.start()):
        month, day = int(numeric.group("month")), int(numeric.group("day"))
        year = int(numeric.group("year"))
        year = year + 2000 if year < 100 else year
        weekday = None
        start = numeric.start()
    elif match:
        month, day = _MONTHS[match.group("month").lower()], int(match.group("day"))
        year = int(match.group("year")) if match.group("year") else None
        weekday = _WEEKDAYS[match.group("weekday")[:3].lower()] if match.group("weekday") else None
        start = match.start()
    else:
        return None
    if start > _DATE_WINDOW:
        return None
    expected = enrollment_year(term, month)
    if year is None:
        year = expected
    elif expected is not None and year != expected:
        return None                     # a year that does not belong to this term's enrollment
    if year is None:
        return None
    try:
        value = date(year, month, day)
    except ValueError:
        return None
    if weekday is not None and value.weekday() != weekday:
        return None                     # weekday and date disagree: do not guess which is right
    return year, month, day


def _read_time(tail: str) -> Optional[tuple[int, int]]:
    if re.search(r"\bnoon\b", tail, re.I):
        return 12, 0
    match = _TIME.search(tail)
    return _parse_time_match(match) if match else None


def read_notice_restrictions(
    websoc_result: dict[str, Any],
    query: RestrictionQuery,
) -> tuple[list[RestrictionEvent], list[dict[str, Any]]]:
    """Events and verbatim statements from one department's WebSoc notices."""
    source_url = str(websoc_result.get("source_url") or "")
    retrieved_at = websoc_result.get("retrieved_at")
    events: list[RestrictionEvent] = []
    statements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in websoc_result.get("comment_blocks") or []:
        if not isinstance(block, dict):
            continue
        scope = "department" if block.get("block_type") == "department" else "school"
        text = str(block.get("text") or "")
        title = str(block.get("title") or "")
        if title and text.startswith(title):
            text = text[len(title):]          # "School of ... comments:" is a heading, not part of a sentence
        for sentence in _sentences(text):
            turn = _UNTIL.search(sentence) or _LIFTED.search(sentence)
            if not turn:
                continue
            kinds = _statement_types(sentence[:turn.end()])
            if not kinds or sentence in seen:
                continue
            seen.add(sentence)
            tail = sentence[turn.end():]
            when = _read_date(tail, query.term)
            effective_at = _iso_los_angeles(when, _read_time(tail)) if when else None
            for restriction_type in kinds:
                statements.append({
                    "scope": scope,
                    "restriction_type": restriction_type.value,
                    "text": sentence[:MAX_STATEMENT_CHARS],
                    "effective_at": effective_at,
                })
                if effective_at is None:
                    continue
                events.append(
                    RestrictionEvent(
                        event_id=f"websoc-{restriction_type.value}-{len(events) + 1}",
                        restriction_type=restriction_type,
                        action="removed",
                        effective_at=effective_at,
                        term=query.term,
                        department=query.department,
                        source_url=source_url,
                        source_role=NOTICE_SOURCE_ROLE,
                        retrieved_at=retrieved_at,
                        source_position={"field": "notice", "block_type": scope},
                        statement=sentence[:MAX_STATEMENT_CHARS],
                    )
                )
    return _most_specific(events), statements[:MAX_STATEMENTS]


def _most_specific(events: list[RestrictionEvent]) -> list[RestrictionEvent]:
    """A department's own date replaces its school's general one of the same kind."""
    department_types = {
        event.restriction_type for event in events
        if (event.source_position or {}).get("block_type") == "department"
    }
    return [
        event for event in events
        if (event.source_position or {}).get("block_type") == "department"
        or event.restriction_type not in department_types
    ]


def is_notice_event(event: Optional[RestrictionEvent]) -> bool:
    return bool(event) and (event.source_position or {}).get("field") == "notice"


def same_notice_block(left: RestrictionEvent, right: RestrictionEvent) -> bool:
    """Two statements of one notice are a list ("upper-division until ...,
    lower-division until ..."), not two sources that disagree."""
    return (
        is_notice_event(left) and is_notice_event(right)
        and left.source_url == right.source_url
        and (left.source_position or {}).get("block_type") == (right.source_position or {}).get("block_type")
    )
