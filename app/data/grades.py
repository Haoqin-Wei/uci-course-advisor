"""
Grade-distribution lookup via Anteater API.

Calls:    GET https://anteaterapi.com/v2/rest/grades/aggregateByCourse
Filters:  ?department=<dept>&courseNumber=<num>

Response shape (single inner record):

    {
      "ok": true,
      "data": [
        {
          "gradeACount":  4853,
          "gradeBCount":  2007,
          "gradeCCount":  751,
          "gradeDCount":  188,
          "gradeFCount":  406,
          "gradePCount":  239,
          "gradeNPCount": 103,
          "gradeWCount":  92,
          "averageGPA":   3.259,
          "department":   "COMPSCI",
          "courseNumber": "122B"
        }
      ]
    }

Counts aggregate ALL historical sections (Anteater holds data since
Summer 2014). We normalize this to a stable internal shape and cache
it on disk under data/grades_cache/<dept>_<courseNumber>.json so the
API isn't hammered on every chat turn. Cache is permanent for the
MVP — delete files to force refresh.

Note: there's a separate endpoint /grades/aggregate (without the
"ByCourse" suffix) that returns ONLY section metadata, no grade
counts — confusingly named, do NOT use it. /grades/aggregateByOffering
gives per-instructor breakdowns.

Public API:
    get_grade_distribution(course_id) -> Optional[dict]
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import requests

from app import observability


logger = logging.getLogger(__name__)

ANTEATER_BASE_URL = "https://anteaterapi.com/v2/rest"
CACHE_DIR         = Path("data/grades_cache")
REQUEST_TIMEOUT_S = 10


# ── Department-prefix mapping (colloquial → Anteater) ────
#
# UCI department codes used by Anteater follow the catalogue's full
# names (with spaces and ampersands). Our codebase uses compact forms.
# Mirror of the forward map in
#   app/data/uci_general/major_requirements/__init__.py
# kept here intentionally to avoid an import cycle.

_COLLOQUIAL_TO_ANTEATER_DEPT: dict[str, str] = {
    # ICS
    "CS":       "COMPSCI",
    "ICS":      "I&C SCI",
    "EECS":     "EECS",
    "CSE":      "CSE",
    "IN4MATX":  "IN4MATX",
    "STATS":    "STATS",
    "SWE":      "SWE",

    # Math / Physical Sciences
    "MATH":     "MATH",
    "PHYSICS":  "PHYSICS",
    "CHEM":     "CHEM",
    "EARTHSS":  "EARTHSS",

    # Bio Sciences
    "BIOSCI":   "BIO SCI",
    "BIO SCI":  "BIO SCI",
    "BIOCHEM":  "BIOCHEM",
    "ECOEVO":   "ECO EVO",
    "ECO EVO":  "ECO EVO",
    "MOLBIO":   "MOL BIO",
    "DEVBIO":   "DEV BIO",
    "NEURBIO":  "NEURBIO",
    "MMG":      "M&MG",
    "PSYBEH":   "PSY BEH",

    # Engineering
    "BME":      "BME",
    "CEE":      "CEE",
    "CBEMS":    "CBEMS",
    "ENGR":     "ENGR",
    "ENGRCEE":  "ENGRCEE",
    "ENGRMAE":  "ENGRMAE",
    "ENGRMSE":  "ENGRMSE",
    "MAE":      "MAE",
    "MSE":      "MSE",

    # Social Sciences
    "ECON":     "ECON",
    "PSYCH":    "PSYCH",
    "POLSCI":   "POL SCI",
    "POL SCI":  "POL SCI",
    "SOCSCI":   "SOC SCI",
    "SOC SCI":  "SOC SCI",
    "SOCIOL":   "SOCIOL",
    "ANTHRO":   "ANTHRO",
    "COGSCI":   "COG SCI",
    "COGS":     "COGS",          # not all data sources use COG SCI
    "INTLST":   "INTL ST",
    "INTL ST":  "INTL ST",
    "LSCI":     "LSCI",
    "LANGSCI":  "LANG SCI",
    "LANG SCI": "LANG SCI",
    "PHILOS":   "PHILOS",

    # Humanities & Arts
    "WRITING":  "WRITING",
    "ENGLISH":  "ENGLISH",
    "HISTORY":  "HISTORY",
    "ART":      "ART",
    "ARTHIS":   "ART HIS",
    "ART HIS":  "ART HIS",
    "MUSIC":    "MUSIC",
    "DRAMA":    "DRAMA",
    "DANCE":    "DANCE",
    "CHINESE":  "CHINESE",
    "JAPANSE":  "JAPANSE",
    "KOREAN":   "KOREAN",
    "SPANISH":  "SPANISH",
    "FRENCH":   "FRENCH",
    "GERMAN":   "GERMAN",
    "CLASSIC":  "CLASSIC",
    "REL STD":  "REL STD",
    "RELSTD":   "REL STD",

    # Business / Management
    "MGMT":     "MGMT",
    "MGMTMBA":  "MGMTMBA",
    "MGMTEP":   "MGMT EP",
    "MGMT EP":  "MGMT EP",
    "MGMTFE":   "MGMT FE",
    "MGMT FE":  "MGMT FE",
    "MGMTHC":   "MGMT HC",
    "MGMT HC":  "MGMT HC",
    "ACCT":     "ACCT",
    "BANA":     "BANA",
    "FIN":      "FIN",

    # Health / Pharm
    "PUBHLTH":  "PUBHLTH",
    "NURSCI":   "NUR SCI",
    "NUR SCI":  "NUR SCI",
    "PHRMSCI":  "PHRMSCI",
    "PHARM":    "PHARM",
    "EPIDEM":   "EPIDEM",

    # Social Ecology
    "CRIM":     "CRIM",
    "SOCECOL":  "SOCECOL",
    "PSYSCI":   "PSY SCI",
    "PP&D":     "PP&D",
    "UPPP":     "UPPP",

    # Education
    "EDUC":     "EDUC",
}


def _parse_course_id(course_id: str) -> tuple[Optional[str], Optional[str]]:
    """
    Split a colloquial course ID into (Anteater department, course number).

    Examples:
        'CS122A'   → ('COMPSCI',   '122A')
        'ICS33'    → ('I&C SCI',   '33')
        'MATH2A'   → ('MATH',      '2A')
        'WRITING39B' → ('WRITING', '39B')
        'STATS67'  → ('STATS',     '67')

    Returns (None, None) if the prefix isn't known or the format isn't
    recognized.
    """
    if not course_id or not isinstance(course_id, str):
        return None, None
    cid = course_id.upper().strip()

    # Try longest prefix match first to disambiguate e.g. "CS" vs "CSE"
    for colloquial in sorted(_COLLOQUIAL_TO_ANTEATER_DEPT.keys(),
                             key=len, reverse=True):
        if cid.startswith(colloquial):
            rest = cid[len(colloquial):]
            # Course number must start with a digit (possibly preceded
            # by H for honors variants like ICSH32 → 'H32').
            if rest and (rest[0].isdigit()
                         or (rest[0] == 'H' and len(rest) > 1 and rest[1].isdigit())):
                return _COLLOQUIAL_TO_ANTEATER_DEPT[colloquial], rest

    return None, None


# ── Cache I/O ────────────────────────────────────────────

def _cache_path(dept: str, course_number: str) -> Path:
    safe_dept = re.sub(r"[^A-Z0-9]+", "_", dept.upper()).strip("_")
    safe_num  = re.sub(r"[^A-Z0-9]+", "_", course_number.upper()).strip("_")
    return CACHE_DIR / f"{safe_dept}_{safe_num}.json"


def _read_cache(dept: str, course_number: str) -> Optional[dict]:
    path = _cache_path(dept, course_number)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("grades cache read failed for %s/%s: %s", dept, course_number, e)
        return None


def _write_cache(dept: str, course_number: str, data: dict) -> None:
    path = _cache_path(dept, course_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("grades cache write failed for %s/%s: %s", dept, course_number, e)


# ── Anteater API call ────────────────────────────────────

def _fetch_aggregate(dept: str, course_number: str) -> Optional[dict]:
    """
    Fetch course-level grade aggregate from Anteater.

    Uses /grades/aggregateByCourse which returns a single record summing
    grade counts across all historical sections of the course. Response
    shape:

        {
          "ok": true,
          "data": [
            {
              "gradeACount": 4853, "gradeBCount": 2007, ...,
              "averageGPA": 3.26,
              "department": "COMPSCI", "courseNumber": "122B"
            }
          ]
        }

    Returns the single inner record, or None on miss/error.

    (We deliberately do NOT use /grades/aggregate — despite the name,
    it returns just section metadata without grade counts. The naming
    is confusing but verified empirically. Use /grades/aggregateByOffering
    if you need per-instructor breakdowns instead.)
    """
    api_key = os.environ.get("ANTEATER_API_KEY", "").strip()
    url     = f"{ANTEATER_BASE_URL}/grades/aggregateByCourse"
    params  = {"department": dept, "courseNumber": course_number}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    try:
        r = requests.get(url, params=params, headers=headers,
                         timeout=REQUEST_TIMEOUT_S)
    except requests.RequestException as e:
        logger.warning("grades API request failed for %s/%s: %s",
                       dept, course_number, e)
        return None

    if r.status_code != 200:
        if r.status_code == 429:
            observability.increment("external_api.rate_limited", service="grades")
            observability.log_event(
                logger,
                logging.WARNING,
                "external_api_rate_limited",
                service="grades",
                course=f"{dept}/{course_number}",
            )
        else:
            observability.increment(
                "external_api.non_200",
                service="grades",
                status=r.status_code,
            )
        logger.info("grades API %d for %s/%s: %s",
                    r.status_code, dept, course_number, r.text[:200])
        return None

    try:
        body = r.json()
    except ValueError:
        logger.warning("grades API returned non-JSON for %s/%s", dept, course_number)
        return None

    if not body.get("ok"):
        logger.info("grades API ok=false for %s/%s: %s",
                    dept, course_number, str(body)[:200])
        return None

    records = body.get("data") or []
    if not records:
        logger.info("grades API returned empty data for %s/%s", dept, course_number)
        return None

    # Single record per course; grade counts are inline at the top level.
    return records[0]


# ── Normalization ────────────────────────────────────────

_LETTER_FIELDS = [
    ("A",  "gradeACount"),
    ("B",  "gradeBCount"),
    ("C",  "gradeCCount"),
    ("D",  "gradeDCount"),
    ("F",  "gradeFCount"),
    ("P",  "gradePCount"),
    ("NP", "gradeNPCount"),
    ("W",  "gradeWCount"),
]


def _normalize(raw: dict, course_id: str) -> Optional[dict]:
    """Convert Anteater's response shape to our internal schema."""
    if not raw:
        return None
    counts: dict[str, int] = {
        letter: int(raw.get(field, 0) or 0)
        for letter, field in _LETTER_FIELDS
    }
    # GPA is computed across A-F only (P/NP/W don't have GPA values).
    graded_total = sum(counts[l] for l in ("A", "B", "C", "D", "F"))
    if graded_total == 0:
        return None
    pct = {
        letter: round(counts[letter] / graded_total * 100, 1)
        for letter in ("A", "B", "C", "D", "F")
    }
    return {
        "course_id":     course_id.upper(),
        "avg_gpa":       round(float(raw.get("averageGPA") or 0), 2) or None,
        "samples":       graded_total + counts["P"] + counts["NP"] + counts["W"],
        "graded":        graded_total,
        "letter_counts": counts,
        "letter_pct":    pct,
        # ── Flat aliases (back-compat with answer.py / mock_data schema)
        # answer.py and any other legacy callers expect pct_A..pct_F
        # directly on the dict, not nested under "letter_pct". We keep
        # both shapes so neither old nor new call sites break.
        "pct_A":         pct["A"],
        "pct_B":         pct["B"],
        "pct_C":         pct["C"],
        "pct_D":         pct["D"],
        "pct_F":         pct["F"],
        "source":        "Anteater /grades/aggregate (all historical)",
    }


# ── Public lookup ────────────────────────────────────────

def get_grade_distribution(course_id: str) -> Optional[dict]:
    """
    Return aggregate historical grade distribution for a course, or
    None if unavailable.

    First checks the on-disk cache; falls back to Anteater API. Results
    are cached permanently (clear data/grades_cache/ to force refresh).
    """
    dept, course_number = _parse_course_id(course_id)
    if not dept or not course_number:
        return None

    cached = _read_cache(dept, course_number)
    if cached is not None:
        # Treat empty dict as "we tried, no data". Don't refetch.
        return cached if cached else None

    raw  = _fetch_aggregate(dept, course_number)
    norm = _normalize(raw, course_id) if raw else None

    # Cache the result (or empty dict for "no data") so we don't refetch.
    _write_cache(dept, course_number, norm or {})
    return norm
