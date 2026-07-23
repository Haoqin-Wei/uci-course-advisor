"""Structured restriction queries and evidence.

The restriction workflow owns factual retrieval and extraction.  The LLM may
explain the resulting bundle, but it must not manufacture missing dates,
audiences, or exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Optional


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
