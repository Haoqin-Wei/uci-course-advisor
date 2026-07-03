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
from typing import Optional

from app.catalog.cache import get_catalog
from app.catalog.normalization import parse_course_mention
from app.catalog.term import Term
from app.catalog.types import CourseRef, SectionRecord
from app.data import anteater
from app.data import professors as profs
from app.memory import get_memory_manager

logger = logging.getLogger(__name__)


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
    course. DB only has IDs that are offered in a known term; for
    everything else we go straight to Anteater /courses/{id}.
    """
    ref = _to_ref(course_id)
    if not ref:
        return {"found": False, "source": "none",
                "reason": f"could not parse course id {course_id!r}"}

    # DB layer first: see if any cached CatalogView knows this course
    from app.catalog.term import get_term_registry
    for term in get_term_registry().all():
        cv = get_catalog(term)
        if not cv:
            continue
        cr = cv.get_course(ref)
        if cr:
            # CSV records lack title/units/description — only worth
            # returning if we ALSO got it from a richer source. For
            # now treat as a hint and still call Anteater for the
            # human-readable fields.
            break

    # API: Anteater /courses
    data = anteater.fetch_course(_anteater_course_key(ref))
    if data:
        return {
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

    return {"found": False, "source": "none",
            "reason": f"{ref.display()} not found in DB or Anteater"}


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
                "sections": [_section_record_to_dict(s) for s in records],
            }

    # API fallback
    sections_raw = anteater.fetch_sections(
        department=ref.department,
        course_number=ref.course_number,
        year=str(t.year),
        quarter=t.quarter,
    )
    if sections_raw:
        return {
            "found": True,
            "source": "api",
            "term": t.display(),
            "course_id": ref.display(),
            "sections": [_api_section_to_dict(s) for s in sections_raw],
        }

    return {
        "found": False,
        "source": "none",
        "term": t.display(),
        "course_id": ref.display(),
        "sections": [],
        "reason": f"no sections published for {ref.display()} in {t.display()}",
    }


# ── Prerequisites ────────────────────────────────────────

def check_prerequisites_met(
    course_id: str,
    completed_courses: Optional[list[str]] = None,
    in_progress_courses: Optional[list[str]] = None,
) -> dict:
    """
    Resolve prereqs via get_course_info then compare against the
    student's completed + in-progress lists. Both lists count toward
    satisfaction (a course currently being taken will be done before
    the term the user is planning).
    """
    info = get_course_info(course_id)
    if not info.get("found"):
        return {"found": False, "source": "none",
                "reason": info.get("reason", f"unknown course {course_id!r}")}
    prereqs_raw = info["course"].get("prerequisites") or []
    # Normalize both sides through parse_course_mention → CourseRef so
    # 'ICS33' (alias) matches 'I&C_SCI_33' (Anteater canonical) and
    # 'I&C SCI 33' (CSV form). Comparing raw strings doesn't work
    # across these formats.
    prereqs_refs = [_to_ref(p) for p in prereqs_raw if p]
    satisfied_refs: set[CourseRef] = set()
    for c in (completed_courses or []) + (in_progress_courses or []):
        ref = _to_ref(c or "")
        if ref:
            satisfied_refs.add(ref)
    missing = [r.display() for r in prereqs_refs
               if r and r not in satisfied_refs]
    required = [r.display() for r in prereqs_refs if r]
    return {
        "found": True,
        "source": info["source"],
        "course_id": info["course"].get("course_id"),
        "met": len(missing) == 0,
        "missing": missing,
        "required": required,
        "prerequisite_text": info["course"].get("prerequisite_text"),
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
    refs = cv.all_course_refs()
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
        out.append({"course_id": r.display()})
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
