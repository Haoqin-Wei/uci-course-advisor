"""
Anteater API client — courses, sections (websoc), instructors.

This module is the live-API arm of the data layer. The local CSV /
CatalogView is queried first by app.data.db; we get called only when
the DB has nothing for the requested key (e.g. a Spring 2026 course
that hasn't been crawled into the local feed yet).

Endpoints used:
    GET /v2/rest/courses/{id}                         single course metadata
    GET /v2/rest/websoc?year&quarter&dept&num         live section schedule
    GET /v2/rest/instructors/{ucinetid_or_name}       instructor profile + RMP

All functions:
  - return None on miss / network error / non-200 (caller decides what
    "no data" means for its tool)
  - never raise; log warnings instead
  - use an in-memory cache to avoid hammering the API across tool
    calls in the same agent loop (different from grades.py's on-disk
    cache because section/instructor data is small and process-local
    is sufficient for v1)

Why shape-mirror grades.py rather than fold into it: grades.py has
~370 lines of cache + normalization logic that's specific to grade
distributions (year-range filtering, A/B/C percent computation).
Mashing course/section/instructor fetches in there would obscure
both. They can be unified later if patterns converge.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

from app import observability

logger = logging.getLogger(__name__)

ANTEATER_BASE_URL = "https://anteaterapi.com/v2/rest"
REQUEST_TIMEOUT_S = 12

# ── In-process cache ────────────────────────────────────
# Keyed by request shape; values are the parsed `data` field from the
# Anteater response. Bounded loosely — agent loops typically do
# <20 fetches/turn so we don't bother with LRU eviction yet.
_course_cache:   dict[str, Optional[dict]] = {}
_sections_cache: dict[tuple[str, str, str, str], Optional[list]] = {}
_instructor_cache: dict[str, Optional[dict]] = {}


def _headers() -> dict:
    api_key = os.environ.get("ANTEATER_API_KEY", "").strip()
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _get_json(url: str, params: Optional[dict] = None) -> Optional[dict]:
    """GET → parsed JSON body. Returns None on any failure path; the
    caller never sees an exception. The Anteater envelope is
    {"ok": bool, "data": ...} — we return the WHOLE body, callers pull
    `data` out themselves since shape varies per endpoint."""
    try:
        r = requests.get(url, params=params, headers=_headers(),
                         timeout=REQUEST_TIMEOUT_S)
    except requests.RequestException as e:
        logger.warning("Anteater request failed (%s): %s", url, e)
        return None
    if r.status_code != 200:
        if r.status_code == 429:
            observability.increment("external_api.rate_limited", service="anteater")
            observability.log_event(
                logger,
                logging.WARNING,
                "external_api_rate_limited",
                service="anteater",
                url=url,
                params=params,
            )
        else:
            observability.increment(
                "external_api.non_200",
                service="anteater",
                status=r.status_code,
            )
        logger.info("Anteater %d (%s %s): %s",
                    r.status_code, url, params, r.text[:200])
        return None
    try:
        body = r.json()
    except ValueError as e:
        logger.warning("Anteater non-JSON response (%s): %s", url, e)
        return None
    if not body.get("ok"):
        logger.info("Anteater ok=false (%s %s): %s",
                    url, params, body.get("message", "")[:200])
        return None
    return body


# ── Courses ──────────────────────────────────────────────

def fetch_course(course_id: str) -> Optional[dict]:
    """Single course metadata from /v2/rest/courses/{id}. The id form
    is the Anteater concatenation (`COMPSCI122A`), no underscore."""
    key = course_id.upper().replace(" ", "").replace("_", "")
    if key in _course_cache:
        return _course_cache[key]
    body = _get_json(f"{ANTEATER_BASE_URL}/courses/{key}")
    data = body.get("data") if body else None
    _course_cache[key] = data
    return data


# ── Sections (live websoc) ──────────────────────────────

def fetch_sections(
    department: str,
    course_number: str,
    year: str,
    quarter: str,
) -> Optional[list[dict]]:
    """
    Live registrar data via /v2/rest/websoc.

    Anteater returns a nested structure (schools → departments →
    courses → sections). We flatten to a list of section dicts so the
    caller doesn't have to walk the tree.
    """
    key = (department.upper(), course_number.upper(), str(year), quarter)
    if key in _sections_cache:
        return _sections_cache[key]

    body = _get_json(
        f"{ANTEATER_BASE_URL}/websoc",
        params={
            "year":         str(year),
            "quarter":      quarter,
            "department":   department,
            "courseNumber": course_number,
        },
    )
    if not body:
        _sections_cache[key] = None
        return None

    data = body.get("data") or {}
    flat: list[dict] = []
    for school in data.get("schools", []):
        for dept in school.get("departments", []):
            for course in dept.get("courses", []):
                for sec in course.get("sections", []):
                    flat.append(sec)
    _sections_cache[key] = flat
    return flat


# ── Instructors (RMP-style profile) ─────────────────────

def fetch_instructor(name_or_ucinetid: str) -> Optional[dict]:
    """
    Instructor profile from /v2/rest/instructors/{key}.

    Accepts either a ucinetid ('thornton') or — handled by the route
    itself — a short name. The endpoint returns shortenedNames (the
    form sections.csv uses: 'THORNTON, A.') so callers can cross-ref.
    """
    key = name_or_ucinetid.strip().lower().split(",")[0].strip()
    # Try the verbatim key first, then a last-name fallback if the
    # caller passed a "LASTNAME, F." style string.
    if key in _instructor_cache:
        return _instructor_cache[key]
    body = _get_json(f"{ANTEATER_BASE_URL}/instructors/{key}")
    data = body.get("data") if body else None
    _instructor_cache[key] = data
    return data
