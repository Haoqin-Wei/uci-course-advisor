"""Structured restriction queries and evidence.

The restriction workflow owns factual retrieval and extraction.  The LLM may
explain the resulting bundle, but it must not manufacture missing dates,
audiences, or exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from html.parser import HTMLParser
import re
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.catalog.normalization import iter_course_mentions


class RestrictionType(str, Enum):
    SCHOOL_MAJOR = "school_major"
    NEW_ONLY = "new_only"
    CLASS_LEVEL = "class_level"
    REPEAT = "repeat"
    AUTHORIZATION_CODE = "authorization_code"
    COURSE_SPECIFIC = "course_specific"
    ADD_DROP_CHANGE = "add_drop_change"
    AMBIGUOUS = "ambiguous"


class EvidenceStatus(str, Enum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    CONFLICTING = "conflicting"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RestrictionQuery:
    term: str
    department: str
    restriction_type: RestrictionType
    course_id: Optional[str] = None
    student_major: Optional[str] = None
    student_school: Optional[str] = None
    academic_level: str = "undergraduate"

    def to_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "department": self.department,
            "course_id": self.course_id,
            "restriction_type": self.restriction_type.value,
            "student_major": self.student_major,
            "student_school": self.student_school,
            "academic_level": self.academic_level,
        }


@dataclass(frozen=True)
class RestrictionEvent:
    event_id: str
    restriction_type: RestrictionType
    action: str
    effective_at: Optional[str]
    term: str
    department: str
    course_scope: tuple[str, ...] = ()
    audience: tuple[str, ...] = ()
    exceptions: tuple[dict[str, Any], ...] = ()
    source_url: str = ""
    source_role: str = ""
    retrieved_at: Optional[str] = None
    source_position: Optional[dict[str, Any]] = None
    statement: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "restriction_type": self.restriction_type.value,
            "action": self.action,
            "effective_at": self.effective_at,
            "term": self.term,
            "department": self.department,
            "course_scope": list(self.course_scope),
            "audience": list(self.audience),
            "exceptions": [dict(item) for item in self.exceptions],
            "source_url": self.source_url,
            "source_role": self.source_role,
            "retrieved_at": self.retrieved_at,
            "source_position": self.source_position,
            "statement": self.statement,
        }


@dataclass(frozen=True)
class RestrictionEligibility:
    eligible: Optional[bool]
    student_major: Optional[str]
    student_school: Optional[str]
    allowed_groups: tuple[str, ...] = ()
    reason: str = ""
    evidence_event_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "student_major": self.student_major,
            "student_school": self.student_school,
            "allowed_groups": list(self.allowed_groups),
            "reason": self.reason,
            "evidence_event_id": self.evidence_event_id,
        }


@dataclass
class RestrictionEvidenceBundle:
    query: RestrictionQuery
    events: list[RestrictionEvent] = field(default_factory=list)
    primary_event_id: Optional[str] = None
    related_event_ids: list[str] = field(default_factory=list)
    eligibility: Optional[RestrictionEligibility] = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    evidence_status: EvidenceStatus = EvidenceStatus.PARTIAL
    missing_required_fields: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def primary_event(self) -> Optional[RestrictionEvent]:
        return next(
            (
                event
                for event in self.events
                if event.event_id == self.primary_event_id
            ),
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query.to_dict(),
            "events": [event.to_dict() for event in self.events],
            "primary_event_id": self.primary_event_id,
            "related_event_ids": list(self.related_event_ids),
            "eligibility": (
                self.eligibility.to_dict() if self.eligibility is not None else None
            ),
            "sources": [dict(source) for source in self.sources],
            "evidence_status": self.evidence_status.value,
            "missing_required_fields": list(self.missing_required_fields),
            "conflicts": [dict(conflict) for conflict in self.conflicts],
        }


def classify_restriction_type(
    text: str,
    *,
    has_course: bool = False,
) -> RestrictionType:
    """Classify the restriction fact the user is asking for."""

    value = text or ""
    if re.search(r"\b(?:new only|nors?|new only restriction)s?\b|新生(?:预留|限制)", value, re.I):
        return RestrictionType.NEW_ONLY
    if re.search(r"专业限制|院系限制|学校限制|\b(?:school|major|department) restrictions?\b", value, re.I):
        return RestrictionType.SCHOOL_MAJOR
    if re.search(r"年级限制|upper[- ]division|class[- ]level|standing", value, re.I):
        return RestrictionType.CLASS_LEVEL
    if re.search(r"重修限制|\brepeat restrictions?\b", value, re.I):
        return RestrictionType.REPEAT
    if re.search(r"授权码|authorization code|\b[ABX][ -]?restrictions?\b", value, re.I):
        return RestrictionType.AUTHORIZATION_CODE
    if re.search(r"add\s*/?\s*drop|change grade|加课.*截止|退课.*截止|改.*grade", value, re.I):
        return RestrictionType.ADD_DROP_CHANGE
    if has_course and re.search(r"限制|开放|能选|restriction|open|enroll", value, re.I):
        return RestrictionType.COURSE_SPECIFIC
    return RestrictionType.AMBIGUOUS


_IGNORED_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "nav",
    "header",
    "footer",
    "aside",
}
_BLOCK_TAGS = {
    "body",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "li",
    "tr",
}
_DATE_LINE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")
_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b", re.I)
_SECTION = re.compile(
    r"^(COMPSCI|CSE|GDIM|I&C SCI|IN4MATX|STATS|SWE)\s+courses\b",
    re.I,
)


class _MainContentParser(HTMLParser):
    """Extract ordered semantic blocks while excluding site chrome."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, Any]] = []
        self._stack: list[dict[str, Any]] = []
        self._ignored_depth = 0
        self._main_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, Optional[str]]],
    ) -> None:
        del attrs
        tag = tag.lower()
        if tag in _IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in {"main", "article"}:
            self._main_depth += 1
        if tag in _BLOCK_TAGS:
            self._stack.append(
                {
                    "tag": tag,
                    "parts": [],
                    "in_main": self._main_depth > 0,
                }
            )

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or not self._stack:
            return
        for block in self._stack:
            block["parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag in _BLOCK_TAGS and self._stack:
            position = len(self.blocks)
            block = self._stack.pop()
            text = _collapse_ws(" ".join(block["parts"]))
            if text:
                self.blocks.append(
                    {
                        "index": position,
                        "tag": block["tag"],
                        "text": text,
                        "in_main": block["in_main"],
                    }
                )
        if tag in {"main", "article"} and self._main_depth:
            self._main_depth -= 1


def extract_main_content_blocks(html: str) -> list[dict[str, Any]]:
    """Return ordered main-content blocks, falling back to cleaned body blocks."""

    parser = _MainContentParser()
    parser.feed(html or "")
    parser.close()
    main = [block for block in parser.blocks if block["in_main"]]
    fallback = [
        block for block in parser.blocks if block["tag"] != "body"
    ] or parser.blocks
    selected = main or fallback
    return [
        {**block, "index": index}
        for index, block in enumerate(selected)
    ]


def select_query_focused_blocks(
    blocks: list[dict[str, Any]],
    query: RestrictionQuery,
    *,
    neighbor_count: int = 2,
) -> list[dict[str, Any]]:
    """Select relevant blocks plus structural/date neighbors for LLM context."""

    if not blocks:
        return []
    needles = {
        query.department.lower(),
        query.restriction_type.value.replace("_", " "),
    }
    if query.course_id:
        needles.add(query.course_id.lower())
    if query.restriction_type == RestrictionType.SCHOOL_MAJOR:
        needles.update({"school/major", "major restriction", "all campus majors"})
    elif query.restriction_type == RestrictionType.NEW_ONLY:
        needles.update({"new only", "nor", "nors"})
    elif query.restriction_type == RestrictionType.AMBIGUOUS:
        needles.update({"restriction", "enrollment"})

    selected: set[int] = set()
    current_heading: Optional[int] = None
    for index, block in enumerate(blocks):
        if block["tag"].startswith("h"):
            current_heading = index
        lowered = block["text"].lower()
        if any(needle and needle in lowered for needle in needles):
            selected.update(
                range(
                    max(0, index - neighbor_count),
                    min(len(blocks), index + neighbor_count + 1),
                )
            )
            if current_heading is not None:
                selected.add(current_heading)
    return [blocks[index] for index in sorted(selected)]


def parse_restriction_timeline(
    blocks: list[dict[str, Any]],
    *,
    term: str,
    department: str,
    source_url: str,
    source_role: str,
    retrieved_at: Optional[str] = None,
) -> list[RestrictionEvent]:
    """Parse date/time/action groups into deterministic restriction events."""

    drafts: list[dict[str, Any]] = []
    current_date: Optional[tuple[int, int, int]] = None
    current_time: Optional[tuple[int, int]] = None
    current_department = department
    active_event: Optional[dict[str, Any]] = None

    for index, block in enumerate(blocks):
        text = block["text"].strip()
        section = _SECTION.search(text)
        if section:
            current_department = _canonical_section_department(section.group(1))
            active_event = None
            continue

        date_match = _DATE_LINE.match(text)
        if date_match:
            month, day, year = (int(value) for value in date_match.groups())
            current_date = (year, month, day)
            current_time = None
            active_event = None
            continue

        time_match = _TIME.search(text)
        if time_match and _looks_like_time_line(text):
            current_time = _parse_time_match(time_match)
            if not _action_type(text):
                continue

        if active_event is not None and block.get("tag") == "li":
            refs = [ref.display() for ref, _start, _end in iter_course_mentions(text)]
            item: dict[str, Any] = {"text": text}
            if len(refs) == 1:
                item["course_id"] = refs[0]
            elif refs:
                item["course_ids"] = refs
            active_event["exceptions"].append(item)
            continue

        action_info = _action_type(text)
        if action_info is not None:
            restriction_type, action = action_info
            inline_date, inline_time = _inline_datetime(text)
            event_date = inline_date or current_date
            event_time = inline_time or current_time
            effective_at = _iso_los_angeles(event_date, event_time)
            audience = _extract_audience(text)
            active_event = {
                "event_id": f"{restriction_type.value}-{len(drafts) + 1}",
                "restriction_type": restriction_type,
                "action": action,
                "effective_at": effective_at,
                "term": term,
                "department": current_department,
                "course_scope": [],
                "audience": list(audience),
                "exceptions": [],
                "source_url": source_url,
                "source_role": source_role,
                "retrieved_at": retrieved_at,
                "source_position": {
                    "block_index": block.get("index", index),
                    "tag": block.get("tag"),
                },
                "statement": text,
            }
            drafts.append(active_event)
            continue

    return [
        RestrictionEvent(
            event_id=draft["event_id"],
            restriction_type=draft["restriction_type"],
            action=draft["action"],
            effective_at=draft["effective_at"],
            term=draft["term"],
            department=draft["department"],
            course_scope=tuple(draft["course_scope"]),
            audience=tuple(draft["audience"]),
            exceptions=tuple(draft["exceptions"]),
            source_url=draft["source_url"],
            source_role=draft["source_role"],
            retrieved_at=draft["retrieved_at"],
            source_position=draft["source_position"],
            statement=draft["statement"],
        )
        for draft in drafts
    ]


def _action_type(text: str) -> Optional[tuple[RestrictionType, str]]:
    lowered = text.lower()
    action = None
    if re.search(r"\b(?:removed|lifted|open(?:ed)? to)\b|解除|开放", lowered):
        action = "removed"
    elif re.search(r"\b(?:remain|continues?|active|restricted to)\b|继续限制", lowered):
        action = "active"
    elif re.search(r"\b(?:extend|reinstate)\w*\b|延长|恢复限制", lowered):
        action = "extended"
    if action is None:
        return None
    if re.search(r"new only|\bNORS?\b|新生预留", text, re.I):
        return RestrictionType.NEW_ONLY, action
    if re.search(r"school/major|school or major|major restrictions?", text, re.I):
        return RestrictionType.SCHOOL_MAJOR, action
    if re.search(r"upper[- ]division standing|class[- ]level", text, re.I):
        return RestrictionType.CLASS_LEVEL, action
    if re.search(r"repeat restrictions?", text, re.I):
        return RestrictionType.REPEAT, action
    return None


def _inline_datetime(
    text: str,
) -> tuple[Optional[tuple[int, int, int]], Optional[tuple[int, int]]]:
    date_match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    date_value = None
    if date_match:
        month, day, year = (int(value) for value in date_match.groups())
        date_value = (year, month, day)
    time_match = _TIME.search(text)
    return date_value, _parse_time_match(time_match) if time_match else None


def _parse_time_match(match: re.Match[str]) -> tuple[int, int]:
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = re.sub(r"[^apm]", "", match.group(3).lower())
    if meridiem.startswith("p") and hour != 12:
        hour += 12
    elif meridiem.startswith("a") and hour == 12:
        hour = 0
    return hour, minute


def _iso_los_angeles(
    date_value: Optional[tuple[int, int, int]],
    time_value: Optional[tuple[int, int]],
) -> Optional[str]:
    if date_value is None:
        return None
    hour, minute = time_value or (0, 0)
    value = datetime(
        *date_value,
        hour,
        minute,
        tzinfo=ZoneInfo("America/Los_Angeles"),
    )
    return value.isoformat()


def _looks_like_time_line(text: str) -> bool:
    stripped = text.strip().rstrip(":")
    return bool(_TIME.fullmatch(stripped))


def _extract_audience(text: str) -> tuple[str, ...]:
    match = re.search(
        r"(?:open(?:ed)?|available)\s+to\s+(.+?)(?:only)?(?:[.;]|$)",
        text,
        re.I,
    )
    if not match:
        return ()
    value = _collapse_ws(match.group(1)).strip(" :")
    return (value,) if value else ()


def _canonical_section_department(value: str) -> str:
    upper = value.upper()
    if upper in {"I&C SCI", "IN4MATX"}:
        return "I&C SCI" if upper == "I&C SCI" else "IN4MATX"
    return upper


def _collapse_ws(value: str) -> str:
    return " ".join((value or "").split())
