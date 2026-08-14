"""
Anteater API wrapper — programs (departments / majors / requirements / GE).

Companion to app/data/anteater.py (which covers courses / sections /
instructors). This module is used by the onboarding wizard and the
profile editor: it answers "what colleges exist", "what majors are
in this college", "what courses count toward this major / GE bucket".

Endpoints exposed here, with the Anteater path they wrap:

    departments()                       /websoc/departments
    majors(division=None, type=None)    /programs/majors
    major(program_id)                   /programs/major?programId=...
    ge_requirements()                   /programs/ugradRequirements?id=GE
    extra_requirements(kind)            /programs/ugradRequirements?id=UC|CHC4|CHC2

Remote calls:
  - return None on miss / network error / non-200 (caller decides)
  - never raise; log warnings instead
  - use a per-process in-memory cache because this data is stable on
    the order of quarters.

The whole-course picker is different: it first uses the versioned local UCI
CSV, then a restart-safe runtime snapshot, so process restarts do not trigger
the old 90-page API crawl.

The User-Agent header matters — Anteater's edge returns 403 to
clients sending the default "Python-urllib/x.y" UA.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from app import observability
from app.catalog.departments import colloquial_course_id

logger = logging.getLogger(__name__)

ANTEATER_BASE_URL = "https://anteaterapi.com/v2/rest"
REQUEST_TIMEOUT_S = 12
_UA = "uci-course-advisor/1.0"

# ── Process-local caches (keyed by request shape) ─────────
_departments_cache: Optional[list[dict]] = None
_majors_cache:      Optional[list[dict]] = None
_major_detail_cache: dict[str, Optional[dict]] = {}
_ext_req_cache:     dict[str, Optional[dict]] = {}
_courses_cache:     dict[str, list[dict]] = {}   # dept code → list of slim courses
_all_courses_cache: Optional[list[dict]] = None  # whole UCI catalog (slim rows)
_all_courses_source: Optional[str] = None

LOCAL_COURSES_PATH = Path("data/uci/courses.csv")
ALL_COURSES_DISK_CACHE_PATH = Path("data/runtime/onboarding_courses.json")
ALL_COURSES_CACHE_SCHEMA_VERSION = 1


def _get(
    path: str,
    params: Optional[dict] = None,
    *,
    timeout_s: Optional[float] = None,
) -> Optional[dict]:
    """Thin GET. Returns parsed JSON `data` field or None."""
    url = f"{ANTEATER_BASE_URL}{path}"
    try:
        r = requests.get(
            url,
            params=params,
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=timeout_s if timeout_s is not None else REQUEST_TIMEOUT_S,
        )
        if r.status_code != 200:
            if r.status_code == 429:
                observability.increment(
                    "external_api.rate_limited",
                    service="anteater_programs",
                )
                observability.log_event(
                    logger,
                    logging.WARNING,
                    "external_api_rate_limited",
                    service="anteater_programs",
                    url=url,
                    params=params,
                )
            else:
                observability.increment(
                    "external_api.non_200",
                    service="anteater_programs",
                    status=r.status_code,
                )
            logger.warning("anteater %s → %d (%s)", url, r.status_code, r.text[:120])
            return None
        body = r.json()
        if not body.get("ok"):
            logger.warning("anteater %s returned ok=false: %s", url, body.get("message"))
            return None
        return body.get("data")
    except (requests.RequestException, ValueError) as e:
        logger.warning("anteater %s failed: %s", url, e)
        return None


# ── Departments ──────────────────────────────────────────

def departments() -> list[dict]:
    """
    All UCI departments registered with WebSoc, sorted alphabetically by
    name. Each entry: {deptCode, deptName}.

    Cached per process — refreshes on app restart.
    """
    global _departments_cache
    if _departments_cache is None:
        data = _get("/websoc/departments") or []
        _departments_cache = sorted(
            (d for d in data if d.get("deptName")),
            key=lambda d: d["deptName"].lower(),
        )
    return list(_departments_cache)  # defensive copy — caller may mutate


# ── Majors ───────────────────────────────────────────────

def majors(
    division: Optional[str] = None,
    type_filter: Optional[str] = None,
) -> list[dict]:
    """
    All programs, optionally filtered by division ("Undergraduate" /
    "Graduate") and/or by `type` ("B.S.", "B.A.", "M.S.", ...). Sorted
    alphabetically by display name.

    Each entry: {id, name, type, division, specializationRequired,
                 specializations: [...]}.
    """
    global _majors_cache
    if _majors_cache is None:
        data = _get("/programs/majors") or []
        _majors_cache = sorted(
            (m for m in data if m.get("name") and m.get("id")),
            key=lambda m: m["name"].lower(),
        )

    out = _majors_cache
    if division:
        out = [m for m in out if m.get("division") == division]
    if type_filter:
        out = [m for m in out if m.get("type") == type_filter]
    return list(out)


def major(
    program_id: str,
    *,
    request_timeout_s: Optional[float] = None,
) -> Optional[dict]:
    """
    Full requirement tree for one major. Returns dict with
    `requirements: [...]` where each leaf has `courses: [...]`.
    None on miss.
    """
    if not program_id:
        return None
    if program_id in _major_detail_cache:
        return _major_detail_cache[program_id]
    data = _get(
        "/programs/major",
        params={"programId": program_id},
        timeout_s=request_timeout_s,
    )
    _major_detail_cache[program_id] = data
    return data


# ── GE / external undergrad requirements ─────────────────

# The GE label prefix is a Roman numeral ("I. ", "II. ", ...). We sort
# requirements blocks by that prefix so they render in the canonical
# Catalogue order (I → VIII), regardless of API insertion order.
_ROMAN = {
    "I":   1, "II":  2, "III": 3, "IV":  4,
    "V":   5, "VI":  6, "VII": 7, "VIII":8,
    "IX":  9, "X":  10,
}
_LABEL_ROMAN_RE = re.compile(r"^(I{1,3}|IV|V|VI{0,3}|IX|X)\.\s")


def _ge_sort_key(req: dict) -> tuple[int, str]:
    label = req.get("label", "") or ""
    m = _LABEL_ROMAN_RE.match(label.strip())
    if m:
        return (_ROMAN.get(m.group(1), 99), label.lower())
    return (99, label.lower())


def ge_requirements() -> Optional[dict]:
    """
    Full GE tree, with the top-level requirements sorted by Roman
    numeral prefix (I. through VIII.). Returns the full dict, not
    just the requirements list, so callers can keep the `id` field.
    """
    data = _get_external("GE")
    if not data:
        return None
    reqs = data.get("requirements") or []
    sorted_reqs = sorted(reqs, key=_ge_sort_key)
    return {**data, "requirements": sorted_reqs}


def extra_requirements(kind: str) -> Optional[dict]:
    """
    UC / CHC4 / CHC2 (honors / colloquia) requirements. Same shape as
    GE but smaller. Not sorted — these don't use Roman-numeral labels.
    """
    if kind not in ("UC", "CHC4", "CHC2"):
        return None
    return _get_external(kind)


def _get_external(kind: str) -> Optional[dict]:
    if kind in _ext_req_cache:
        return _ext_req_cache[kind]
    data = _get("/programs/ugradRequirements", params={"id": kind})
    _ext_req_cache[kind] = data
    return data


# ── Courses by department ────────────────────────────────

# Anteater caps `take` at 100; paginate through `skip` until a short
# page comes back. Per-process cache because depts are stable per
# quarter and the wizard will revisit dept clicks often.
_COURSES_PAGE_SIZE = 100


def list_courses_by_department(dept_code: str) -> list[dict]:
    """
    Fetch every course in `dept_code` (e.g. 'COMPSCI', 'I&C SCI',
    'AC ENG'). Returns a slim, JSON-friendly list:
        [{id, courseNumber, title, courseLevel, minUnits, maxUnits}, ...]

    Courses are sorted by numeric course number (ICS 6B before ICS 31
    before ICS 122A). Empty list on miss or network error.
    """
    if not dept_code:
        return []
    key = dept_code.strip()
    if key in _courses_cache:
        return list(_courses_cache[key])

    all_rows: list[dict] = []
    skip = 0
    while True:
        page = _get("/courses", params={
            "department": key,
            "take": _COURSES_PAGE_SIZE,
            "skip": skip,
        })
        if not page:                          # network error or non-list
            break
        rows = page if isinstance(page, list) else []
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < _COURSES_PAGE_SIZE:
            break
        skip += _COURSES_PAGE_SIZE
        if skip >= 2000:                      # safety cap; no real dept is this big
            logger.warning("list_courses_by_department(%s) hit safety cap", key)
            break

    slim = [{
        "id":          row.get("id"),
        "department":  row.get("department"),
        "courseNumber": row.get("courseNumber"),
        "courseNumeric": row.get("courseNumeric"),
        "title":       row.get("title"),
        "courseLevel": row.get("courseLevel"),
        "minUnits":    row.get("minUnits"),
        "maxUnits":    row.get("maxUnits"),
    } for row in all_rows if row.get("id")]

    # Sort by numeric course number (Anteater's `courseNumeric`), so
    # ICS 6B comes before ICS 31; ties fall back to lexical.
    slim.sort(key=lambda c: (c.get("courseNumeric") or 0, c.get("courseNumber") or ""))

    _courses_cache[key] = slim
    return list(slim)


def list_all_courses() -> list[dict]:
    """
    Return the whole slim course catalogue without a cold-start API crawl.

    The versioned local ``data/uci/courses.csv`` is the primary source.  If a
    deployment omits that file, a runtime JSON snapshot survives process
    restarts.  Only when neither local source exists do we paginate Anteater's
    ``coursesCursor`` endpoint, then persist the successful result.
    """
    global _all_courses_cache, _all_courses_source
    if _all_courses_cache is not None:
        return list(_all_courses_cache)

    local = _load_local_courses()
    if local:
        _all_courses_cache = local
        _all_courses_source = "local_csv"
        return list(_all_courses_cache)

    persisted = _load_all_courses_disk_cache()
    if persisted:
        _all_courses_cache = persisted
        _all_courses_source = "runtime_cache"
        return list(_all_courses_cache)

    all_rows: list[dict] = []
    cursor: Optional[str] = None
    pages = 0
    while True:
        params: dict[str, object] = {"take": _COURSES_PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        page = _get("/coursesCursor", params=params)
        if not page or not isinstance(page, dict):
            break
        items = page.get("items") or []
        all_rows.extend(items)
        cursor = page.get("nextCursor")
        pages += 1
        if not cursor or not items:
            break
        if pages > 200:                       # ~20k courses; safety stop
            logger.warning("list_all_courses() hit page-safety cap")
            break

    slim = [_slim_network_course(row) for row in all_rows if row.get("id")]

    slim.sort(key=lambda c: c.get("id") or "")
    if slim:
        _all_courses_cache = slim
        _all_courses_source = "anteater_api"
        _save_all_courses_disk_cache(slim)
    return list(slim)


def all_courses_source() -> str:
    """Describe the source used by the most recent whole-catalogue read."""
    return _all_courses_source or "none"


def _slim_network_course(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "department": row.get("department"),
        "courseNumber": row.get("courseNumber"),
        "courseNumeric": row.get("courseNumeric"),
        "title": row.get("title"),
        "courseLevel": row.get("courseLevel"),
        "minUnits": row.get("minUnits"),
        "maxUnits": row.get("maxUnits"),
    }


def _csv_number(value: object) -> Optional[int | float]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        number = float(raw)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _load_local_courses() -> list[dict]:
    """Build the profile picker payload from the checked-in UCI catalogue."""
    if not LOCAL_COURSES_PATH.exists():
        return []
    try:
        with LOCAL_COURSES_PATH.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error):
        logger.warning(
            "failed to read local onboarding catalogue: %s",
            LOCAL_COURSES_PATH,
            exc_info=True,
        )
        return []

    slim: list[dict] = []
    for row in rows:
        department = str(row.get("department") or "").strip()
        number = str(row.get("course_number") or "").strip()
        course_id = colloquial_course_id(department, number)
        if not course_id:
            continue
        slim.append({
            "id": course_id,
            "department": department,
            "courseNumber": number,
            "courseNumeric": _csv_number(row.get("course_numeric")),
            "title": row.get("title"),
            "courseLevel": row.get("course_level"),
            "minUnits": _csv_number(row.get("min_units")),
            "maxUnits": _csv_number(row.get("max_units")),
        })
    slim.sort(key=lambda course: course.get("id") or "")
    return slim


def _load_all_courses_disk_cache() -> list[dict]:
    if not ALL_COURSES_DISK_CACHE_PATH.exists():
        return []
    try:
        payload = json.loads(
            ALL_COURSES_DISK_CACHE_PATH.read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != ALL_COURSES_CACHE_SCHEMA_VERSION
        or not isinstance(payload.get("courses"), list)
    ):
        return []
    return [
        row for row in payload["courses"]
        if isinstance(row, dict) and row.get("id")
    ]


def _save_all_courses_disk_cache(courses: list[dict]) -> None:
    try:
        ALL_COURSES_DISK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = ALL_COURSES_DISK_CACHE_PATH.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": ALL_COURSES_CACHE_SCHEMA_VERSION,
                    "generated_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "courses": courses,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(ALL_COURSES_DISK_CACHE_PATH)
    except OSError:
        logger.warning(
            "failed to persist onboarding catalogue cache: %s",
            ALL_COURSES_DISK_CACHE_PATH,
            exc_info=True,
        )
