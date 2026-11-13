"""
Local RateMyProfessor data: ratings + reviews + tag aggregates.

Two artifacts back this module:

    data/professor/uci_professors.json          ── small (~1.5MB), loaded
                                                   once into memory at import
    data/professor/professor_reviews.db (SQLite) ── ~37MB, queried lazily
                                                   by legacy_id (+ class)

The professor JSON gives us avgRating / avgDifficulty / wouldTakeAgain%
per instructor. The reviews DB carries per-comment detail used by the
agent's review-summarization tool.

Lookups are EXACT only:
    "KING, S."        → split on comma → (lastname=king, initial=s)
    "Susan King"      → tokenize       → (lastname=king, initial=s)
    "king"            → lastname-only; ambiguity = miss
    legacyId=168167   → direct hash hit

scripts/verify_professor_data.py audits the hit rate against the full
sections.csv instructor list — if precision-only matching is too lossy
we'll add fuzzy/department-tiebreak then.

Tier rules (Steam-style three-bucket + sample-size floor):
    mostly_positive   ── avgRating ≥ 4.2  AND numRatings ≥ 10
    mostly_negative   ── avgRating ≤ 2.5  AND numRatings ≥ 10
    insufficient_data ── numRatings < 5
    mixed             ── everything else
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Anchored to repo root so this works regardless of CWD.
_ROOT       = Path(__file__).resolve().parent.parent.parent
PROFS_JSON  = _ROOT / "data" / "professor" / "uci_professors.json"
REVIEWS_DB  = _ROOT / "data" / "professor" / "professor_reviews.db"


# ══════════════════════════════════════════════════════════
#  Tier classification
# ══════════════════════════════════════════════════════════

TIER_THRESHOLDS = {
    "positive_min_rating":     4.2,
    "negative_max_rating":     2.5,
    "tier_min_sample":         10,   # below this we won't say good/bad
    "insufficient_max_sample": 4,    # 1-4 ratings = sample too thin
}

TIER_LABELS = {
    "mostly_positive":   {"en": "Mostly Positive",   "zh": "好评如潮"},
    "mostly_negative":   {"en": "Mostly Negative",   "zh": "差评如潮"},
    "mixed":             {"en": "Mixed",             "zh": "褒贬不一"},
    "insufficient_data": {"en": "Insufficient Data", "zh": "样本不足"},
    "unrated":           {"en": "Unrated",           "zh": "暂无评分"},
}


def classify_tier(avg_rating: Optional[float], num_ratings: Optional[int]) -> dict:
    """Return {tier, label_en, label_zh, avg_rating, num_ratings}."""
    n = num_ratings or 0
    r = avg_rating

    if r is None or n == 0:
        tier = "unrated"
    elif n <= TIER_THRESHOLDS["insufficient_max_sample"]:
        tier = "insufficient_data"
    elif r >= TIER_THRESHOLDS["positive_min_rating"] and n >= TIER_THRESHOLDS["tier_min_sample"]:
        tier = "mostly_positive"
    elif r <= TIER_THRESHOLDS["negative_max_rating"] and n >= TIER_THRESHOLDS["tier_min_sample"]:
        tier = "mostly_negative"
    else:
        tier = "mixed"

    return {
        "tier":        tier,
        "label_en":    TIER_LABELS[tier]["en"],
        "label_zh":    TIER_LABELS[tier]["zh"],
        "avg_rating":  r,
        "num_ratings": n,
    }


# ══════════════════════════════════════════════════════════
#  Section-code → RMP department name map
# ══════════════════════════════════════════════════════════
#
# sections.csv uses compact UCI catalog codes ('COMPSCI', 'BIO SCI',
# 'PUBHLTH'); the RMP snapshot stores spelled-out academic department
# names ('Computer Science', 'Biological Sciences', 'Public Health').
# We use this map for department-tiebreak when multiple professors
# share (lastname, first-initial). A section-code maps to a *set* of
# RMP department substrings — a candidate matches if ANY substring is
# contained in the candidate's lowercased department.
#
# Built from the union of observed section codes and RMP department
# strings. Unknown section codes fall back to whole-word token overlap.

_SECTION_TO_RMP_DEPT: dict[str, tuple[str, ...]] = {
    # Computing & Info
    # Don't include the generic "engineering" fragment for codes that
    # ought to mean a specific sub-discipline — it would let a section
    # in EECS match a Mechanical Engineering professor.
    "COMPSCI":  ("computer science", "informatics", "computer"),
    "I&C SCI":  ("computer science", "informatics", "information"),
    "IN4MATX":  ("informatics", "information"),
    "EECS":     ("electrical engineering", "computer science", "electrical", "computer"),
    "CSE":      ("computer science", "computer engineering"),
    "SWE":      ("software engineering", "computer science"),
    "STATS":    ("statistics", "mathematics"),

    # Math / Physical Sciences
    "MATH":     ("mathematics", "math"),
    "PHYSICS":  ("physics", "astronomy"),
    "CHEM":     ("chemistry",),
    "EARTHSS":  ("earth", "earth system"),

    # Biological Sciences
    "BIO SCI":  ("biological sciences", "biology"),
    "MOL BIO":  ("molecular biology", "biology", "biological"),
    "DEV BIO":  ("developmental biology", "biology", "biological"),
    "ECO EVO":  ("ecology", "biology", "biological"),
    "NEURBIO":  ("neurobiology", "biology", "neuroscience"),
    "BIOCHEM":  ("biochemistry", "biology", "chemistry"),
    "M&MG":     ("microbiology", "molecular biology", "biology"),
    "ANATOMY":  ("anatomy", "biology"),
    "PATH":     ("pathology", "biology"),

    # Engineering
    "ENGR":     ("engineering",),
    "ENGRMAE":  ("mechanical engineering", "aerospace", "engineering"),
    "ENGRCEE":  ("civil engineering", "environmental engineering", "engineering"),
    "BME":      ("biomedical engineering", "engineering"),
    "MSE":      ("materials science", "engineering"),
    "CBE":      ("chemical engineering", "engineering"),

    # Social Sciences
    "ECON":     ("economics",),
    "POL SCI":  ("political science",),
    "PSCI":     ("political science",),
    "PSYCH":    ("psychology", "psychology & social behavior"),
    "PSY BEH":  ("psychology & social behavior", "psychology"),
    "ANTHRO":   ("anthropology",),
    "SOCIOL":   ("sociology",),
    "SOCECOL":  ("social science", "social ecology", "psychology"),
    "COGS":     ("cognitive science", "psychology"),
    "INTL ST":  ("international studies", "political science"),
    "CRM/LAW":  ("criminal justice", "criminology"),

    # Humanities
    "ENGLISH":  ("english",),
    "WRITING":  ("writing",),
    "COM LIT":  ("comparative literature", "english", "humanities"),
    "HISTORY":  ("history",),
    "PHILOS":   ("philosophy",),
    "LPS":      ("philosophy", "logic", "logic  philosophy"),
    "HUMAN":    ("humanities",),
    "CLASSIC":  ("classics", "humanities"),
    "LIT JRN":  ("literary journalism", "english", "writing"),

    # Arts
    "ART":      ("art",),
    "ART HIS":  ("art history", "art"),
    "ARTS":     ("art",),
    "DRAMA":    ("dramatic arts", "drama", "theatre"),
    "DANCE":    ("dance",),
    "MUSIC":    ("music",),
    "FLM&MDA":  ("film", "media", "film and media studies"),

    # Languages
    "CHINESE":  ("chinese", "languages", "east asian"),
    "JAPANSE":  ("japanese", "languages", "east asian"),
    "KOREAN":   ("korean", "languages", "east asian"),
    "FRENCH":   ("french", "languages"),
    "GERMAN":   ("german", "languages"),
    "ITALIAN":  ("italian", "languages"),
    "SPANISH":  ("spanish", "languages"),
    "ARABIC":   ("arabic", "languages"),
    "LATIN":    ("latin", "classics", "languages"),
    "GREEK":    ("greek", "classics", "languages"),

    # Health
    "PUBHLTH":  ("public health", "health"),
    "NUR SCI":  ("nursing",),
    "PHRMSCI":  ("pharmaceutical sciences", "pharmacy"),
    "EPIDEM":   ("epidemiology", "public health"),
    "PHARM":    ("pharmacy", "pharmaceutical"),
    "EHS":      ("environmental health", "public health"),

    # Education / Management
    "EDUC":     ("education",),
    "MGMT":     ("management", "business"),
    "MGMTMBA":  ("business", "management"),
    "MGMTPHD":  ("management", "business"),
    "BANA":     ("business analytics", "business", "management"),
    "FIN":      ("finance", "business", "management"),

    # Misc
    "AFAM":     ("african american studies", "humanities"),
    "ASIANAM":  ("asian american studies", "humanities"),
    "GEN&SEX":  ("gender", "women's studies", "humanities"),
    "EURO ST":  ("european studies", "humanities"),
    "GLBLCLT":  ("global cultures", "humanities"),
}


def _dept_matches(rmp_dept: str | None, section_dept: str | None) -> bool:
    """True if a RMP dept name corresponds to a section dept code."""
    if not rmp_dept or not section_dept:
        return False
    rmp = rmp_dept.lower()
    code = section_dept.strip().upper()

    fragments = _SECTION_TO_RMP_DEPT.get(code)
    if fragments:
        return any(frag in rmp for frag in fragments)
    # Unknown code → fall back to whole-token overlap (e.g. "WRITING" → "writing")
    code_tokens = {t for t in code.lower().split() if len(t) > 2}
    return any(t in rmp for t in code_tokens)


# ══════════════════════════════════════════════════════════
#  In-memory professor index (built once, lazily)
# ══════════════════════════════════════════════════════════

_index_lock = threading.Lock()
_loaded                                          = False
_by_legacy_id:        dict[int, dict]            = {}
_by_last_initial:     dict[tuple[str, str], list[dict]] = {}   # (last, "k")  — first initial only
_by_last_initials:    dict[tuple[str, str], list[dict]] = {}   # (last, "kd") — concatenated initials of all firstName tokens
_by_lastname:         dict[str, list[dict]]      = {}


def _build_index() -> None:
    global _loaded
    if _loaded:
        return
    with _index_lock:
        if _loaded:
            return
        if not PROFS_JSON.exists():
            logger.warning("professor JSON not found at %s — local lookup disabled", PROFS_JSON)
            _loaded = True
            return
        try:
            data = json.loads(PROFS_JSON.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("failed to load %s: %s", PROFS_JSON, e)
            _loaded = True
            return

        for rec in data:
            legacy = rec.get("legacyId")
            if isinstance(legacy, int):
                _by_legacy_id[legacy] = rec

            last  = (rec.get("lastName")  or "").strip().lower()
            first = (rec.get("firstName") or "").strip()
            if not last:
                continue

            _by_lastname.setdefault(last, []).append(rec)
            if first:
                # First letter of the first given name. "J. B." → "j"; "Jean-Paul" → "j".
                m = re.search(r"[A-Za-z]", first)
                if m:
                    initial = m.group(0).lower()
                    _by_last_initial.setdefault((last, initial), []).append(rec)
                # Concatenated initials of all whitespace-separated tokens
                # in firstName, so "Kimberly Diane" → "kd" and "J. B." → "jb".
                # This lets queries like "EDWARDS, KD." disambiguate.
                full_initials = "".join(
                    tok[0].lower()
                    for tok in re.findall(r"[A-Za-z][A-Za-z\-']*", first)
                )
                if len(full_initials) >= 2:
                    _by_last_initials.setdefault((last, full_initials), []).append(rec)
        _loaded = True
        logger.info(
            "professor index built: %d profs, %d (last,initial) keys, %d (last,initials) keys",
            len(_by_legacy_id), len(_by_last_initial), len(_by_last_initials),
        )


# ══════════════════════════════════════════════════════════
#  Lookup
# ══════════════════════════════════════════════════════════

def _parse_query(q: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (lastname_lower, initials_lower) from any common form.

    `initials` is whatever letter cluster appears after the comma (or in
    the first token, for 'First Last' form), with dots/spaces stripped:
        "EDWARDS, KD."       → ("edwards", "kd")
        "EDWARDS, K. D."     → ("edwards", "kd")
        "KING, S."           → ("king",    "s")
        "Susan King"         → ("king",    "s")
        "king"               → ("king",    None)

    Returns (None, None) if nothing usable can be pulled out.
    """
    s = (q or "").strip()
    if not s:
        return None, None

    # "LASTNAME, F." / "LASTNAME, K. D." / "Lastname, First"
    if "," in s:
        last, _, rest = s.partition(",")
        last = last.strip().lower()
        # Pull the initials out of `rest`. Two shapes to handle:
        #   "F."       → "f"        — single initial
        #   "K. D."    → "kd"       — multi initial separated by spaces/dots
        #   "First"    → "f"        — given name → first letter only
        rest = rest.strip()
        if not rest:
            return (last or None, None)
        # Heuristic: if every alphabetic run in `rest` is 1 char long it's
        # a list of initials; concat them. Otherwise it's a given name —
        # use just the first letter.
        tokens = re.findall(r"[A-Za-z]+", rest)
        if tokens and all(len(t) <= 2 for t in tokens):
            # Every token is short → they ARE the initials. "KD" or
            # ["K","D"] both → "kd".
            initials = "".join(t.lower() for t in tokens)
        else:
            initials = tokens[0][0].lower() if tokens else ""
        return (last or None, initials or None)

    # "First Last" / "First Middle Last" / single token
    tokens = s.split()
    if len(tokens) == 1:
        return (tokens[0].lower(), None)
    last = tokens[-1].lower()
    # First token = given name → take just its first letter
    m = re.search(r"[A-Za-z]", tokens[0])
    return (last, m.group(0).lower() if m else None)


def lookup_professor(
    name_or_id: str,
    department: Optional[str] = None,
) -> Optional[dict]:
    """Return the raw professor JSON record, or None on miss/ambiguity.

    `department` is the section's UCI dept code ('COMPSCI', 'BIO SCI'),
    used to break ties when multiple professors share (lastname, initial).
    Pass it whenever you know the course context — without it, common
    surnames like 'LEE, J.' will miss because of ambiguity.
    """
    _build_index()
    s = (name_or_id or "").strip()
    if not s:
        return None

    if s.isdigit():
        return _by_legacy_id.get(int(s))

    last, initials = _parse_query(s)
    if not last:
        return None

    # Stage 1 — multi-initial exact match (handles "KD" → Kimberly Diane).
    if initials and len(initials) >= 2:
        hits = _by_last_initials.get((last, initials)) or []
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            picked = _resolve_with_department(hits, department, s)
            if picked is not None:
                return picked
        # else fall through to single-initial form below

    # Stage 2 — single-initial (first letter of firstName).
    if initials:
        first_letter = initials[0]
        hits = _by_last_initial.get((last, first_letter)) or []
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            picked = _resolve_with_department(hits, department, s)
            if picked is not None:
                return picked
        logger.debug("unresolved (last,initial) for %r dept=%r: %d records",
                     s, department, len(hits))
        return None

    # Stage 3 — lastname only, no initials provided.
    hits = _by_lastname.get(last) or []
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1 and department:
        picked = _resolve_with_department(hits, department, s)
        if picked is not None:
            return picked
    return None


def _resolve_with_department(
    candidates: list[dict],
    department: Optional[str],
    query_for_log: str,
) -> Optional[dict]:
    """If a department code uniquely picks one candidate, return it."""
    if not department:
        return None
    survivors = [c for c in candidates if _dept_matches(c.get("department"), department)]
    if len(survivors) == 1:
        return survivors[0]
    if len(survivors) > 1:
        # Prefer the candidate with the most ratings — more likely the
        # active faculty member rather than a one-quarter visitor.
        survivors.sort(key=lambda c: -(c.get("numRatings") or 0))
        top = survivors[0]
        # Only return if the top candidate is meaningfully more reviewed
        # than the runner-up; otherwise we'd be guessing.
        if (top.get("numRatings") or 0) >= 3 * ((survivors[1].get("numRatings") or 0) + 1):
            return top
        logger.debug("dept %r narrowed %r to %d but still ambiguous",
                     department, query_for_log, len(survivors))
    return None


# ══════════════════════════════════════════════════════════
#  Public helpers
# ══════════════════════════════════════════════════════════

def build_profile(record: dict) -> dict:
    """Normalize a raw JSON record into the agent-facing shape (includes tier)."""
    avg = record.get("avgRating")
    n   = record.get("numRatings") or 0
    tier = classify_tier(avg, n)
    return {
        "name":                 f"{record.get('firstName','')} {record.get('lastName','')}".strip(),
        "first_name":           record.get("firstName"),
        "last_name":            record.get("lastName"),
        "department":           record.get("department"),
        "legacy_id":            record.get("legacyId"),
        "avg_rating":           avg,
        "avg_difficulty":       record.get("avgDifficulty"),
        "would_take_again_pct": record.get("wouldTakeAgainPercent"),
        "num_ratings":          n,
        "tier":                 tier,
    }


def verify_rating_identity(requested_name: str, result: dict) -> dict:
    """Do not turn a conflicting full-name match into another person's rating.

    Canonical registrar initials keep their existing resolution behavior.
    An explicitly supplied full name requires the returned full name to
    agree; matching only surname and first initial is insufficient.
    """
    if not result.get("found"):
        return result
    requested = " ".join((requested_name or "").upper().split())
    if "," in requested:
        surname, given = requested.split(",", 1)
        full_name = any(len(token) > 2 for token in re.findall(r"[A-Z]+", given))
        expected = f"{given.strip()} {surname.strip()}"
    else:
        full_name = len(requested.split()) > 1
        expected = requested
    if not full_name:
        return result
    profile = result.get("instructor") or {}
    actual = profile.get("name") or " ".join(
        part for part in (profile.get("first_name"), profile.get("last_name")) if part
    )
    key = lambda value: re.sub(r"[.\s]+", "", str(value).upper())
    if not actual or key(actual) != key(expected):
        return {"found": False, "source": result.get("source"),
                "status": "ambiguous_instructor", "instructor_name": requested_name,
                "reason": "The returned professor full name does not match the requested full name."}
    return result


# ══════════════════════════════════════════════════════════
#  Review DB access
# ══════════════════════════════════════════════════════════

_db_conn_local = threading.local()


def _conn() -> Optional[sqlite3.Connection]:
    if not REVIEWS_DB.exists():
        logger.warning("review DB not found at %s — run scripts/import_professor_reviews.py", REVIEWS_DB)
        return None
    c = getattr(_db_conn_local, "conn", None)
    if c is None:
        c = sqlite3.connect(f"file:{REVIEWS_DB}?mode=ro", uri=True, check_same_thread=False)
        c.row_factory = sqlite3.Row
        _db_conn_local.conn = c
    return c


def _norm_class(s: str | None) -> str | None:
    if not s:
        return None
    return "".join(s.split()).lower() or None


_COURSE_NUM_RE = re.compile(r"\d+[a-z]*$")


def _course_token(course_id: str | None) -> str | None:
    """Pull just the course-number tail used in the review DB.

    'COMPSCI 122A' → '122a';  '122A' → '122a';  'I&C SCI 33' → '33';
    'CHEM51B' → '51b';  'chem51b' → '51b'.

    Reviews are stored with the number-only `class` field (never the
    dept prefix), so we strip any leading department letters and keep
    only the trailing course number + optional letter suffix.
    """
    if not course_id:
        return None
    norm = _norm_class(course_id)
    if not norm:
        return None
    m = _COURSE_NUM_RE.search(norm)
    return m.group(0) if m else norm


def get_reviews(
    legacy_id: int,
    course: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Top-N reviews for an instructor (optionally filtered to one course).

    Ordered by thumbs_up DESC, then date DESC — high-signal first.
    """
    c = _conn()
    if c is None or not legacy_id:
        return []

    sql = (
        "SELECT class_raw, comment, clarity_rating, difficulty_rating, "
        "helpful_rating, grade, rating_tags, would_take_again, "
        "thumbs_up, thumbs_down, created_at "
        "FROM reviews WHERE teacher_legacy_id = ?"
    )
    params: list = [legacy_id]
    tok = _course_token(course)
    if tok:
        sql += " AND class_norm = ?"
        params.append(tok)
    sql += " ORDER BY thumbs_up DESC, created_at DESC LIMIT ?"
    params.append(max(1, min(int(limit or 5), 50)))

    rows = c.execute(sql, params).fetchall()
    return [
        {
            "class":            r["class_raw"],
            "comment":          r["comment"],
            "clarity":          r["clarity_rating"],
            "difficulty":       r["difficulty_rating"],
            "helpful":          r["helpful_rating"],
            "grade":            r["grade"],
            "tags":             _split_tags(r["rating_tags"]),
            "would_take_again": bool(r["would_take_again"]) if r["would_take_again"] is not None else None,
            "thumbs_up":        r["thumbs_up"],
            "thumbs_down":      r["thumbs_down"],
            "date":             r["created_at"],
        }
        for r in rows
    ]


def _split_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    # ratingTags uses '--' as separator: "Amazing lectures--Lecture heavy--Test heavy"
    return [t.strip() for t in raw.split("--") if t and t.strip()]


def aggregate_tags(
    legacy_id: int,
    course: str | None = None,
    top_n: int = 8,
) -> list[dict]:
    """Most-cited tags for an instructor (optionally per-course)."""
    c = _conn()
    if c is None or not legacy_id:
        return []

    sql    = "SELECT rating_tags FROM reviews WHERE teacher_legacy_id = ? AND rating_tags IS NOT NULL AND rating_tags != ''"
    params: list = [legacy_id]
    tok = _course_token(course)
    if tok:
        sql += " AND class_norm = ?"
        params.append(tok)

    counts: dict[str, int] = {}
    for (raw,) in c.execute(sql, params):
        for tag in _split_tags(raw):
            counts[tag] = counts.get(tag, 0) + 1

    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"tag": t, "count": n} for t, n in ordered[:top_n]]


def review_stats(legacy_id: int, course: str | None = None) -> dict:
    """Per-course stats: total comments, avg ratings, grade counts."""
    c = _conn()
    if c is None or not legacy_id:
        return {"count": 0}

    sql = (
        "SELECT COUNT(*) as cnt, "
        "AVG(clarity_rating)    as avg_clarity, "
        "AVG(difficulty_rating) as avg_difficulty, "
        "AVG(helpful_rating)    as avg_helpful "
        "FROM reviews WHERE teacher_legacy_id = ?"
    )
    params: list = [legacy_id]
    tok = _course_token(course)
    if tok:
        sql += " AND class_norm = ?"
        params.append(tok)

    row = c.execute(sql, params).fetchone()
    return {
        "count":           row["cnt"] or 0,
        "avg_clarity":     round(row["avg_clarity"], 2)    if row["avg_clarity"]    is not None else None,
        "avg_difficulty":  round(row["avg_difficulty"], 2) if row["avg_difficulty"] is not None else None,
        "avg_helpful":     round(row["avg_helpful"], 2)    if row["avg_helpful"]    is not None else None,
    }
