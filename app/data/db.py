"""
Data Access Layer

Every public function here returns a uniform envelope:

    {"found": bool, "source": "db" | "api" | "none", ...payload..., "reason": str?}

Resolution order is always DB-first, then API:

    1. Local CatalogView (data/uci/*.csv, loaded by UCIRelationalLoader)
       — fast, deterministic, covers the terms we've crawled
    2. Anteater API (app.data.anteater)
       — live, authoritative, covers anything UCI publishes right now
    3. Not found — return {"found": False, "source": "none", "reason": "..."}

Term-strict: every term-scoped function requires a `term` parameter
and never returns data from a different term. If the student selected
Spring 2026 we DO NOT silently substitute Fall 2025 — that was the
exact failure mode that motivated this refactor.

No mock fallback anywhere. If neither DB nor API has the data, the
caller (typically an agent tool dispatcher) gets `found=False` and
the LLM is expected to say so honestly.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Optional

from app.catalog.cache import get_catalog
from app.catalog.coverage import get_term_coverage
from app.catalog.normalization import parse_course_mention
from app.catalog.term import Term
from app.catalog.types import CourseRef, CourseRecord, SectionRecord
from app import observability
from app.data import anteater
from app.data import professors as profs
from app.data.prerequisites import evaluate_prerequisite_tree
from app.memory import get_memory_manager

logger = logging.getLogger(__name__)

_course_info_cache: dict[str, dict] = {}


# ══════════════════════════════════════════════════════════
#  Internal helpers
# ══════════════════════════════════════════════════════════

def _to_ref(course_id: str) -> Optional[CourseRef]:
    """LLM-friendly course_id ('CS122A', 'I&C SCI 33') → CourseRef."""
    return parse_course_mention(course_id or "")


def _to_term(term: str) -> Optional[Term]:
    return Term.parse(term or "")


def _anteater_course_key(ref: CourseRef) -> str:
    """Anteater /courses/{id} expects 'COMPSCI122A' (no space, no underscore)."""
    return f"{ref.department.replace(' ', '')}{ref.course_number}"


def _course_cache_key(ref: CourseRef) -> str:
    return ref.display().replace(" ", "").upper()


def _course_number_sort_key(value: str) -> tuple[int, str, str]:
    import re

    text = (value or "").upper()
    match = re.match(r"([A-Z]*)(\d+)([A-Z]*)$", text)
    if not match:
        return (10_000, text, "")
    prefix, number, suffix = match.groups()
    return (int(number), prefix, suffix)


def _format_units(course: CourseRecord):
    if course.units is not None:
        return int(course.units) if float(course.units).is_integer() else course.units
    if course.min_units is None and course.max_units is None:
        return None
    if course.min_units == course.max_units:
        value = course.min_units
        return int(value) if value is not None and float(value).is_integer() else value

    def fmt(value):
        if value is None:
            return "?"
        return str(int(value)) if float(value).is_integer() else str(value)

    return f"{fmt(course.min_units)}–{fmt(course.max_units)}"


def _course_has_local_metadata(course: CourseRecord) -> bool:
    return any(
        value not in (None, "", (), [])
        for value in (
            course.title,
            course.units,
            course.min_units,
            course.max_units,
            course.description,
            course.course_level,
            course.restriction,
            course.prerequisite_text,
            course.prerequisite_tree,
            course.prerequisites,
        )
    )


def _course_record_to_dict(course: CourseRecord) -> dict:
    provenance = course.provenance
    provenance_dict = (
        {
            "source_term": provenance.source_term,
            "target_term": provenance.target_term,
            "loader": provenance.loader,
            "source_file": provenance.source_file,
            "is_historical_proxy": provenance.is_historical_proxy,
        }
        if provenance
        else None
    )
    return {
        "course_id": course.ref.display(),
        "title": course.title,
        "units": _format_units(course),
        "min_units": course.min_units,
        "max_units": course.max_units,
        "level": course.course_level,
        "school": course.school,
        "department": course.department_name or course.ref.department,
        "description": course.description,
        "same_as": course.same_as,
        "restriction": course.restriction,
        "prerequisite_text": course.prerequisite_text,
        "prerequisite_tree": course.prerequisite_tree,
        "prerequisites": [ref.display() for ref in course.prerequisites],
        "dependencies": [ref.display() for ref in course.dependencies],
        "ge_categories": list(course.ge_categories),
        "terms_offered": list(course.terms_offered),
        "all_known_instructors": list(course.all_known_instructors),
        "provenance": provenance_dict,
    }


def _find_local_course_record(ref: CourseRef) -> Optional[CourseRecord]:
    from app.catalog.term import get_term_registry

    for term in get_term_registry().all():
        cv = get_catalog(term)
        if not cv:
            continue
        record = cv.get_course(ref)
        if record and _course_has_local_metadata(record):
            return record
    return None


def _section_record_to_dict(s: SectionRecord) -> dict:
    """Shape that agent tools serialize back to the LLM. Plain primitives
    only — no dataclasses, no None vs missing ambiguity for the LLM."""
    # `restrictions` may not be on older SectionRecord dataclasses
    # (CSV loader hasn't been re-cut). Tolerate missing attribute.
    return {
        "section_code":  s.section_code,
        "section_num":   getattr(s, "section_num", None),  # "A" / "A1" / "B" / "C3"
        "section_type":  s.section_type,
        "days":          s.days,
        "start_time":    s.start_time,
        "end_time":      s.end_time,
        "time_display":  (f"{s.days} {s.start_time}-{s.end_time}"
                          if s.days and s.start_time else None),
        "location":      s.location,
        "instructors":   list(s.instructors),
        "max_capacity":  s.max_capacity,
        "enrolled":      s.section_enrolled,
        "seats_open":    s.seats_open,
        "waitlisted":    s.num_on_waitlist,
        "status":        s.status,
        "is_cancelled":  s.is_cancelled,
        "ge_categories": list(s.ge_categories),
        "restrictions":  getattr(s, "restrictions", None),
        # CSV loader doesn't carry finalExam — None is fine, frontend
        # gracefully shows "TBA" in that case.
        "final_exam":    getattr(s, "final_exam", None),
    }


def _api_section_to_dict(sec: dict) -> dict:
    """Anteater websoc section → same shape as _section_record_to_dict."""
    meetings = sec.get("meetings") or []
    primary  = meetings[0] if meetings else {}
    start = primary.get("startTime") or {}
    end   = primary.get("endTime") or {}
    days  = primary.get("days")
    st = (f"{start['hour']:02d}:{start['minute']:02d}"
          if start and start.get("hour") is not None else None)
    et = (f"{end['hour']:02d}:{end['minute']:02d}"
          if end and end.get("hour") is not None else None)
    location = ", ".join(primary.get("bldg") or []) or None
    cap = _safe_int(sec.get("maxCapacity"))
    enrolled = _safe_int(sec.get("numCurrentlyEnrolled", {}).get("totalEnrolled")
                         if isinstance(sec.get("numCurrentlyEnrolled"), dict)
                         else sec.get("totalEnrolled"))
    seats_open = (max(0, cap - enrolled) if cap is not None and enrolled is not None
                  else None)
    return {
        "section_code":  sec.get("sectionCode"),
        "section_num":   sec.get("sectionNum"),       # "A" / "A1" / "B" / "C3" — the group letter is the prefix
        "section_type":  sec.get("sectionType"),
        "days":          days,
        "start_time":    st,
        "end_time":      et,
        "time_display":  (f"{days} {st}-{et}" if days and st else None),
        "location":      location,
        "instructors":   list(sec.get("instructors") or []),
        "max_capacity":  cap,
        "enrolled":      enrolled,
        "seats_open":    seats_open,
        "waitlisted":    _safe_int(sec.get("numOnWaitlist")),
        "status":        sec.get("status"),
        "is_cancelled":  False,
        "ge_categories": [],
        # Anteater returns `restrictions` as a concatenated string like
        # "A" or "AB" or "EJL" — the SOC Rstr column. Surface as-is
        # so the dispatcher can decode against RESTRICTION_CODES.
        "restrictions":  sec.get("restrictions"),
        # finalExam: dict with examStatus + (if scheduled) dayOfWeek,
        # month, day, startTime/endTime {hour, minute}, bldg.
        # Pass through verbatim so the frontend can format.
        "final_exam":    sec.get("finalExam"),
    }


def _api_live_section_to_dict(sec: dict, *, retrieved_at: Optional[str]) -> dict:
    """Anteater WebSoc section → live availability schema."""

    base = _api_section_to_dict(sec)
    waitlist_capacity = _safe_int(sec.get("numWaitlistCap"))
    new_only_reserved = _safe_int(sec.get("numNewOnlyReserved"))
    updated_at = sec.get("updatedAt")
    return {
        **base,
        "waitlist_capacity": waitlist_capacity,
        "new_only_reserved": new_only_reserved,
        "updated_at": updated_at,
        "retrieved_at": retrieved_at,
        "source": "live_anteater_websoc",
        "is_live": True,
    }


def _local_not_live_section_to_dict(s: SectionRecord, *, reason: str) -> dict:
    return {
        **_section_record_to_dict(s),
        "waitlist_capacity": None,
        "new_only_reserved": None,
        "updated_at": None,
        "retrieved_at": None,
        "source": "local_not_live",
        "is_live": False,
        "not_live_reason": reason,
    }


def _safe_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


# ══════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════

# ── Student profile (memory layer; already real) ─────────

def get_student_profile(student_id: str) -> dict:
    """Read the durable profile dict from the memory provider. No
    fallback — if the user has never been seen, returns found=False."""
    try:
        mem = get_memory_manager()
        profile = mem.get_profile(student_id)
    except Exception as e:
        logger.warning("get_student_profile failed: %s", e)
        return {"found": False, "source": "none",
                "reason": f"profile lookup failed: {e}"}
    if not profile:
        return {"found": False, "source": "none",
                "reason": f"no profile recorded for {student_id}"}
    return {"found": True, "source": "db", "profile": profile}


# ── Single course catalog metadata ───────────────────────

def get_course_info(course_id: str) -> dict:
    """
    Catalog metadata (title, units, description, prerequisites) for one
    course. Local courses.csv metadata is authoritative when present;
    Anteater is only a fallback for courses absent from the local build.
    """
    ref = _to_ref(course_id)
    if not ref:
        return {"found": False, "source": "none",
                "reason": f"could not parse course id {course_id!r}"}

    cache_key = _course_cache_key(ref)
    if cache_key in _course_info_cache:
        return deepcopy(_course_info_cache[cache_key])

    record = _find_local_course_record(ref)
    if record:
        result = {
            "found": True,
            "source": "db",
            "course": _course_record_to_dict(record),
        }
        _course_info_cache[cache_key] = deepcopy(result)
        return result

    # API: Anteater /courses
    data = anteater.fetch_course(_anteater_course_key(ref))
    if data:
        result = {
            "found": True,
            "source": "api",
            "course": {
                "course_id":   ref.display(),
                "title":       data.get("title"),
                "units":       data.get("minUnits") if data.get("minUnits") == data.get("maxUnits")
                               else f"{data.get('minUnits')}–{data.get('maxUnits')}",
                "level":       data.get("courseLevel"),
                "school":      data.get("school"),
                "department":  data.get("departmentName"),
                "description": data.get("description"),
                "prerequisite_text": data.get("prerequisiteText"),
                "prerequisites":     [p.get("id") for p in (data.get("prerequisites") or [])],
                "all_known_instructors": data.get("instructors") or [],
            },
        }
        _course_info_cache[cache_key] = deepcopy(result)
        return result

    return {"found": False, "source": "none",
            "reason": f"{ref.display()} not found in DB or Anteater"}


def batch_get_course_info(course_ids: list[str]) -> dict:
    """Batch metadata enrichment for callers that already enumerated courses.

    Results use the exact same course payload as get_course_info(). Duplicate
    inputs are collapsed by canonical CourseRef while preserving first-seen
    output order.
    """
    courses: list[dict] = []
    missing: list[dict] = []
    seen: set[str] = set()

    for raw_course_id in course_ids or []:
        ref = _to_ref(raw_course_id)
        if not ref:
            missing.append({
                "course_id": raw_course_id,
                "reason": f"could not parse course id {raw_course_id!r}",
            })
            continue
        key = _course_cache_key(ref)
        if key in seen:
            continue
        seen.add(key)
        info = get_course_info(raw_course_id)
        if info.get("found") and info.get("course"):
            courses.append(info["course"])
        else:
            missing.append({
                "course_id": ref.display(),
                "reason": info.get("reason", "not found"),
            })

    return {
        "found": bool(courses),
        "source": "db_or_api" if courses else "none",
        "courses": courses,
        "missing": missing,
        "total_found": len(courses),
    }


# ── Sections (term-strict) ───────────────────────────────

def get_sections(course_id: str, term: str) -> dict:
    """
    Sections for a course in a specific term. DB first (CatalogView for
    that term), then Anteater websoc. Returns an EMPTY list if neither
    has data — distinct from found=False which means we could not even
    interpret the request.
    """
    ref = _to_ref(course_id)
    if not ref:
        return {"found": False, "source": "none", "sections": [],
                "reason": f"could not parse course id {course_id!r}"}
    t = _to_term(term)
    if not t:
        return {"found": False, "source": "none", "sections": [],
                "reason": f"could not parse term {term!r} (expected e.g. 'Spring 2026')"}
    coverage = get_term_coverage(t)
    coverage_status = coverage.get("coverage_status")
    if coverage_status in {"partial", "stale", "unavailable"}:
        observability.increment("data.coverage_status", status=coverage_status)
        observability.log_event(
            logger,
            logging.WARNING,
            "data_coverage",
            term=t.display(),
            course_id=ref.display(),
            status=coverage_status,
            source="catalog",
            updated_at=coverage.get("updated_at"),
        )

    # DB
    cv = get_catalog(t)
    if cv:
        records = cv.get_sections(ref)
        if records:
            return {
                "found": True,
                "source": "db",
                "term": t.display(),
                "course_id": ref.display(),
                "coverage_status": coverage.get("coverage_status"),
                "data_coverage": coverage,
                "sections": [_section_record_to_dict(s) for s in records],
            }

        if coverage_status in {"complete", "partial", "stale"}:
            if coverage_status == "complete":
                return {
                    "found": False,
                    "source": "db",
                    "term": t.display(),
                    "course_id": ref.display(),
                    "coverage_status": coverage_status,
                    "data_coverage": coverage,
                    "sections": [],
                    "reason": f"no sections published for {ref.display()} in {t.display()}",
                }
            return {
                "found": False,
                "source": "db",
                "term": t.display(),
                "course_id": ref.display(),
                "coverage_status": coverage_status,
                "data_coverage": coverage,
                "sections": [],
                "reason": (
                    f"local data for {t.display()} is {coverage_status}; "
                    f"cannot confirm whether {ref.display()} has no sections"
                ),
            }

    # API fallback only when local term data is unavailable. Complete
    # local terms should not need live confirmation; partial/stale terms
    # deliberately return cannot-confirm above instead of silently
    # presenting missing local rows as definitive no-offering facts.
    try:
        sections_raw = anteater.fetch_sections(
            department=ref.department,
            course_number=ref.course_number,
            year=str(t.year),
            quarter=t.quarter,
        )
    except Exception as e:
        logger.warning("live section fallback failed for %s %s: %s", ref.display(), t.display(), e)
        observability.increment("data.refresh_failures", source="anteater")
        observability.log_event(
            logger,
            logging.WARNING,
            "data_refresh_failure",
            source="anteater",
            term=t.display(),
            course_id=ref.display(),
            error=f"{type(e).__name__}: {e}",
        )
        sections_raw = None
    if sections_raw:
        return {
            "found": True,
            "source": "api",
            "term": t.display(),
            "course_id": ref.display(),
            "coverage_status": coverage.get("coverage_status"),
            "data_coverage": coverage,
            "sections": [_api_section_to_dict(s) for s in sections_raw],
        }

    if coverage.get("coverage_status") == "unavailable":
        return {
            "found": False,
            "source": "none",
            "term": t.display(),
            "course_id": ref.display(),
            "coverage_status": "unavailable",
            "data_coverage": coverage,
            "sections": [],
            "reason": (
                f"no local catalog data for {t.display()} and live API could not "
                f"confirm sections for {ref.display()}"
            ),
        }

    return {
        "found": False,
        "source": "none",
        "term": t.display(),
        "course_id": ref.display(),
        "coverage_status": coverage.get("coverage_status"),
        "data_coverage": coverage,
        "sections": [],
        "reason": f"no sections published for {ref.display()} in {t.display()}",
    }


def get_live_sections(
    course_id: str,
    term: str,
    section_codes: Optional[list[str]] = None,
    force_refresh: bool = False,
) -> dict:
    """
    Live availability/status for a course in a specific term.

    This is intentionally separate from `get_sections()`: ordinary
    planning can use deterministic local catalog data, but questions
    about current seats, waitlists, OPEN/FULL/Waitl status, NOR, or
    restriction codes should use this live WebSoc path first.
    """

    ref = _to_ref(course_id)
    if not ref:
        return {"found": False, "source": "none", "sections": [],
                "is_live": False,
                "reason": f"could not parse course id {course_id!r}"}
    t = _to_term(term)
    if not t:
        return {"found": False, "source": "none", "sections": [],
                "is_live": False,
                "reason": f"could not parse term {term!r} (expected e.g. 'Spring 2026')"}

    normalized_codes = [
        str(code).strip()
        for code in (section_codes or [])
        if str(code).strip()
    ]
    coverage = get_term_coverage(t)

    try:
        live = anteater.fetch_live_sections(
            year=str(t.year),
            quarter=t.quarter,
            department=ref.department,
            course_number=ref.course_number,
            section_codes=normalized_codes or None,
            force_refresh=force_refresh,
        )
    except Exception as e:
        logger.warning("live WebSoc availability failed for %s %s: %s", ref.display(), t.display(), e)
        observability.increment("data.refresh_failures", source="anteater_live_websoc")
        observability.log_event(
            logger,
            logging.WARNING,
            "data_refresh_failure",
            source="anteater_live_websoc",
            term=t.display(),
            course_id=ref.display(),
            error=f"{type(e).__name__}: {e}",
        )
        live = None

    if live is not None:
        sections_raw = live.get("sections") or []
        sections = [
            _api_live_section_to_dict(s, retrieved_at=live.get("retrieved_at"))
            for s in sections_raw
        ]
        return {
            "found": bool(sections),
            "source": "live_anteater_websoc",
            "is_live": True,
            "term": t.display(),
            "course_id": ref.display(),
            "coverage_status": coverage.get("coverage_status"),
            "data_coverage": coverage,
            "retrieved_at": live.get("retrieved_at"),
            "cache_hit": bool(live.get("cache_hit")),
            "sections": sections,
            "reason": None if sections else (
                f"live WebSoc returned no sections for {ref.display()} in {t.display()}"
            ),
        }

    fallback_reason = "live Anteater WebSoc unavailable; local data is not current availability"
    cv = get_catalog(t)
    if cv:
        records = cv.get_sections(ref)
        if normalized_codes:
            code_set = set(normalized_codes)
            records = [s for s in records if s.section_code in code_set]
        if records:
            return {
                "found": True,
                "source": "local_not_live",
                "is_live": False,
                "term": t.display(),
                "course_id": ref.display(),
                "coverage_status": coverage.get("coverage_status"),
                "data_coverage": coverage,
                "retrieved_at": None,
                "cache_hit": False,
                "sections": [
                    _local_not_live_section_to_dict(s, reason=fallback_reason)
                    for s in records
                ],
                "reason": fallback_reason,
            }

    return {
        "found": False,
        "source": "none",
        "is_live": False,
        "term": t.display(),
        "course_id": ref.display(),
        "coverage_status": coverage.get("coverage_status"),
        "data_coverage": coverage,
        "retrieved_at": None,
        "cache_hit": False,
        "sections": [],
        "reason": (
            f"live WebSoc could not verify current availability for "
            f"{ref.display()} in {t.display()}"
        ),
    }


# ── Prerequisites ────────────────────────────────────────

def check_prerequisites_met(
    course_id: str,
    completed_courses: Optional[list[str]] = None,
    in_progress_courses: Optional[list[str]] = None,
    allow_in_progress: bool = True,
) -> dict:
    """
    Resolve prereqs via get_course_info then evaluate the structured
    prerequisite tree against the student's completed + in-progress
    lists. In-progress courses count by default because this tool is
    used for future-term planning; the response makes that policy
    explicit via `in_progress_policy`.
    """
    info = get_course_info(course_id)
    if not info.get("found"):
        return {"found": False, "source": "none",
                "status": "unknown",
                "met": False,
                "missing": [],
                "unknown": [info.get("reason", f"unknown course {course_id!r}")],
                "reason": info.get("reason", f"unknown course {course_id!r}")}

    course = info["course"]
    result = evaluate_prerequisite_tree(
        course.get("prerequisite_tree"),
        completed_courses=completed_courses or [],
        in_progress_courses=in_progress_courses or [],
        flat_prerequisites=course.get("prerequisites") or [],
        prerequisite_text=course.get("prerequisite_text"),
        allow_in_progress=allow_in_progress,
    )
    required = course.get("prerequisites") or []
    return {
        "found": True,
        "source": info["source"],
        "course_id": course.get("course_id"),
        "status": result.status,
        "met": result.met,
        "missing": result.missing,
        "unknown": result.unknown,
        "satisfied": result.satisfied,
        "in_progress_used": result.in_progress_used,
        "required": required,
        "prerequisite_text": course.get("prerequisite_text"),
        "prerequisite_tree": course.get("prerequisite_tree"),
        "in_progress_policy": (
            "counts_for_future_term" if allow_in_progress else "ignored"
        ),
    }


# ── Search courses (DB-only, term-scoped) ────────────────

def search_courses(
    term: str,
    department: Optional[str] = None,
    ge_category: Optional[str] = None,
    exclude_ids: Optional[list[str]] = None,
) -> dict:
    """
    Catalog search restricted to a single term. We list CourseRefs from
    that term's CatalogView; the agent can call get_course_info on any
    candidate to get title/units/description.

    Anteater has no multi-criteria search endpoint, so DB-only here.
    If the local CSV doesn't have that term, we report no results
    rather than silently returning data from a different term.
    """
    t = _to_term(term)
    if not t:
        return {"found": False, "source": "none", "courses": [],
                "reason": f"could not parse term {term!r}"}
    cv = get_catalog(t)
    if not cv:
        return {"found": False, "source": "none", "courses": [],
                "reason": f"no local catalog data for {t.display()}"}
    excluded = {(c or "").replace(" ", "").replace("_", "").upper()
                for c in (exclude_ids or [])}
    refs = sorted(
        cv.all_course_refs(),
        key=lambda r: (r.department, _course_number_sort_key(r.course_number), r.course_number),
    )
    out = []
    for r in refs:
        if department and r.department != department.upper():
            continue
        if (r.display().replace(" ", "").upper()) in excluded:
            continue
        if ge_category:
            cr = cv.get_course(r)
            if not cr or ge_category not in (cr.ge_categories or ()):
                continue
        cr = cv.get_course(r)
        entry = {"course_id": r.display()}
        if cr:
            entry.update({
                "title": cr.title,
                "units": _format_units(cr),
                "level": cr.course_level,
                "ge_categories": list(cr.ge_categories),
            })
        out.append(entry)
    return {
        "found": True,
        "source": "db",
        "term": t.display(),
        "courses": out[:30],
        "total_found": len(out),
        "truncated": len(out) > 30,
    }


# ── Professor / instructor rating ────────────────────────

def get_professor_rating(
    instructor_name: str,
    department: Optional[str] = None,
) -> dict:
    """
    Instructor rating block. DB-first (local RMP snapshot in
    data/professor/uci_professors.json via app.data.professors), then
    Anteater /instructors/{key} for catalog metadata the snapshot
    doesn't carry (ucinetid, email, title, courseHistory).

    `department` is the UCI dept code ('COMPSCI', 'BIO SCI', 'PUBHLTH')
    used to disambiguate common surnames like 'LEE, J.' where multiple
    professors share lastname+initial. Always pass it when the
    instructor comes from a known course.

    The local snapshot gives us the authoritative RMP numbers plus a
    Steam-style `tier` block (好评如潮 / 褒贬不一 / 差评如潮 /
    样本不足 / 暂无评分). When the local lookup misses we still call
    Anteater so the agent can answer questions about instructors
    who exist at UCI but aren't on RMP.
    """
    if not (instructor_name or "").strip():
        return {"found": False, "source": "none",
                "reason": "empty instructor name"}

    rec = profs.lookup_professor(instructor_name, department=department)
    if rec:
        profile = profs.build_profile(rec)
        return {
            "found": True,
            "source": "db",
            "instructor": profile,
        }

    data = anteater.fetch_instructor(instructor_name)
    if not data:
        return {"found": False, "source": "none",
                "reason": (
                    f"no local RMP record for {instructor_name!r} and Anteater "
                    "returned no profile either"
                )}
    return {
        "found": True,
        "source": "api",
        "instructor": {
            "name":            data.get("name"),
            "ucinetid":        data.get("ucinetid"),
            "title":           data.get("title"),
            "department":      data.get("department"),
            "shortened_names": data.get("shortenedNames") or [],
            "email":           data.get("email"),
            "rating":          data.get("rating"),
            "courseHistory":   data.get("courseHistory"),
            "tier":            profs.classify_tier(None, 0),
        },
    }


def get_professor_reviews(
    instructor_name: str,
    course: Optional[str] = None,
    limit: int = 5,
    department: Optional[str] = None,
) -> dict:
    """Top-N student reviews for an instructor, optionally filtered to a course.

    Local-only — reviews live in data/professor/professor_reviews.db.
    Returns {found, source, instructor, course, reviews, stats}.

    Pass `department` (e.g. 'COMPSCI') to disambiguate common surnames.
    """
    if not (instructor_name or "").strip():
        return {"found": False, "source": "none",
                "reason": "empty instructor name"}

    rec = profs.lookup_professor(instructor_name, department=department)
    if not rec:
        return {"found": False, "source": "none",
                "reason": f"no local RMP record for {instructor_name!r}"}

    legacy = rec.get("legacyId")
    reviews = profs.get_reviews(legacy, course=course, limit=limit)
    if not reviews:
        return {
            "found":      False,
            "source":     "db",
            "instructor": f"{rec.get('firstName','')} {rec.get('lastName','')}".strip(),
            "course":     course,
            "reason": (
                f"no reviews found for that instructor"
                + (f" in {course}" if course else "")
            ),
        }

    return {
        "found":      True,
        "source":     "db",
        "instructor": f"{rec.get('firstName','')} {rec.get('lastName','')}".strip(),
        "legacy_id":  legacy,
        "course":     course,
        "stats":      profs.review_stats(legacy, course=course),
        "reviews":    reviews,
    }


async def get_professor_summary(
    instructor_name: str,
    course: Optional[str] = None,
    department: Optional[str] = None,
    force_refresh: bool = False,
) -> dict:
    """LLM-summarized strengths/weaknesses/best_for/avoid_if for an instructor.

    Cache-first per (legacy_id, course) on disk under
    data/professor/summaries/. The first call for a (prof, course) pair
    costs one LLM round-trip; every later call is free.

    Pass `department` to disambiguate common surnames, `course` to get
    a course-specific summary (otherwise summarizes all reviews).
    """
    if not (instructor_name or "").strip():
        return {"found": False, "source": "none",
                "reason": "empty instructor name"}

    rec = profs.lookup_professor(instructor_name, department=department)
    if not rec:
        return {"found": False, "source": "none",
                "reason": f"no local RMP record for {instructor_name!r}"}

    from app.data import professor_summary as ps
    result = await ps.summarize_professor(
        rec.get("legacyId"), course=course, force_refresh=force_refresh,
    )
    if result.get("found"):
        result["instructor"] = (
            f"{rec.get('firstName','')} {rec.get('lastName','')}".strip()
        )
    return result


def get_professor_tags(
    instructor_name: str,
    course: Optional[str] = None,
    department: Optional[str] = None,
) -> dict:
    """Top tags students applied to an instructor (optionally per-course).

    Pass `department` (e.g. 'COMPSCI') to disambiguate common surnames.
    """
    if not (instructor_name or "").strip():
        return {"found": False, "source": "none",
                "reason": "empty instructor name"}

    rec = profs.lookup_professor(instructor_name, department=department)
    if not rec:
        return {"found": False, "source": "none",
                "reason": f"no local RMP record for {instructor_name!r}"}

    legacy = rec.get("legacyId")
    tags = profs.aggregate_tags(legacy, course=course)
    return {
        "found":      bool(tags),
        "source":     "db",
        "instructor": f"{rec.get('firstName','')} {rec.get('lastName','')}".strip(),
        "course":     course,
        "tags":       tags,
    }


# ── Grade distribution (delegates to grades.py) ─────────

def get_grade_distribution(course_id: str) -> dict:
    """
    Historical grade aggregate. grades.py already does its own
    DB-cache → API fetch dance, so we just adapt the return shape.
    """
    from app.data import grades as grades_module
    data = grades_module.get_grade_distribution(course_id)
    if not data:
        return {"found": False, "source": "none",
                "reason": f"no grade data published for {course_id!r}"}
    # grades.py decides cache vs live internally; we don't know which.
    return {"found": True, "source": "db_or_api", "grades": data,
            "course_id": course_id}
