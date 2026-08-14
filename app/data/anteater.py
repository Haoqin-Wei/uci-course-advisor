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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Generic, Literal, Optional, TypeVar

import requests

from app import observability
from app.terms import TermKey

logger = logging.getLogger(__name__)

ANTEATER_BASE_URL = "https://anteaterapi.com/v2/rest"
REQUEST_TIMEOUT_S = 12
LIVE_WEBSOC_TTL_SECONDS = 5 * 60
CALENDAR_ALL_URL = f"{ANTEATER_BASE_URL}/calendar/all"
WEBSOC_TERMS_URL = f"{ANTEATER_BASE_URL}/websoc/terms"
WEBSOC_URL = f"{ANTEATER_BASE_URL}/websoc"
# A regular UCI term should publish at least one of these high-volume
# departments.  Availability checks stop after the first non-empty response,
# avoiding the multi-megabyte all-department WebSoc payload.
AVAILABILITY_PROBE_DEPARTMENTS = ("COMPSCI", "MATH", "BIO SCI")

T = TypeVar("T")
AnteaterResultStatus = Literal[
    "ok",
    "timeout",
    "network_error",
    "non_200",
    "invalid_json",
    "api_error",
    "schema_error",
]


@dataclass(frozen=True)
class AnteaterResult(Generic[T]):
    """Structured transport/schema result for sync-sensitive endpoints."""

    status: AnteaterResultStatus
    url: str
    checked_at: str
    data: Optional[T] = None
    error: Optional[str] = None
    http_status: Optional[int] = None
    content_length: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class TermAvailabilityResult:
    term: TermKey
    available: bool
    course_count: int
    section_count: int
    status: AnteaterResultStatus
    source_url: str
    checked_at: str
    error: Optional[str] = None
    http_status: Optional[int] = None
    content_length: Optional[int] = None

# ── In-process cache ────────────────────────────────────
# Keyed by request shape; values are the parsed `data` field from the
# Anteater response. Bounded loosely — agent loops typically do
# <20 fetches/turn so we don't bother with LRU eviction yet.
_course_cache:   dict[str, Optional[dict]] = {}
_sections_cache: dict[tuple[str, str, str, str], Optional[list]] = {}
_live_sections_cache: dict[tuple, tuple[float, Optional[dict]]] = {}
_instructor_cache: dict[str, Optional[dict]] = {}


def _headers() -> dict:
    api_key = os.environ.get("ANTEATER_API_KEY", "").strip()
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _get_json(
    url: str,
    params: Optional[dict] = None,
    *,
    audit_tool: Optional[str] = None,
    timeout_s: Optional[float] = None,
) -> Optional[dict]:
    """GET → parsed JSON body. Returns None on any failure path; the
    caller never sees an exception. The Anteater envelope is
    {"ok": bool, "data": ...} — we return the WHOLE body, callers pull
    `data` out themselves since shape varies per endpoint."""
    started = observability.now()
    if audit_tool:
        observability.log_agent_web_fetch_started(
            logger,
            url=url,
            method="GET",
            tool=audit_tool,
            workflow_id="anteater_api",
            request_params=params,
        )
    try:
        r = requests.get(
            url,
            params=params,
            headers=_headers(),
            timeout=timeout_s if timeout_s is not None else REQUEST_TIMEOUT_S,
        )
    except requests.RequestException as e:
        if audit_tool:
            observability.log_agent_web_fetch_failed(
                logger,
                url=url,
                method="GET",
                tool=audit_tool,
                workflow_id="anteater_api",
                duration_ms=observability.elapsed_ms(started),
                error=f"{type(e).__name__}: {e}",
                request_params=params,
            )
        logger.warning("Anteater request failed (%s): %s", url, e)
        return None
    if r.status_code != 200:
        if audit_tool:
            observability.log_agent_web_fetch_failed(
                logger,
                url=url,
                method="GET",
                tool=audit_tool,
                workflow_id="anteater_api",
                duration_ms=observability.elapsed_ms(started),
                error=f"HTTP {r.status_code}",
                status_code=r.status_code,
                request_params=params,
            )
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
        if audit_tool:
            observability.log_agent_web_fetch_failed(
                logger,
                url=url,
                method="GET",
                tool=audit_tool,
                workflow_id="anteater_api",
                duration_ms=observability.elapsed_ms(started),
                error=f"invalid JSON: {e}",
                status_code=r.status_code,
                request_params=params,
            )
        logger.warning("Anteater non-JSON response (%s): %s", url, e)
        return None
    if not body.get("ok"):
        if audit_tool:
            observability.log_agent_web_fetch_failed(
                logger,
                url=url,
                method="GET",
                tool=audit_tool,
                workflow_id="anteater_api",
                duration_ms=observability.elapsed_ms(started),
                error=f"API ok=false: {body.get('message', '')}",
                status_code=r.status_code,
                request_params=params,
            )
        logger.info("Anteater ok=false (%s %s): %s",
                    url, params, body.get("message", "")[:200])
        return None
    if audit_tool:
        observability.log_agent_web_fetch_completed(
            logger,
            url=url,
            final_url=getattr(r, "url", None) or url,
            method="GET",
            tool=audit_tool,
            workflow_id="anteater_api",
            status_code=r.status_code,
            content_length=len(getattr(r, "content", b"") or b""),
            content_type=(getattr(r, "headers", {}) or {}).get("content-type"),
            duration_ms=observability.elapsed_ms(started),
            request_params=params,
        )
    return body


def _get_json_result(url: str, params: Optional[dict] = None) -> AnteaterResult[Any]:
    """GET an Anteater envelope without collapsing distinct failure modes."""
    checked_at = _utc_now()
    try:
        response = requests.get(
            url,
            params=params,
            headers=_headers(),
            timeout=REQUEST_TIMEOUT_S,
        )
    except requests.Timeout as exc:
        observability.increment("anteater.request", endpoint=url, result="timeout")
        logger.warning("Anteater timeout (%s %s): %s", url, params, exc)
        return AnteaterResult("timeout", url, checked_at, error=str(exc))
    except requests.RequestException as exc:
        observability.increment("anteater.request", endpoint=url, result="network_error")
        logger.warning("Anteater request failed (%s %s): %s", url, params, exc)
        return AnteaterResult("network_error", url, checked_at, error=str(exc))

    content_length = len(response.content or b"")
    if response.status_code != 200:
        observability.increment(
            "anteater.request",
            endpoint=url,
            result="non_200",
            status=response.status_code,
        )
        logger.warning(
            "Anteater non-200 endpoint=%s status=%s content_length=%s",
            url,
            response.status_code,
            content_length,
        )
        return AnteaterResult(
            "non_200",
            url,
            checked_at,
            error=f"HTTP {response.status_code}",
            http_status=response.status_code,
            content_length=content_length,
        )
    try:
        body = response.json()
    except ValueError as exc:
        observability.increment("anteater.request", endpoint=url, result="invalid_json")
        logger.warning(
            "Anteater invalid JSON endpoint=%s content_length=%s",
            url,
            content_length,
        )
        return AnteaterResult(
            "invalid_json",
            url,
            checked_at,
            error=str(exc),
            http_status=response.status_code,
            content_length=content_length,
        )
    if not isinstance(body, dict):
        return AnteaterResult(
            "schema_error",
            url,
            checked_at,
            error="response envelope must be an object",
            http_status=response.status_code,
            content_length=content_length,
        )
    if body.get("ok") is not True:
        message = str(body.get("message") or "Anteater returned ok=false")
        observability.increment("anteater.request", endpoint=url, result="api_error")
        logger.warning("Anteater ok=false endpoint=%s message=%s", url, message[:200])
        return AnteaterResult(
            "api_error",
            url,
            checked_at,
            error=message,
            http_status=response.status_code,
            content_length=content_length,
        )
    if "data" not in body:
        return AnteaterResult(
            "schema_error",
            url,
            checked_at,
            error="response envelope is missing data",
            http_status=response.status_code,
            content_length=content_length,
        )
    observability.increment("anteater.request", endpoint=url, result="ok")
    return AnteaterResult(
        "ok",
        url,
        checked_at,
        data=body["data"],
        http_status=response.status_code,
        content_length=content_length,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _flatten_websoc_sections(data: dict) -> list[dict]:
    flat: list[dict] = []
    for school in data.get("schools", []):
        for dept in school.get("departments", []):
            for course in dept.get("courses", []):
                for sec in course.get("sections", []):
                    flat.append(sec)
    return flat


def _websoc_counts(data: dict) -> tuple[int, int]:
    course_count = 0
    section_count = 0
    for school in data.get("schools", []):
        if not isinstance(school, dict):
            continue
        for department in school.get("departments", []):
            if not isinstance(department, dict):
                continue
            for course in department.get("courses", []):
                if not isinstance(course, dict):
                    continue
                course_count += 1
                sections = course.get("sections")
                if isinstance(sections, list):
                    section_count += sum(isinstance(section, dict) for section in sections)
    return course_count, section_count


# ── Automatic-term sync endpoints ──────────────────────

def fetch_calendar_all() -> AnteaterResult[list[dict]]:
    result = _get_json_result(CALENDAR_ALL_URL)
    if not result.ok:
        return result
    if not isinstance(result.data, list) or not all(
        isinstance(record, dict) for record in result.data
    ):
        return AnteaterResult(
            "schema_error",
            result.url,
            result.checked_at,
            error="calendar data must be a list of objects",
            http_status=result.http_status,
            content_length=result.content_length,
        )
    return result


def fetch_websoc_terms() -> AnteaterResult[list[dict]]:
    result = _get_json_result(WEBSOC_TERMS_URL)
    if not result.ok:
        return result
    if not isinstance(result.data, list) or not all(
        isinstance(record, dict)
        and isinstance(record.get("shortName"), str)
        and bool(record["shortName"].strip())
        for record in result.data
    ):
        return AnteaterResult(
            "schema_error",
            result.url,
            result.checked_at,
            error="WebSoc terms must contain non-empty shortName values",
            http_status=result.http_status,
            content_length=result.content_length,
        )
    return result


def fetch_full_websoc(term: TermKey) -> AnteaterResult[dict]:
    result = _get_json_result(
        WEBSOC_URL,
        params={"year": str(term.year), "quarter": term.quarter},
    )
    if not result.ok:
        return result
    if not isinstance(result.data, dict) or not isinstance(result.data.get("schools"), list):
        return AnteaterResult(
            "schema_error",
            result.url,
            result.checked_at,
            error="full WebSoc data must contain a schools list",
            http_status=result.http_status,
            content_length=result.content_length,
        )
    return result


def check_term_data_availability(term: TermKey) -> TermAvailabilityResult:
    """Probe small department slices; a term shell is not considered available."""
    total_content_length = 0
    last_result: Optional[AnteaterResult[dict]] = None
    for department in AVAILABILITY_PROBE_DEPARTMENTS:
        result = _get_json_result(
            WEBSOC_URL,
            params={
                "year": str(term.year),
                "quarter": term.quarter,
                "department": department,
            },
        )
        last_result = result
        total_content_length += int(result.content_length or 0)
        if not result.ok or not isinstance(result.data, dict):
            return TermAvailabilityResult(
                term=term,
                available=False,
                course_count=0,
                section_count=0,
                status=result.status,
                source_url=result.url,
                checked_at=result.checked_at,
                error=result.error,
                http_status=result.http_status,
                content_length=total_content_length or result.content_length,
            )
        course_count, section_count = _websoc_counts(result.data)
        if course_count > 0 and section_count > 0:
            return TermAvailabilityResult(
                term=term,
                available=True,
                course_count=course_count,
                section_count=section_count,
                status=result.status,
                source_url=result.url,
                checked_at=result.checked_at,
                http_status=result.http_status,
                content_length=total_content_length,
            )

    assert last_result is not None
    return TermAvailabilityResult(
        term=term,
        available=False,
        course_count=0,
        section_count=0,
        status=last_result.status,
        source_url=last_result.url,
        checked_at=last_result.checked_at,
        http_status=last_result.http_status,
        content_length=total_content_length,
    )


# ── Courses ──────────────────────────────────────────────

def fetch_course(
    course_id: str,
    *,
    request_timeout_s: Optional[float] = None,
) -> Optional[dict]:
    """Single course metadata from /v2/rest/courses/{id}. The id form
    is the Anteater concatenation (`COMPSCI122A`), no underscore."""
    key = course_id.upper().replace(" ", "").replace("_", "")
    if key in _course_cache:
        return _course_cache[key]
    body = _get_json(
        f"{ANTEATER_BASE_URL}/courses/{key}",
        audit_tool="get_course",
        timeout_s=request_timeout_s,
    )
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
        audit_tool="get_sections",
    )
    if not body:
        _sections_cache[key] = None
        return None

    data = body.get("data") or {}
    flat = _flatten_websoc_sections(data)
    _sections_cache[key] = flat
    return flat


def fetch_live_sections(
    *,
    year: str,
    quarter: str,
    department: Optional[str] = None,
    course_number: Optional[str] = None,
    section_codes: Optional[list[str]] = None,
    force_refresh: bool = False,
    request_timeout_s: Optional[float] = None,
) -> Optional[dict]:
    """
    Live WebSoc data via Anteater, with AntAlmanac-style 5 minute
    freshness semantics.

    Use this for current availability/status questions. Unlike
    `fetch_sections`, this returns an envelope with `retrieved_at` and
    cache metadata so callers can tell users exactly how fresh the seat
    count is.
    """

    normalized_codes = tuple(
        sorted(str(code).strip() for code in (section_codes or []) if str(code).strip())
    )
    dept = (department or "").upper().strip()
    num = (course_number or "").upper().strip()
    key = ("live_websoc", str(year), quarter, dept, num, normalized_codes)

    now = time.time()
    if not force_refresh and key in _live_sections_cache:
        cached_at, cached = _live_sections_cache[key]
        if now - cached_at <= LIVE_WEBSOC_TTL_SECONDS:
            observability.increment("live_websoc.cache", result="hit")
            if cached is None:
                return None
            return {**cached, "cache_hit": True}
    observability.increment("live_websoc.cache", result="miss")

    params: dict[str, str] = {
        "year": str(year),
        "quarter": quarter,
    }
    if normalized_codes:
        params["sectionCodes"] = ",".join(normalized_codes)
    else:
        if not dept or not num:
            logger.warning(
                "live WebSoc request missing department/courseNumber and sectionCodes"
            )
            return None
        params["department"] = dept
        params["courseNumber"] = num

    retrieved_at = _utc_now()
    started_at = observability.now()
    body = _get_json(
        f"{ANTEATER_BASE_URL}/websoc",
        params=params,
        audit_tool="get_live_sections",
        timeout_s=request_timeout_s,
    )
    observability.observe_ms(
        "live_websoc.api_latency",
        observability.elapsed_ms(started_at),
        service="anteater",
        endpoint="/websoc",
    )
    if not body:
        observability.increment("live_websoc.api_result", result="unavailable")
        _live_sections_cache[key] = (now, None)
        last_success = _live_sections_cache.get(("last_success", *key))
        if last_success:
            succeeded_at, succeeded = last_success
            age_seconds = max(0.0, now - succeeded_at)
            if age_seconds <= LIVE_WEBSOC_TTL_SECONDS:
                observability.increment("live_websoc.fallback", result="last_known")
                return {
                    **succeeded,
                    "cache_hit": True,
                    "stale": True,
                    "stale_age_seconds": round(age_seconds, 2),
                }
        return None

    data = body.get("data") or {}
    observability.increment("live_websoc.api_result", result="ok")
    result = {
        "source": "live_anteater_websoc",
        "retrieved_at": retrieved_at,
        "cache_hit": False,
        "stale": False,
        "sections": _flatten_websoc_sections(data),
    }
    _live_sections_cache[key] = (now, result)
    _live_sections_cache[("last_success", *key)] = (now, result)
    return result


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
    body = _get_json(
        f"{ANTEATER_BASE_URL}/instructors/{key}",
        audit_tool="get_instructor",
    )
    data = body.get("data") if body else None
    _instructor_cache[key] = data
    return data
