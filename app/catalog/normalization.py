"""
Normalization — turn free text into canonical CourseRef / instructor names.

Used by:
  - Loaders (to ingest raw data into canonical refs)
  - Validators (to interpret LLM output)
  - Optionally chat.py for user input

Design rule: ALL course/instructor regex lives here. Validator/loader
code never writes its own regex against course-mention text.
"""

from __future__ import annotations
import re
from typing import Optional

from app.catalog.types import CourseRef
from app.catalog.departments import resolve_department, known_departments


# ── Course ID extraction ─────────────────────────────────

# Matches "COMPSCI 122B", "compsci 122b", "CS33" (no space), "SOC SCI 178C",
# "I&C SCI 33", "CRM/LAW C214", "FLM&MDA 160", "IN4MATX 124", "MATH 2D",
# "ANTHRO 41A", "EECS 170LB", "BIO SCI N165", "HUMAN H1CS", "IN4MATX43".
#
# Dept token rules:
#   - At least 2 chars (no real UCI dept is single-letter; requiring 2+
#     prevents eating a leading course-number letter like 'C214' as dept)
#   - Starts with a letter
#   - Can contain digits in the middle (e.g. IN4MATX)
#   - Must END with a letter, & or / — never a digit. This lets
#     "IN4MATX43" split cleanly into dept="IN4MATX" + num="43".
#
# Number side is digit-anchored. Dept validated against alias table after.
_DEPT_TOKEN = r"[A-Za-z][A-Za-z0-9&/]*[A-Za-z&/]"   # 2+ chars, ends with letter/&//

# Use ASCII-only boundaries. Python's ``\b`` is Unicode-aware, so a
# Chinese character next to ``CS122A`` is treated as another word
# character and ``我想选CS122A这门课`` would not match. These look-arounds
# only reject adjacent ASCII identifier chars; CJK neighbors are valid.
_COURSE_MENTION = re.compile(
    r"(?<![A-Za-z0-9_&/])"
    r"(?P<dept>" + _DEPT_TOKEN + r"(?:\s+" + _DEPT_TOKEN + r")?)"   # 1 or 2 tokens
    r"\s*"
    r"(?P<num>[A-Z]?\d{1,3}[A-Z0-9/]{0,5})"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def parse_course_mention(text: str) -> Optional[CourseRef]:
    """Try to interpret a single fragment as a CourseRef.

    Best used on short fragments ('COMPSCI 122B', 'ICS33'). For finding
    mentions inside long LLM output, use iter_course_mentions() instead.
    """
    if not text:
        return None
    m = _COURSE_MENTION.search(text)
    if not m:
        return None
    return _build_ref(m.group("dept"), m.group("num"))


def iter_course_mentions(text: str) -> list[tuple[CourseRef, int, int]]:
    """Find every course mention in `text`.

    Returns list of (ref, char_start, char_end). Overlapping or
    duplicate refs are returned as found — the caller decides how
    to dedup.
    """
    out: list[tuple[CourseRef, int, int]] = []
    for m in _COURSE_MENTION.finditer(text or ""):
        ref = _build_ref(m.group("dept"), m.group("num"))
        if ref is not None:
            out.append((ref, m.start(), m.end()))
    return out


def _build_ref(dept_raw: str, num_raw: str) -> Optional[CourseRef]:
    """Resolve a raw (dept_text, num_text) pair to a canonical CourseRef.

    Falls back to single-token resolution when the regex greedily
    captured a junk prefix (e.g. 'taking COMPSCI 122B' → dept_raw =
    'taking COMPSCI' → fallback to last token 'COMPSCI').

    Returns None if dept can't be resolved at all.
    """
    canonical = resolve_department(dept_raw)
    if canonical is None and " " in dept_raw:
        # Try the LAST token (handles "taking COMPSCI", "the COMPSCI", ...)
        canonical = resolve_department(dept_raw.rsplit(None, 1)[-1])
    if canonical is None and " " in dept_raw:
        # Try the FIRST token (handles "COMPSCI here", though rare)
        canonical = resolve_department(dept_raw.split(None, 1)[0])
    if canonical is None:
        # Last resort: collapse whitespace (handles "i & c sci" → "i&csci")
        compact = "".join(dept_raw.split()).lower()
        for c in known_departments():
            if "".join(c.split()).lower() == compact:
                canonical = c
                break
    if canonical is None:
        return None
    return CourseRef(department=canonical, course_number=num_raw.upper())


# ── Instructor name handling ─────────────────────────────

# UCI canonical: "LASTNAME, F." — uppercase, last-name first, initial(s)
# with trailing period. May have multi-token last names ("DE SOUZA SANTO,
# V.") or compound initials ("LEE, J.K.").
_INSTRUCTOR_CANONICAL = re.compile(
    r"^([A-Z][A-Z' \-]*),\s*([A-Z](?:\.[A-Z])*\.?)?\s*$"
)


def normalize_instructor(name: str) -> str:
    """Aggressively normalize an instructor name string.

    Uppercases, collapses whitespace. Does NOT enforce the canonical
    'LAST, F.' format — passes through if format is unusual.
    """
    return " ".join((name or "").upper().split())


def instructor_last_name(canonical: str) -> Optional[str]:
    """Extract last name from a UCI canonical instructor string.

    Returns None if the input doesn't look like a name at all.
    """
    if not canonical:
        return None
    m = _INSTRUCTOR_CANONICAL.match(canonical.strip())
    if m:
        return m.group(1).strip()
    # Fallback: take everything before the first comma
    if "," in canonical:
        out = canonical.split(",", 1)[0].strip().upper()
        return out or None
    return None


# ── LLM-output instructor extraction ─────────────────────

# Find professor mentions in free LLM text. We require an explicit
# title keyword to reduce false positives — bare last names like
# "Thornton" are too ambiguous.
#
# Three patterns combined:
#
# A. Title-then-name (English + Chinese, left-positioned title):
#      "Professor Smith"
#      "教授 Smith"  /  "教授是 Smith"
#      "教授（比如 Smith）"  /  "教授, Smith"
#
# B. Name-then-title (Chinese, right-positioned title):
#      "Smith 教授"
#
# C. Continuation: after a name in pattern A, scan forward for
#    "或 Y" / "and Y" / "及 Y" — common when LLM lists multiple profs.
#      "教授是 X 或 Y"  → captures both X and Y
#
# Common false-positive avoided: bare capitalized words in CJK text
# (course names, places). Title keyword is required either before or
# after the name.

# Allow up to 12 non-Latin chars between the title and the name —
# this covers "教授（比如", "老师，", "讲师是" etc.
_PROF_MENTION_TITLE_FIRST = re.compile(
    r"""(?:
        \b(?:Professor|Prof\.?|Instructor)\s+
        |
        (?:教授|老师|讲师|授课老师)[^A-Za-z\n]{0,12}
    )
    (?P<name>[A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+){0,2})
    (?![A-Za-z\-'])
    """,
    re.VERBOSE,
)

_PROF_MENTION_NAME_FIRST = re.compile(
    r"""(?<![A-Za-z\-'])
    (?P<name>[A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+){0,2})
    \s*(?:教授|老师|讲师)
    """,
    re.VERBOSE,
)

# After a title-first match, scan up to ~60 chars for additional names
# joined by 或/and/及/与. Common LLM pattern: "教授是 X 或 Y" lists two.
_FOLLOWUP_NAME = re.compile(
    r"""(?:或者?|and|及|与|,|，)\s*
    (?P<name>[A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+){0,2})
    (?![A-Za-z\-'])
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _surname_from_match(name_str: str) -> Optional[str]:
    """Extract a likely surname from a multi-token captured name.

    Returns None for common false-positive tokens (titles, articles)
    that occasionally slip through the regex when 'Prof' itself or a
    similar word ends up as the captured name.
    """
    tokens = name_str.split()
    tokens = [t.rstrip(".,") for t in tokens if t.strip(".,")]
    if not tokens:
        return None
    surname = tokens[-1].upper()
    if surname.endswith("'S"):
        surname = surname[:-2]
    if surname in _EXCLUDED_AS_SURNAME:
        return None
    return surname


# Tokens that occasionally get captured as "surname" but aren't.
_EXCLUDED_AS_SURNAME: set[str] = {
    "PROF", "PROFESSOR", "INSTRUCTOR", "TEACHER", "LECTURER",
    "DR", "MR", "MS", "MRS",
    "THE", "AND", "OR", "OF", "BY", "WITH", "FROM",
    "TBA", "STAFF",
}


def iter_instructor_mentions(text: str) -> list[tuple[str, int, int]]:
    """Find every professor mention in `text`.

    Returns list of (surname_guess, start, end). Each mention requires
    an explicit Professor / 教授 / 老师 / 讲师 keyword. Continuation
    names ("教授是 X 或 Y") are picked up by scanning forward after
    each title-first match.
    """
    if not text:
        return []
    out: list[tuple[str, int, int]] = []
    seen_spans: set[tuple[int, int]] = set()

    # Pattern A: title-first (with follow-up scan)
    for m in _PROF_MENTION_TITLE_FIRST.finditer(text):
        span = (m.start(), m.end())
        if span in seen_spans:
            continue
        seen_spans.add(span)

        surname = _surname_from_match(m.group("name"))
        if surname:
            out.append((surname, m.start(), m.end()))

        # Look ~60 chars ahead for "或 Y" / "and Y" continuations
        tail = text[m.end():m.end() + 60]
        for follow in _FOLLOWUP_NAME.finditer(tail):
            f_start = m.end() + follow.start()
            f_end = m.end() + follow.end()
            f_span = (f_start, f_end)
            if f_span in seen_spans:
                continue
            seen_spans.add(f_span)
            f_surname = _surname_from_match(follow.group("name"))
            if f_surname:
                out.append((f_surname, f_start, f_end))

    # Pattern B: name-then-title (no follow-up needed)
    for m in _PROF_MENTION_NAME_FIRST.finditer(text):
        span = (m.start(), m.end())
        if span in seen_spans:
            continue
        seen_spans.add(span)
        surname = _surname_from_match(m.group("name"))
        if surname:
            out.append((surname, m.start(), m.end()))

    return out
