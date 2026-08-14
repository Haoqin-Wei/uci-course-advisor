"""Deterministic parsing of explicit and relative UCI term references."""

from __future__ import annotations

import re
from typing import Optional

from app.terms.models import TermKey, TermParseError, TermParseResult


_QUARTER_ALIASES = {
    "fall": "Fall",
    "autumn": "Fall",
    "秋": "Fall",
    "秋季": "Fall",
    "秋学期": "Fall",
    "winter": "Winter",
    "冬": "Winter",
    "冬季": "Winter",
    "冬学期": "Winter",
    "spring": "Spring",
    "春": "Spring",
    "春季": "Spring",
    "春学期": "Spring",
    "summer1": "Summer1",
    "summer 1": "Summer1",
    "summer session 1": "Summer1",
    "暑期1": "Summer1",
    "夏季1": "Summer1",
    "summer10wk": "Summer10wk",
    "summer 10wk": "Summer10wk",
    "summer 10 week": "Summer10wk",
    "暑期10周": "Summer10wk",
    "夏季10周": "Summer10wk",
    "summer2": "Summer2",
    "summer 2": "Summer2",
    "summer session 2": "Summer2",
    "暑期2": "Summer2",
    "夏季2": "Summer2",
}

_ALIAS_PATTERN = "|".join(
    sorted((re.escape(alias) for alias in _QUARTER_ALIASES), key=len, reverse=True)
)
_EXPLICIT_PATTERNS = (
    re.compile(rf"(?<!\d)(?P<year>20\d{{2}})\s*(?:年)?\s*[_-]?\s*(?P<quarter>{_ALIAS_PATTERN})(?![A-Za-z0-9])", re.I),
    re.compile(rf"(?<![A-Za-z0-9])(?P<quarter>{_ALIAS_PATTERN})\s*[_-]?\s*(?P<year>20\d{{2}})(?!\d)", re.I),
)

_YEAR_TOKEN_PATTERN = r"(?:20\d{2}|\d{2})"
_YEAR_PAIR_CONNECTOR = r"(?:和|与|及|、|/|&|,|，|and)"
_SHARED_QUARTER_PATTERNS = (
    re.compile(
        rf"(?<![A-Za-z0-9])"
        rf"(?P<year1>{_YEAR_TOKEN_PATTERN})\s*(?:年)?\s*"
        rf"{_YEAR_PAIR_CONNECTOR}\s*"
        rf"(?P<year2>{_YEAR_TOKEN_PATTERN})\s*(?:年)?\s*[_-]?\s*"
        rf"(?P<quarter>{_ALIAS_PATTERN})(?![A-Za-z0-9])",
        re.I,
    ),
    re.compile(
        rf"(?<![A-Za-z0-9])(?P<quarter>{_ALIAS_PATTERN})\s*[_-]?\s*"
        rf"(?P<year1>{_YEAR_TOKEN_PATTERN})\s*(?:年)?\s*"
        rf"{_YEAR_PAIR_CONNECTOR}\s*"
        rf"(?P<year2>{_YEAR_TOKEN_PATTERN})(?:\s*年)?(?!\d)",
        re.I,
    ),
)
_SHORT_YEAR_TERM_PATTERNS = (
    re.compile(
        rf"(?<!\d)\d{{2}}\s*(?:年)?\s*[_-]?\s*"
        rf"(?:{_ALIAS_PATTERN})(?![A-Za-z0-9])",
        re.I,
    ),
    re.compile(
        rf"(?<![A-Za-z0-9])(?:{_ALIAS_PATTERN})\s*[_-]?\s*"
        rf"\d{{2}}(?!\d)",
        re.I,
    ),
)

_RELATIVE_PATTERNS = (
    (re.compile(r"当前学期|本学期|这个学期|this\s+(?:term|quarter)", re.I), 0, True),
    (re.compile(r"下学期|下一学期|下个学期|next\s+(?:term|quarter)", re.I), 1, False),
    (re.compile(r"上学期|上一学期|上个学期|previous\s+(?:term|quarter)|last\s+(?:term|quarter)", re.I), -1, False),
)

_AMBIGUOUS_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:fall|autumn|winter|spring|summer)(?!\s*(?:session\s*)?(?:1|2|10\s*(?:wk|week))?\s*20\d{2})"
    r"|(?:秋季?|冬季?|春季?|暑期|夏季)(?!\s*(?:1|2|10周)?\s*20\d{2}|\s*20\d{2})",
    re.I,
)


def _quarter(alias: str) -> str:
    return _QUARTER_ALIASES[re.sub(r"\s+", " ", alias.strip().lower())]


def _year(value: str) -> int:
    raw = value.strip()
    return int(raw) if len(raw) == 4 else 2000 + int(raw)


def _explicit_term_matches(text: str) -> list[tuple[int, int, TermKey]]:
    complete_matches: list[tuple[int, int, TermKey]] = []
    for pattern in _EXPLICIT_PATTERNS:
        for match in pattern.finditer(text):
            try:
                key = TermKey(_year(match.group("year")), _quarter(match.group("quarter")))
            except (KeyError, ValueError):
                continue
            span = match.span()
            if not any(
                span[0] < end and start < span[1]
                for start, end, _ in complete_matches
            ):
                complete_matches.append((span[0], span[1], key))

    matches: list[tuple[int, int, TermKey]] = []

    # A shared quarter makes two abbreviated years unambiguous enough to
    # expand deterministically: "25和26winter" means Winter 2025 + 2026.
    # Requiring the connector and quarter prevents course numbers such as
    # "ECON 25 winter" from being silently treated as a year.
    for pattern in _SHARED_QUARTER_PATTERNS:
        for match in pattern.finditer(text):
            try:
                quarter = _quarter(match.group("quarter"))
                keys = (
                    TermKey(_year(match.group("year1")), quarter),
                    TermKey(_year(match.group("year2")), quarter),
                )
            except (KeyError, ValueError):
                continue
            start, end = match.span()
            # Do not carry one quarter across two years when either year
            # already belongs to a complete term with a different quarter,
            # e.g. "Fall 2026 与 2027 Spring".
            if any(
                start < complete_end
                and complete_start < end
                and complete_key.quarter != quarter
                for complete_start, complete_end, complete_key in complete_matches
            ):
                continue
            for key in keys:
                if not any(
                    existing_start == start
                    and existing_end == end
                    and existing_key == key
                    for existing_start, existing_end, existing_key in matches
                ):
                    matches.append((start, end, key))

    for start, end, key in complete_matches:
        if not any(start < found_end and found_start < end for found_start, found_end, _ in matches):
            matches.append((start, end, key))
    matches.sort(key=lambda item: item[0])
    return matches


def extract_explicit_term_keys(text: str) -> tuple[TermKey, ...]:
    """Return complete year+quarter references without resolving relatives."""
    if not isinstance(text, str) or not text.strip():
        return ()
    matches = _explicit_term_matches(text)
    return tuple(dict.fromkeys(key for _, _, key in matches))


def parse_term_key(value: str) -> TermParseResult:
    """Parse a value that must contain exactly one explicit term."""
    result = parse_term_text(value, require_term=True)
    if result.kind == "multi":
        return TermParseResult(
            error=TermParseError("ambiguous", "expected one term but found multiple", value),
        )
    return result


def parse_term_text(
    text: str,
    *,
    automatic_term: Optional[TermKey] = None,
    require_term: bool = False,
) -> TermParseResult:
    """Extract canonical term references from a user message.

    Relative references are always based on ``automatic_term``. A pinned
    conversation term must never be supplied as that base.
    """
    if not isinstance(text, str) or not text.strip():
        error = TermParseError("invalid", "term text is empty", str(text)) if require_term else None
        return TermParseResult(error=error)

    matches = _explicit_term_matches(text)

    reset_to_auto = False
    for pattern, offset, reset in _RELATIVE_PATTERNS:
        for match in pattern.finditer(text):
            if automatic_term is None:
                return TermParseResult(
                    error=TermParseError(
                        "missing_base",
                        "relative term requires the current automatic term",
                        match.group(0),
                    )
                )
            if not automatic_term.is_regular:
                return TermParseResult(
                    error=TermParseError(
                        "invalid",
                        "automatic term must be Fall, Winter, or Spring",
                        automatic_term.canonical_name,
                    )
                )
            key = automatic_term
            if offset > 0:
                key = key.next_regular()
            elif offset < 0:
                key = key.previous_regular()
            matches.append((match.start(), match.end(), key))
            reset_to_auto = reset_to_auto or reset

    matches.sort(key=lambda item: item[0])
    terms: list[TermKey] = []
    for _, _, key in matches:
        if key not in terms:
            terms.append(key)

    scrubbed = text
    matched_spans = sorted({(start, end) for start, end, _ in matches}, reverse=True)
    for start, end in matched_spans:
        scrubbed = scrubbed[:start] + (" " * (end - start)) + scrubbed[end:]
    short_year = next(
        (pattern.search(scrubbed) for pattern in _SHORT_YEAR_TERM_PATTERNS if pattern.search(scrubbed)),
        None,
    )
    if short_year:
        return TermParseResult(
            error=TermParseError(
                "ambiguous",
                "a standalone two-digit year requires clarification or a four-digit year",
                short_year.group(0),
            )
        )
    ambiguous = _AMBIGUOUS_PATTERN.search(scrubbed)
    if ambiguous:
        return TermParseResult(
            error=TermParseError(
                "ambiguous",
                "quarter reference requires a four-digit year",
                ambiguous.group(0),
            )
        )

    if not terms and require_term:
        return TermParseResult(
            error=TermParseError("invalid", "could not map term to a supported UCI quarter", text),
        )
    return TermParseResult(tuple(terms), reset_to_auto=reset_to_auto)
