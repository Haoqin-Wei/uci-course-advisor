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
    "inferred",
    "history",
]


@dataclass(frozen=True)
class QueryScope:
    terms: tuple[TermKey, ...]
    source: QueryTermSource
    explicit: bool
    ambiguous: bool
    error: Optional[TermParseError]
    course_ids: tuple[str, ...]
    intent: str = "lookup"
    inferred_year: bool = False

    @property
    def canonical_terms(self) -> tuple[str, ...]:
        return tuple(term.canonical_name for term in self.terms)


_CURRENT_TERM = re.compile(
    r"当前学期|本学期|current\s+(?:term|quarter)|this\s+quarter",
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
        if len(terms) == 12:
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


_HISTORY = re.compile(
    r"近[两二2]年|过去[两二2]年|往年|历年|开课规律|通常|一般.*开|"
    r"(?:哪个|哪些|什么)学期.*开|只(?:在|有).*开|什么时候开(?:课|设|[？?]|$)|"
    r"以前.*(?:教|开)|过去.*(?:教|开)|"
    r"historical|historically|usually|typically|offering pattern|"
    r"(?:which|what) (?:terms|quarters)|only (?:offered|in)|past (?:two|2) years",
    re.I,
)
_PAST = re.compile(r"以前|之前|过去|上次|上个|上一|开过|教过|曾经|\b(?:previous|last|was|were)\b|used to", re.I)
_SUMMER = re.compile(r"summer|暑期|夏季", re.I)
_ORDER = {"Winter": 0, "Spring": 1, "Fall": 2}


def recent_regular_terms(anchor: TermKey, count: int = 6) -> tuple[TermKey, ...]:
    terms = []
    for _ in range(count):
        anchor = anchor.previous_regular()
        terms.append(anchor)
    return tuple(reversed(terms))


def _infer_quarter(quarter: str, anchor: TermKey, *, past: bool = False) -> TermKey:
    year = anchor.year
    if past:
        if _ORDER[quarter] >= _ORDER[anchor.quarter]:
            year -= 1
    elif _ORDER[quarter] < _ORDER[anchor.quarter]:
        year += 1
    return TermKey(year, quarter)


def resolve_query_scope(
    user_message: str,
    default_term: str,
    automatic_term: str,
    recent_query_focus: Optional[Mapping[str, object]],
    uci_now: datetime,
    *,
    current_term: Optional[str] = "",
    relative_base: Optional[str] = None,
) -> QueryScope:
    """Resolve target terms; historical evidence never replaces those targets."""
    text = user_message or ""
    courses = _course_ids(text)
    default, error = _term(default_term)
    if error:
        return _error_scope(error, courses)
    automatic, error = _term(automatic_term)
    if error:
        return _error_scope(error, courses)
    current, _ = _term(current_term if current_term != "" else automatic_term)
    base, _ = _term(relative_base or (current.canonical_name if current else automatic_term))
    assert default and automatic and base
    if _SUMMER.search(text):
        return _error_scope(TermParseError("invalid", "Summer is not supported yet; use Fall, Winter or Spring.", text), courses)

    explicit_terms = extract_explicit_term_keys(text)
    focus_terms = _focus_terms(recent_query_focus)
    if any(not t.is_regular for t in explicit_terms):
        return _error_scope(TermParseError("invalid", "Summer is not supported yet.", text), courses)

    # Collect every explicit/relative mention, including mixed comparisons.
    mentions: list[tuple[int, TermKey]] = []
    for key in explicit_terms:
        year_at = text.lower().find(str(key.year))
        mentions.append((max(0, year_at), key))
    for pattern, offset in ((_CURRENT_TERM, 0), (_NEXT_TERM, 1), (_PREVIOUS_TERM, -1)):
        for match in pattern.finditer(text):
            if offset == 0 and current is None:
                return _error_scope(TermParseError("missing_base", "UCI is between regular quarters; there is no current Fall/Winter/Spring term.", text), courses)
            key = current if offset == 0 else (base.next_regular() if offset > 0 else base.previous_regular())
            mentions.append((match.start(), key))
    if mentions:
        terms = tuple(dict.fromkeys(key for _, key in sorted(mentions, key=lambda item: item[0])))
        source = "comparison" if len(terms) > 1 else "explicit" if explicit_terms else "relative"
        return QueryScope(terms, source, bool(explicit_terms), False, None, courses,
                          intent="comparison" if len(terms) > 1 else "lookup")

    if _HISTORY.search(text):
        return QueryScope(recent_regular_terms(current or automatic), "history", False, False, None,
                          courses or tuple((recent_query_focus or {}).get("course_ids") or []),
                          intent="offering_pattern")

    year_match = _RELATIVE_YEAR.search(text)
    if year_match:
        quarters = _quarter_signals(text) or (default.quarter,)
        year = uci_now.year + _relative_year_offset(year_match)
        terms = tuple(TermKey(year, quarter) for quarter in quarters)
        return QueryScope(terms, "comparison" if len(terms) > 1 else "relative", False, False, None, courses)

    # A lone two-digit year could be a course number; retain the parser's
    # clarification instead of silently ignoring the digits.
    residual = parse_term_text(text)
    if residual.error and re.search(r"(?<![0-9])\d{2}\s*(?:winter|fall|spring)", text, re.I):
        return _error_scope(residual.error, courses)
    quarters = _quarter_signals(text)
    if quarters:
        anchor = focus_terms[-1] if focus_terms else current or automatic
        terms = tuple(_infer_quarter(q, anchor, past=bool(_PAST.search(text))) for q in quarters)
        if re.search(r"next\s+(?:fall|winter|spring)|下(?:个|一个)(?:秋|冬|春)", text, re.I):
            terms = tuple(TermKey(t.year + 1, t.quarter) if current and t == current else t for t in terms)
        return QueryScope(terms, "comparison" if len(terms) > 1 else "inferred", False, False, None,
                          courses, intent="comparison" if len(terms) > 1 else "lookup", inferred_year=True)

    if _FOCUS_TERMS.search(text):
        if len(focus_terms) >= 2:
            return QueryScope(focus_terms, "comparison", False, False, None, courses, intent="comparison")
        return _error_scope(TermParseError("ambiguous", "The referenced terms are not available in recent discussion.", text), courses)
    if focus_terms and (_FOCUS_SINGLE.search(text) or _FOLLOWUP_CONNECTOR.search(text)
                        or text.strip().lower() in {"继续", "continue", "yes", "好", "好的"}):
        selected = focus_terms
        if re.search(r"这个学期|那个学期|this\s+term|that\s+term", text, re.I) and not _COMPARE.search(text):
            selected = focus_terms[-1:]
        intent = (recent_query_focus or {}).get("intent")
        if intent != "offering_pattern":
            intent = "comparison" if len(selected) > 1 else "lookup"
        return QueryScope(selected, "followup", False, False, None, courses, intent=intent)
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
    return tuple(current[:12])
