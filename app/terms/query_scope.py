"""Pure per-turn query-term resolution.

This module never reads or writes conversation storage. It converts trusted
backend state plus the current user message into the only terms that tools may
use for the turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Mapping, Optional

from app.catalog.departments import colloquial_course_id
from app.catalog.normalization import iter_course_mentions
from app.terms.models import TermKey, TermParseError
from app.terms.parser import extract_explicit_term_keys, parse_term_key, parse_term_text


QueryTermSource = Literal[
    "default",
    "explicit",
    "relative",
    "followup",
    "comparison",
]


@dataclass(frozen=True)
class QueryScope:
    terms: tuple[TermKey, ...]
    source: QueryTermSource
    explicit: bool
    ambiguous: bool
    error: Optional[TermParseError]
    course_ids: tuple[str, ...]

    @property
    def canonical_terms(self) -> tuple[str, ...]:
        return tuple(term.canonical_name for term in self.terms)


_CURRENT_TERM = re.compile(
    r"当前学期|本学期|current\s+(?:term|quarter)",
    re.I,
)
_NEXT_TERM = re.compile(
    r"下学期|下一学期|下个学期|下个\s*(?:term|quarter)|next\s+(?:term|quarter)",
    re.I,
)
_PREVIOUS_TERM = re.compile(
    r"上学期|上一学期|上个学期|上个\s*(?:term|quarter)|"
    r"previous\s+(?:term|quarter)|last\s+(?:term|quarter)",
    re.I,
)
_RELATIVE_YEAR = re.compile(
    r"(?P<cn>今年|去年|明年)|(?P<en>this|last|next)\s+year",
    re.I,
)
_FOCUS_TERMS = re.compile(
    r"这两个学期|那两个学期|这两学期|those\s+(?:two\s+)?terms|"
    r"these\s+(?:two\s+)?terms|both\s+terms|这几个学期",
    re.I,
)
_FOCUS_SINGLE = re.compile(
    r"这个学期|那个学期|this\s+term|that\s+term|"
    r"这门课|那门课|this\s+course|that\s+course",
    re.I,
)
_COMPARE = re.compile(r"比较|对比|区别|compare|versus|vs\.?|between", re.I)
_FOLLOWUP_CONNECTOR = re.compile(
    r"^\s*(?:那(?:么)?|然后|再看|再比较|what\s+about|how\s+about|then\b|and\b)",
    re.I,
)

_QUARTER_SIGNALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bfall\b|\bautumn\b|秋季?|秋学期", re.I), "Fall"),
    (re.compile(r"\bwinter\b|冬季?|冬学期", re.I), "Winter"),
    (re.compile(r"\bspring\b|春季?|春学期", re.I), "Spring"),
    (
        re.compile(r"\bsummer\s*(?:session\s*)?1\b|暑期1|夏季1", re.I),
        "Summer1",
    ),
    (
        re.compile(r"\bsummer\s*10\s*(?:wk|week)\b|暑期10周|夏季10周", re.I),
        "Summer10wk",
    ),
    (
        re.compile(r"\bsummer\s*(?:session\s*)?2\b|暑期2|夏季2", re.I),
        "Summer2",
    ),
)


def _course_ids(text: str) -> tuple[str, ...]:
    result: list[str] = []
    for ref, _start, _end in iter_course_mentions(text or ""):
        course_id = colloquial_course_id(ref.department, ref.course_number)
        if course_id and course_id not in result:
            result.append(course_id)
    return tuple(result)


def _term(value: str) -> tuple[Optional[TermKey], Optional[TermParseError]]:
    parsed = parse_term_key(str(value or ""))
    if parsed.kind == "single":
        return parsed.terms[0], None
    return None, parsed.error or TermParseError(
        "invalid",
        "expected one canonical default term",
        str(value or ""),
    )


def _focus_terms(focus: Optional[Mapping[str, object]]) -> tuple[TermKey, ...]:
    terms: list[TermKey] = []
    for raw in ((focus or {}).get("terms") or []):
        parsed = parse_term_key(str(raw))
        if parsed.kind == "single" and parsed.terms[0] not in terms:
            terms.append(parsed.terms[0])
        if len(terms) == 3:
            break
    return tuple(terms)


def _relative_year_offset(match: re.Match[str]) -> int:
    token = (match.group("cn") or match.group("en") or "").lower()
    if token in {"去年", "last"}:
        return -1
    if token in {"明年", "next"}:
        return 1
    return 0


def _quarter_signals(text: str) -> tuple[str, ...]:
    found: list[tuple[int, str]] = []
    for pattern, quarter in _QUARTER_SIGNALS:
        for match in pattern.finditer(text):
            found.append((match.start(), quarter))
    found.sort()
    return tuple(dict.fromkeys(quarter for _, quarter in found))


def _error_scope(
    error: TermParseError,
    course_ids: tuple[str, ...],
) -> QueryScope:
    return QueryScope((), "default", False, True, error, course_ids)


def resolve_query_scope(
    user_message: str,
    default_term: str,
    automatic_term: str,
    recent_query_focus: Optional[Mapping[str, object]],
    uci_now: datetime,
) -> QueryScope:
    """Resolve the immutable term allowlist for one request."""
    text = user_message or ""
    courses = _course_ids(text)
    default, default_error = _term(default_term)
    automatic, automatic_error = _term(automatic_term)
    if default_error is not None:
        return _error_scope(default_error, courses)
    if automatic_error is not None:
        return _error_scope(automatic_error, courses)
    assert default is not None and automatic is not None

    # Complete terms always win, including explicit cross-term comparisons.
    explicit_terms = extract_explicit_term_keys(text)
    if explicit_terms:
        source: QueryTermSource = (
            "comparison"
            if len(explicit_terms) > 1 or _COMPARE.search(text)
            else "explicit"
        )
        return QueryScope(explicit_terms, source, True, False, None, courses)

    focus_terms = _focus_terms(recent_query_focus)
    if _FOCUS_TERMS.search(text):
        if len(focus_terms) >= 2:
            return QueryScope(
                focus_terms,
                "comparison",
                False,
                False,
                None,
                courses,
            )
        return _error_scope(
            TermParseError(
                "ambiguous",
                "the referenced terms are not available in recent query focus",
                text,
            ),
            courses,
        )
    if _FOCUS_SINGLE.search(text) and focus_terms:
        selected = focus_terms if _COMPARE.search(text) else focus_terms[-1:]
        return QueryScope(selected, "followup", False, False, None, courses)

    # "Current term" is explicitly the backend automatic term.
    if _CURRENT_TERM.search(text):
        return QueryScope((automatic,), "relative", False, False, None, courses)

    for pattern, direction in ((_NEXT_TERM, 1), (_PREVIOUS_TERM, -1)):
        if pattern.search(text):
            if not default.is_regular:
                return _error_scope(
                    TermParseError(
                        "ambiguous",
                        "adjacent terms require a regular Fall/Winter/Spring default",
                        default.canonical_name,
                    ),
                    courses,
                )
            relative = (
                default.next_regular()
                if direction > 0
                else default.previous_regular()
            )
            return QueryScope((relative,), "relative", False, False, None, courses)

    year_match = _RELATIVE_YEAR.search(text)
    if year_match:
        quarters = _quarter_signals(text)
        if not quarters:
            if not default.is_regular:
                return _error_scope(
                    TermParseError(
                        "ambiguous",
                        "relative year without a quarter cannot inherit a Summer default",
                        year_match.group(0),
                    ),
                    courses,
                )
            quarters = (default.quarter,)
        year = uci_now.year + _relative_year_offset(year_match)
        terms = tuple(TermKey(year, quarter) for quarter in quarters)
        source = "comparison" if len(terms) > 1 else "relative"
        return QueryScope(terms, source, False, False, None, courses)

    # Preserve the canonical parser's useful quarter-without-year ambiguity.
    residual = parse_term_text(text)
    if residual.error is not None:
        return _error_scope(residual.error, courses)

    return QueryScope((default,), "default", False, False, None, courses)


def next_recent_focus_terms(
    user_message: str,
    scope: QueryScope,
    previous_focus: Optional[Mapping[str, object]],
) -> tuple[str, ...]:
    """Build the structured term focus used by a later pronoun follow-up.

    ``那 2025 Fall 呢？`` queries only 2025 Fall in its own turn, but keeps
    the immediately previous term beside it so ``这两个学期`` can be resolved
    deterministically on the next turn. A standalone explicit term resets the
    focus instead of silently accumulating conversation history.
    """
    current = list(scope.canonical_terms)
    if (
        scope.source == "explicit"
        and len(current) == 1
        and _FOLLOWUP_CONNECTOR.search(user_message or "")
    ):
        previous = [term.canonical_name for term in _focus_terms(previous_focus)]
        if previous and previous[-1] != current[0]:
            current = [previous[-1], current[0]]
    return tuple(current[:3])
