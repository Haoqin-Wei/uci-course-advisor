"""One view of a student's academic record for recommendations.

Courses come from four places: the transcript (with grades), exam credit
mapped through the campus chart, transfer credit the student mapped by hand,
and courses in progress this term. Prerequisite checks, the degree path and
GE progress all read this view, so a course counted in one place is counted
in the others.
"""

from __future__ import annotations

from typing import Iterable

from app.academic import store
from app.catalog.course_equivalents import equivalents, expand

# Credit without a letter grade satisfies a prerequisite's minimum grade:
# the campus granted the course.
CREDIT_GRADE = "P"


def recommendation_record(user_id: str, attempts: Iterable) -> dict:
    """``attempts`` are the transcript's passed courses, as IDs or ``{course_id, grade}``."""
    details = store.get_academic_details(user_id)
    exams = store.get_exam_credits(user_id)
    graded: dict[str, dict] = {}
    for attempt in attempts or []:
        entry = dict(attempt) if isinstance(attempt, dict) else {"course_id": str(attempt), "grade": None}
        course = str(entry.get("course_id") or "").strip()
        if not course:
            continue
        # A renamed course answers to every ID it has had (IN4MATX 43 / SWE 43).
        for name in equivalents(course):
            graded.setdefault(name, {**entry, "course_id": name})
    transcript_courses = [str(a.get("course_id") if isinstance(a, dict) else a) for a in attempts or []]
    credited = [course for exam in exams for course in exam.get("credited_courses") or []]
    credited += details.get("transfer_courses") or []
    for name in expand(credited):
        graded.setdefault(name, {"course_id": name, "grade": CREDIT_GRADE})
    in_progress = [course for course in details.get("in_progress_courses") or []]
    return {
        "completed_course_attempts": list(graded.values()),
        "credited_courses": expand(credited),
        "in_progress_courses": expand(in_progress),
        "in_progress_term": details.get("in_progress_term"),
        # Everything not to recommend again and to count toward requirements.
        "courses_done": expand([*transcript_courses, *credited, *in_progress]),
        "ge_basis": {
            # One entry per course actually earned; a renamed or "A or B" credit is not doubled.
            "courses": [*transcript_courses, *in_progress, *(details.get("transfer_courses") or [])],
            "exam_credits": [{"ge_courses": exam.get("ge_courses") or [], "ge_direct": exam.get("ge_direct") or {}}
                             for exam in exams],
            "confirmed": list(details.get("ge_confirmed") or []),
            # Stated as still open: a real gap, where a transcript alone could not tell.
            "not_met": list(details.get("ge_not_met") or []),
        },
        "official_major": details.get("official_major"),
        "exam_credits_total": len(exams),
        "exam_credits_unmatched": sum(1 for exam in exams if not exam.get("matched")),
    }
