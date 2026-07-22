"""
Validation types — Issue, Severity, ValidationReport, ValidationContext.

Severity model:
    info  - notable but not actionable (e.g. 'historical data caveat')
    warn  - mention is suspicious; reader should verify
    error - mention is definitely wrong; should be removed/rewritten

Action model (decided by policies.py, not by individual validators):
    KEEP      - send LLM answer untouched
    ANNOTATE  - send through, add footer summarizing issues
    REMOVE    - strip problem spans before sending      [Phase 2]
    BLOCK     - discard LLM answer, fall back to template [Phase 2]
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.catalog.types import CourseRef
from app.catalog.view import CatalogView


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class SuggestedAction(str, Enum):
    KEEP = "keep"
    ANNOTATE = "annotate"
    REMOVE = "remove"
    BLOCK = "block"


@dataclass
class Issue:
    validator: str                                # e.g. "course_exists"
    code: str                                     # e.g. "HALLUCINATED_COURSE_ID"
    severity: Severity
    message: str
    location: Optional[dict] = None               # {"start": int, "end": int, "snippet": str}
    evidence: dict = field(default_factory=dict)
    suggested_action: SuggestedAction = SuggestedAction.ANNOTATE

    def to_dict(self) -> dict:
        return {
            "validator": self.validator,
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "location": self.location,
            "evidence": self.evidence,
            "suggested_action": self.suggested_action.value,
        }


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.WARN]

    @property
    def infos(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.INFO]

    @property
    def overall(self) -> str:
        if self.errors:
            return "fail"
        if self.warnings:
            return "warn"
        return "pass"

    def to_dict(self) -> dict:
        return {
            "overall": self.overall,
            "issue_count": len(self.issues),
            "error_count": len(self.errors),
            "warn_count": len(self.warnings),
            "issues": [i.to_dict() for i in self.issues],
        }


@dataclass
class ValidationContext:
    """Everything a validator needs to do its job.

    Built once per chat turn (after LLM answer is generated) and passed
    to every validator in turn.
    """
    llm_answer: str
    retrieved: dict                               # retrieval result envelope
    catalog: CatalogView
    session_state: dict
    cards: list[dict] = field(default_factory=list)
    user_message: str = ""
    retrieval_performed: bool = False
    query_terms: list[str] = field(default_factory=list)
    tool_terms: list[str] = field(default_factory=list)
    validation_term: Optional[str] = None

    @property
    def retrieved_primary_refs(self) -> set[CourseRef]:
        return _refs_from_results(self.retrieved.get("primary", []))

    @property
    def retrieved_flagged_refs(self) -> set[CourseRef]:
        return _refs_from_results(self.retrieved.get("flagged", []))

    @property
    def all_retrieved_refs(self) -> set[CourseRef]:
        return self.retrieved_primary_refs | self.retrieved_flagged_refs


def _refs_from_results(items: list[dict]) -> set[CourseRef]:
    """Extract CourseRefs from retrieval result items.

    Older and newer callers may use compact IDs like 'ICS33' or catalog
    IDs like 'COMPSCI 122B'. Route both through parse_course_mention so
    IDs are canonicalized consistently.
    """
    from app.catalog.normalization import parse_course_mention
    out: set[CourseRef] = set()
    for item in items:
        c = item.get("course") or {}
        cid = c.get("course_id") or ""
        ref = parse_course_mention(cid)
        if ref:
            out.add(ref)
    return out
