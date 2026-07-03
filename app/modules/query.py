"""
Query Module

Maps to SKILL.md: Step 3 — Search Knowledge Base.

Routes queries to the appropriate data layer functions based on
the type of question:
  - Course recommendation / filtering → search_courses
  - Professor comparison / RMP        → get_professor_rating
  - Difficulty / grade distribution    → get_grade_distribution
  - Prerequisite check                 → check_prerequisites_met
  - Time conflict / scheduling         → get_sections + conflict check
  - GE / major requirement             → search_courses with filters

Applies SKILL.md filtering rules:
  1. Exclude already-taken or enrolled courses
  2. Flag unmet prerequisites (don't include as primary recommendations)
  3. Flag courses that conflict with current schedule
  4. Apply user preferences (easy, high-rated professor, time slot)
"""

from typing import Optional
from app.data import db
from app.modules import conflict


def query_course_recommendations(
    term: str,
    major: Optional[str] = None,
    completed_courses: Optional[list[str]] = None,
    selected_courses: Optional[list[str]] = None,
    difficulty_preference: Optional[str] = None,
    recommendation_goal: Optional[str] = None,
) -> dict:
    """
    Main recommendation query. Returns structured results with
    primary recommendations, alternatives, and warnings.
    """
    completed = completed_courses or []
    selected = selected_courses or []
    exclude_ids = completed + selected

    # ── Fetch candidate courses ───────────────────────────
    if recommendation_goal == "ge_fulfillment":
        candidates = [c for c in db.search_courses(exclude_ids=exclude_ids) if c.get("ge_category")]
    elif recommendation_goal == "major_requirement" and major:
        candidates = db.search_courses(major_requirement=major, exclude_ids=exclude_ids)
    else:
        candidates = db.search_courses(exclude_ids=exclude_ids)

    # ── Build the student's current schedule for conflict checks ──
    # Each selected course → its actual sections in this term. Conflict
    # detection compares candidate sections against these.
    student_schedule_sections: list[dict] = []
    for sel_cid in selected:
        student_schedule_sections.extend(db.get_sections(sel_cid, term))

    # ── Enrich each candidate ─────────────────────────────
    enriched = []
    for course in candidates:
        cid = course["course_id"]

        # Prerequisites check — selected courses count as "in progress",
        # i.e. will be completed in time for next-term planning.
        prereq_status = db.check_prerequisites_met(
            cid, completed, in_progress_courses=selected,
        )

        # Sections for the requested term
        sections = db.get_sections(cid, term)
        if not sections:
            continue  # No sections offered this term

        # Grade distribution
        grades = db.get_grade_distribution(cid)

        # Professor ratings for each section
        section_details = []
        for sec in sections:
            prof_rating = db.get_professor_rating(sec["instructor"])
            section_details.append({**sec, "professor_rating": prof_rating})

        # Surface the main lecture before discussion/lab sections so the
        # card preview displays the canonical meeting, not a Friday-only Dis.
        _TYPE_ORDER = {"Lec": 0, "Sem": 1, "Stu": 2, "Lab": 3, "Dis": 4, "Tut": 5}
        section_details.sort(
            key=lambda s: (_TYPE_ORDER.get(s.get("sectionType", ""), 99),
                           s.get("sectionCode", "")),
        )

        # Time-conflict check (Phase 2.8) — per-section, then rolled up.
        section_conflicts = conflict.find_conflicts(
            section_details, student_schedule_sections,
        )
        # Attach per-section conflict info so the LLM and frontend can
        # show "this Lec conflicts but Tu Lab is free" detail if needed.
        for sec in section_details:
            sec["conflicts_with"] = section_conflicts.get(sec.get("section_id", ""), [])

        conflict_info = conflict.summarize_for_card(section_details, section_conflicts)
        # `has_conflict` is True only when *every* section conflicts — partial
        # conflicts still leave the student a route via the unblocked section.
        has_conflict = conflict_info["status"] == "all"

        enriched.append({
            "course": course,
            "prereq_met": prereq_status["met"],
            "prereq_missing": prereq_status["missing"],
            "sections": section_details,
            "grade_distribution": grades,
            "has_conflict": has_conflict,
            "conflict_summary": conflict_info["summary"],
            "conflict_status":  conflict_info["status"],
        })

    # ── Sort by preference ────────────────────────────────
    enriched = _sort_by_preference(enriched, difficulty_preference, recommendation_goal)

    # ── Separate primary vs flagged ───────────────────────
    primary = [e for e in enriched if e["prereq_met"] and not e["has_conflict"]]
    flagged = [e for e in enriched if not e["prereq_met"] or e["has_conflict"]]

    return {
        "primary": primary[:5],
        "flagged": flagged[:3],
        "total_found": len(enriched),
    }


def query_single_course(course_id: str, term: Optional[str] = None) -> Optional[dict]:
    """Retrieve full details for a single course."""
    course = db.get_course_info(course_id)
    if not course:
        return None

    sections = db.get_sections(course_id, term)
    grades = db.get_grade_distribution(course_id)
    prereqs = db.get_prerequisites(course_id)

    section_details = []
    for sec in sections:
        prof = db.get_professor_rating(sec["instructor"])
        section_details.append({**sec, "professor_rating": prof})

    return {
        "course": course,
        "sections": section_details,
        "grade_distribution": grades,
        "prerequisites": prereqs,
    }


def query_professor(instructor_name: str) -> Optional[dict]:
    """Retrieve professor rating details."""
    return db.get_professor_rating(instructor_name)


def check_schedule_conflict(
    course_id: str,
    student_id: str,
    term: str,
) -> dict:
    raise NotImplementedError(
        "Schedule conflict checks must use app.scheduling service with an "
        "explicit pending schedule; the old demo placeholder was removed."
    )


# ── Internal helpers ─────────────────────────────────────

def _sort_by_preference(
    enriched: list[dict],
    difficulty_pref: Optional[str],
    goal: Optional[str],
) -> list[dict]:
    """
    Sort candidates based on user preferences.
    TODO: Make scoring more sophisticated.
    """
    def score(item):
        s = 0
        grades = item.get("grade_distribution") or {}

        # Prefer higher avg GPA if user wants easy
        if difficulty_pref == "easy":
            s += grades.get("avg_gpa", 0) * 10

        # Prefer professor rating
        best_prof = 0
        for sec in item.get("sections", []):
            pr = sec.get("professor_rating") or {}
            overall = pr.get("overall") or 0
            best_prof = max(best_prof, overall)
        s += best_prof * 5

        # Bonus for major requirement match
        if goal == "major_requirement":
            s += 10 if item["course"]["major_requirement"] else 0

        return s

    enriched.sort(key=score, reverse=True)
    return enriched
