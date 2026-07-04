"""
Tool definitions exposed to the agent loop.

Each tool is two things:
  1. A JSON schema entry in TOOL_SCHEMAS (sent to the LLM via the
     OpenAI-compatible `tools=[]` parameter).
  2. A Python dispatcher in DISPATCH that the loop calls.

Dispatchers thinly wrap `app.data.db`. db.py is the layer that does
DB-first → API-fallback → {found, source, reason}; tools just pass
results back to the LLM. Term-strict: tools that read term-scoped
data require a `term` argument from the model; if the model forgets,
we inject the student's currently-selected term from the tool
context as a safety net so we never silently return data from the
wrong term.

`dispatch(name, args, context)` is the single entry point. `context`
carries per-request state: user_id + selected term.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Optional

from app.data import db
from app.scheduling import (
    resolve_pending_schedule_sections,
    sections_overlap,
    validate_schedule_bundle,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════
#  JSON schemas — what the LLM sees
# ══════════════════════════════════════════════════════════

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_course_info",
            "description": (
                "Get a UCI course's catalog metadata (title, units, "
                "level, school, description, prerequisite text). "
                "Term-agnostic — returns the course as it exists in "
                "the catalog regardless of when it's offered."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "course_id": {
                        "type": "string",
                        "description": "Course code in any common form: 'CS122A', 'COMPSCI 122A', 'ICS33'.",
                    },
                },
                "required": ["course_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sections",
            "description": (
                "List sections of a course in a SPECIFIC term: section "
                "code, lecture/discussion type, days, time, location, "
                "instructors, capacity, enrolled count, seats_open. "
                "ALWAYS pass `term` — never assume the term from "
                "context. Returns sections=[] with found=false if the "
                "course isn't offered that term."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "course_id": {"type": "string"},
                    "term": {
                        "type": "string",
                        "description": "Required. Form: 'Spring 2026', 'Fall 2026', 'Spring 2025'.",
                    },
                },
                "required": ["course_id", "term"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_grade_distribution",
            "description": (
                "Historical grade distribution for a course (A/B/C "
                "percentages + average GPA), aggregated across all "
                "past offerings. Use when the student asks about "
                "difficulty, GPA impact, or 'is this an easy class'."
            ),
            "parameters": {
                "type": "object",
                "properties": {"course_id": {"type": "string"}},
                "required": ["course_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_professor_rating",
            "description": (
                "RateMyProfessor-style rating for an instructor: "
                "avg_rating (1-5), avg_difficulty, would_take_again_pct, "
                "num_ratings, plus a Steam-style `tier` block:\n"
                "  - mostly_positive (好评如潮) — avg_rating ≥ 4.2 AND ≥10 ratings\n"
                "  - mostly_negative (差评如潮) — avg_rating ≤ 2.5 AND ≥10 ratings\n"
                "  - mixed (褒贬不一) — anything in between\n"
                "  - insufficient_data (样本不足) — fewer than 5 ratings\n"
                "  - unrated (暂无评分) — not on RMP\n"
                "DB-first (local RMP snapshot), falls back to Anteater for "
                "instructors without RMP coverage. Use the tier label when "
                "summarizing — don't restate the raw number unless asked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instructor_name": {
                        "type": "string",
                        "description": (
                            "'LASTNAME, F.' (the form sections.csv uses), "
                            "'First Last', or a ucinetid like 'thornton'."
                        ),
                    },
                    "department": {
                        "type": "string",
                        "description": (
                            "UCI dept code from the course the instructor "
                            "teaches ('COMPSCI', 'BIO SCI', 'PUBHLTH'). "
                            "ALWAYS pass this when you know the course "
                            "context — common surnames like 'LEE, J.' "
                            "won't resolve without it."
                        ),
                    },
                },
                "required": ["instructor_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_professor_reviews",
            "description": (
                "Top student reviews (comments + per-review ratings + grade) "
                "for an instructor, optionally filtered to a specific course. "
                "Sorted by community thumbs-up then recency. Use this when the "
                "student wants qualitative detail beyond the tier — what's it "
                "like to take this professor, are exams fair, etc. Local-only; "
                "missing instructors return found=false."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instructor_name": {
                        "type": "string",
                        "description": "'LASTNAME, F.', 'First Last', or ucinetid.",
                    },
                    "course": {
                        "type": "string",
                        "description": (
                            "Optional course filter — pass '122A' or 'CS122A' "
                            "to restrict to that course's reviews."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of reviews to return (1-20, default 5).",
                    },
                    "department": {
                        "type": "string",
                        "description": (
                            "UCI dept code from the course context — "
                            "pass when known to disambiguate common surnames."
                        ),
                    },
                },
                "required": ["instructor_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summarize_professor_reviews",
            "description": (
                "LLM-distilled structured summary of an instructor's "
                "student reviews — strengths/weaknesses, best_for/"
                "avoid_if descriptions, workload, exam_style, grading_"
                "style, teaching_style. Use this when you need to "
                "CHARACTERIZE a professor in depth (口碑 / 风格 / "
                "适合什么样的学生), NOT for short factual queries "
                "where the tier label from get_professor_rating "
                "suffices. Cache-first per (instructor, course) — first "
                "call costs ~1s, repeats are free. Returns English "
                "fields regardless of conversation language; translate "
                "as needed when quoting to the student."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instructor_name": {
                        "type": "string",
                        "description": "'LASTNAME, F.', 'First Last', or ucinetid.",
                    },
                    "course": {
                        "type": "string",
                        "description": (
                            "Optional course filter ('122A', 'CS122A'). "
                            "Pass it when the student is asking about a "
                            "specific class; omit for an all-courses "
                            "career summary."
                        ),
                    },
                    "department": {
                        "type": "string",
                        "description": (
                            "UCI dept code from the course context — "
                            "pass when known to disambiguate common surnames."
                        ),
                    },
                },
                "required": ["instructor_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_professor_tags",
            "description": (
                "Aggregated RMP tags students applied to this instructor "
                "(e.g. 'Tough grader', 'Caring', 'Test heavy', 'Amazing "
                "lectures') with counts. Cheaper than fetching full reviews "
                "when you only need a high-level vibe check."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instructor_name": {
                        "type": "string",
                        "description": "'LASTNAME, F.', 'First Last', or ucinetid.",
                    },
                    "course": {
                        "type": "string",
                        "description": "Optional course filter, e.g. '122A'.",
                    },
                    "department": {
                        "type": "string",
                        "description": (
                            "UCI dept code from the course context — "
                            "pass when known to disambiguate common surnames."
                        ),
                    },
                },
                "required": ["instructor_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_prerequisites_met",
            "description": (
                "Check whether the student satisfies a course's "
                "prerequisites. Uses the student's recorded completed "
                "+ in-progress courses automatically — don't pass them "
                "unless you want to test a hypothetical scenario. "
                "Note: v1 treats prereqs as a flat list (AND), not an "
                "OR-tree, so 'missing' may overstate the gap when the "
                "real requirement is 'A OR B'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "course_id": {"type": "string"},
                    "completed_courses": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional override; omit to use the student's actual completed list.",
                    },
                    "in_progress_courses": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional override; omit to use the student's actual current enrollment.",
                    },
                },
                "required": ["course_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_courses",
            "description": (
                "List courses offered in a specific term, optionally "
                "filtered by department or GE category. Returns "
                "{course_id} entries only — call get_course_info on "
                "any candidate to get title/units/description. "
                "DB-only (no API fallback) — Anteater has no "
                "multi-criteria search endpoint."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {
                        "type": "string",
                        "description": "Required. e.g. 'Spring 2026'.",
                    },
                    "department": {
                        "type": "string",
                        "description": "Canonical UCI department code: 'COMPSCI', 'I&C SCI', 'MATH', 'STATS'.",
                    },
                    "ge_category": {
                        "type": "string",
                        "description": "GE category like 'III', 'IV', 'VII'.",
                    },
                    "exclude_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Course IDs to filter out (e.g. courses the student already took).",
                    },
                },
                "required": ["term"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_section_conflict",
            "description": (
                "For every section pair of course_a × course_b in the "
                "given term, report whether their meeting times "
                "overlap. any_compatible_combination=true means the "
                "student CAN take both, just has to pick the right "
                "sections."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "course_a": {"type": "string"},
                    "course_b": {"type": "string"},
                    "term": {
                        "type": "string",
                        "description": "Required. e.g. 'Spring 2026'.",
                    },
                },
                "required": ["course_a", "course_b", "term"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_student_profile",
            "description": (
                "Full persistent profile for the current student. "
                "Identity basics (major, year, completed, currently "
                "enrolled, term) are already in your system context, "
                "but call this when you need anything else recorded "
                "in their profile, plus their preferences."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_policy",
            "description": (
                "Look up an official UCI academic policy. Use this "
                "when the student's question depends on institutional "
                "rules (unit caps, graduation requirements, P/NP "
                "limits, quarter dates, etc.) rather than course data. "
                "Returns the policy data plus a source URL to cite. "
                "Call with no topic to list available topics."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "enum": [
                            # Enrollment / units
                            "unit_limits",
                            "course_value",
                            "enrollment_rules",
                            # Degree
                            "degree_requirements",
                            "class_level",
                            "residence",
                            "general_education",
                            "elwr",
                            "major_gpa",
                            # Grading / academic standing
                            "grading",
                            "incomplete_grade",
                            "pass_no_pass",
                            "academic_notice",
                            "honors",
                            # Schedule changes
                            "add_drop",
                            "enrollment_responsibility",
                            "webreg",
                            "co_classes",
                            "restriction_codes",
                            "student_status_loss",
                            "withdrawal",
                            "finals",
                            # Discipline
                            "academic_integrity",
                            # Reference
                            "academic_calendar",
                            "sources",
                        ],
                        "description": (
                            "Pick the topic most directly relevant to the student's "
                            "question. Topics map to UCI Senate Manual sections + "
                            "catalogue rules:\n"
                            "ENROLLMENT — unit_limits (per-quarter min/max, career cap, "
                            "summer/ICS petition limits), course_value (1 course = 4 units; "
                            "credit by exam; upper-div credit rules), enrollment_rules "
                            "(Lec+Dis/Lab pairing requirement on WebReg, late-secondary-add).\n"
                            "DEGREE — degree_requirements (180 / 2.0), class_level "
                            "(freshman/.../senior unit thresholds), residence "
                            "(36 of final 45 at UCI), general_education (categories I–VIII), "
                            "elwr (Entry Level Writing — 3-quarter deadline), major_gpa "
                            "(2.0 in major + upper-div major, denial-of-major rules).\n"
                            "GRADING — grading (A+ to F, P/NP, S/U, repetition rules), "
                            "incomplete_grade (I and IP timelines, auto-conversion to F), "
                            "pass_no_pass (4-unit avg/qtr, eligibility, major rules), "
                            "academic_notice (probation triggers, disqualification, appeals), "
                            "honors (quarterly 12 units + 3.5; graduation 16% cap with "
                            "Latin breakdown).\n"
                            "SCHEDULE — add_drop (week 1-2 free in WebReg / 3-6 via "
                            "Enrollment Exceptions w/ dean approval / after 6 = W), "
                            "enrollment_responsibility (student's duty to verify "
                            "enrollment, drop classes they've stopped attending), "
                            "webreg (system hours, 48-hr priority window, holds, "
                            "$50 late fee, status column meanings), "
                            "co_classes (Lec+Dis same-session, tentative enroll, "
                            "auto-drop on logout, switching procedure), "
                            "restriction_codes (A-X: prereq/auth code/class level/"
                            "major/grading restrictions on the SOC 'Rstr' column), "
                            "student_status_loss (Friday 5pm week-3 cutoff, "
                            "consequences, reinstatement paths), "
                            "withdrawal (honorable vs unauthorized → F), "
                            "finals (3-hour cap, syllabus rule, async window).\n"
                            "DISCIPLINE — academic_integrity (4 violation types, 5 "
                            "sanctions, 30/10/10-day timelines, appeal paths).\n"
                            "REFERENCE — academic_calendar (quarter begin/end), "
                            "sources (URLs for citation)."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_recommendation",
            "description": (
                "REQUIRED when recommending a SET of courses for a specific term "
                "(e.g. a term schedule, a list of electives to consider). Stages "
                "a structured course-card list that the frontend renders as "
                "click-to-add cards next to your prose. Call this AT LEAST ONCE "
                "per recommendation turn — the prose alone won't surface cards. "
                "Backend enriches each item with catalog title / units / "
                "sections / grades / prereq status, so you only pass IDs + "
                "category + rationale.\n"
                "HARD RULE: courses that have no sections in the target term "
                "get DROPPED (UCI enrollment is by 5-digit registrar code which "
                "is term-scoped — no code = nothing for the student to type into "
                "WebReg). Dropped courses come back in the return value's "
                "`skipped` field; check it and call this tool again with "
                "replacements if the slate is short.\n"
                "Do NOT call this for single-course questions, professor "
                "questions, or pure information lookups."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "description": "Courses to stage as cards, in display order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "course_id": {
                                    "type": "string",
                                    "description": "Course code in any common form (CS143A, ICS33, MATH2B).",
                                },
                                "category": {
                                    "type": "string",
                                    "enum": ["core", "practical", "career", "advanced", "elective"],
                                    "description": (
                                        "Visual category driving the card's color stripe. "
                                        "core = major-required; practical = project/lab/portfolio; "
                                        "career = internship/interview/industry-relevant; "
                                        "advanced = upper-div elective with prestige; "
                                        "elective = open elective / GE / breadth."
                                    ),
                                },
                                "priority": {
                                    "type": "string",
                                    "enum": ["high", "medium", "low"],
                                    "description": "Recommendation strength badge. Default medium.",
                                },
                                "reason": {
                                    "type": "string",
                                    "maxLength": 80,
                                    "description": (
                                        "ONE SHORT PHRASE — at most ~10 words / 80 chars. "
                                        "Designed to fit on a card next to the course title. "
                                        "Good: 'CSE core, OS principles' / 'easy GE-IV filler' / "
                                        "'algorithms — interview prep'. "
                                        "BAD: 'Linear Algebra — foundational for CSE; essential "
                                        "for computer graphics, ML, and upper-div systems courses. "
                                        "Lec A (Lu) has 210 seats open...' (too long; section "
                                        "details are in the sub-card). "
                                        "Write nouns and short phrases, NOT full sentences. "
                                        "Backend hard-truncates at 80 chars."
                                    ),
                                },
                            },
                            "required": ["course_id", "category", "reason"],
                        },
                    },
                    "term": {
                        "type": "string",
                        "description": (
                            "Term the recommendation targets, e.g. 'Fall 2026'. "
                            "If omitted, the session's selected term is used."
                        ),
                    },
                },
                "required": ["items"],
            },
        },
    },
]


# ══════════════════════════════════════════════════════════
#  Dispatchers — thin wrappers around db.py
# ══════════════════════════════════════════════════════════

def _tool_get_course_info(course_id: str) -> dict:
    return db.get_course_info(course_id)


def _tool_get_sections(
    course_id: str,
    *,
    context: dict,
    term: Optional[str] = None,
) -> dict:
    # Safety net: if the model forgot to pass term, fall back to the
    # session's selected term rather than returning unscoped data.
    effective_term = term or context.get("term")
    if not effective_term:
        return {"found": False, "source": "none", "sections": [],
                "reason": "term is required and no term was provided"}
    return db.get_sections(course_id, effective_term)


def _tool_get_grade_distribution(course_id: str) -> dict:
    return db.get_grade_distribution(course_id)


def _tool_get_professor_rating(
    instructor_name: str,
    department: Optional[str] = None,
) -> dict:
    return db.get_professor_rating(instructor_name, department=department)


def _tool_get_professor_reviews(
    instructor_name: str,
    course: Optional[str] = None,
    limit: int = 5,
    department: Optional[str] = None,
) -> dict:
    return db.get_professor_reviews(
        instructor_name, course=course, limit=limit, department=department,
    )


def _tool_get_professor_tags(
    instructor_name: str,
    course: Optional[str] = None,
    department: Optional[str] = None,
) -> dict:
    return db.get_professor_tags(instructor_name, course=course, department=department)


# Async tool: dispatch returns the coroutine; the agent loop awaits it.
def _tool_summarize_professor_reviews(
    instructor_name: str,
    course: Optional[str] = None,
    department: Optional[str] = None,
):
    return db.get_professor_summary(
        instructor_name, course=course, department=department,
    )


def _tool_check_prerequisites_met(
    course_id: str,
    *,
    context: dict,
    completed_courses: Optional[list[str]] = None,
    in_progress_courses: Optional[list[str]] = None,
) -> dict:
    # Default to the student's actual lists if the model didn't pass them.
    if completed_courses is None or in_progress_courses is None:
        prof = db.get_student_profile(context.get("user_id", ""))
        if prof.get("found"):
            p = prof["profile"]
            if completed_courses is None:
                completed_courses = p.get("completed_courses", [])
            if in_progress_courses is None:
                in_progress_courses = p.get("selected_courses", [])
    return db.check_prerequisites_met(
        course_id,
        completed_courses=completed_courses or [],
        in_progress_courses=in_progress_courses or [],
    )


def _tool_search_courses(
    *,
    context: dict,
    term: Optional[str] = None,
    department: Optional[str] = None,
    ge_category: Optional[str] = None,
    exclude_ids: Optional[list[str]] = None,
) -> dict:
    effective_term = term or context.get("term")
    if not effective_term:
        return {"found": False, "source": "none", "courses": [],
                "reason": "term is required and no term was provided"}
    return db.search_courses(
        term=effective_term,
        department=department,
        ge_category=ge_category,
        exclude_ids=exclude_ids,
    )


# ── Section conflict: real time-overlap detection ────────


def _tool_check_section_conflict(
    course_a: str,
    course_b: str,
    *,
    context: dict,
    term: Optional[str] = None,
) -> dict:
    effective_term = term or context.get("term")
    if not effective_term:
        return {"found": False, "source": "none",
                "reason": "term is required and no term was provided"}

    a = db.get_sections(course_a, effective_term)
    b = db.get_sections(course_b, effective_term)
    if not a.get("found") or not a.get("sections"):
        return {"found": False, "source": a.get("source", "none"),
                "reason": a.get("reason", f"no sections for {course_a}")}
    if not b.get("found") or not b.get("sections"):
        return {"found": False, "source": b.get("source", "none"),
                "reason": b.get("reason", f"no sections for {course_b}")}

    pairs: list[dict] = []
    any_compatible = False
    for sa in a["sections"]:
        for sb in b["sections"]:
            overlap = sections_overlap(sa, sb)
            if not overlap:
                any_compatible = True
            pairs.append({
                "a_section": sa.get("section_code"),
                "a_meeting": sa.get("time_display"),
                "b_section": sb.get("section_code"),
                "b_meeting": sb.get("time_display"),
                "overlap":   overlap,
            })
    return {
        "found": True,
        "source": "computed",
        "term":   effective_term,
        "course_a": course_a,
        "course_b": course_b,
        "pairs":  pairs,
        "any_compatible_combination": any_compatible,
    }


# ── Policy lookup ────────────────────────────────────────

def _tool_get_policy(topic: Optional[str] = None) -> dict:
    """
    Return UCI policy data by topic. Topics map to constants in
    app.data.policies. Each response includes the source URL so the
    LLM can cite where the rule came from.

    Calling with no topic returns the list of available topics so the
    model can browse without guessing.
    """
    from app.data import policies as p

    # Topic → (data, source_key).
    # source_key matches a key in p.SOURCES so we can return a URL the
    # LLM can cite. Topics here come from the catalogue + Senate Manual
    # — see app/data/policies.py for the underlying constants.
    registry: dict[str, tuple[object, str]] = {
        # Enrollment / units
        "unit_limits":          (p.UNIT_LIMITS,         "academic_regulations"),
        "course_value":         (p.COURSE_VALUE,        "senate_manual"),
        "enrollment_rules":     (p.ENROLLMENT_RULES,    "registration"),

        # Degree
        "degree_requirements":  (p.DEGREE_REQUIREMENTS, "bachelor_requirements"),
        "class_level":          (p.CLASS_LEVEL,         "senate_manual"),
        "residence":            (p.RESIDENCE,           "senate_manual"),
        "general_education":    (p.GENERAL_EDUCATION,   "bachelor_requirements"),
        "elwr":                 (p.ELWR,                "senate_manual"),
        "major_gpa":            (p.MAJOR_GPA,           "senate_manual"),

        # Grading / academic standing
        "grading":              (p.GRADING,             "senate_manual"),
        "incomplete_grade":     (p.INCOMPLETE_GRADE,    "senate_manual"),
        "pass_no_pass":         (p.PASS_NO_PASS,        "senate_manual"),
        "academic_notice":      (p.ACADEMIC_NOTICE,     "senate_manual"),
        "honors":               (p.HONORS,              "senate_manual"),

        # Schedule changes
        "add_drop":                  (p.ADD_DROP,                "registration"),
        "enrollment_responsibility": (p.ENROLLMENT_RESPONSIBILITY, "registration"),
        "webreg":                    (p.WEBREG,                  "webreg"),
        "co_classes":                (p.CO_CLASSES,              "co_classes"),
        "restriction_codes":         (p.RESTRICTION_CODES,       "restriction_codes"),
        "student_status_loss":       (p.STUDENT_STATUS_LOSS,     "student_status"),
        "withdrawal":                (p.WITHDRAWAL,              "senate_manual"),
        "finals":                    (p.FINALS,                  "senate_manual"),

        # Discipline
        "academic_integrity":   (p.ACADEMIC_INTEGRITY,  "academic_integrity"),

        # Reference
        "academic_calendar":    (p.ACADEMIC_CALENDAR,   "academic_calendar"),
        "sources":              (p.SOURCES,             "registrar_calendar"),
    }

    if not topic:
        return {
            "available_topics": sorted(registry.keys()),
            "hint": "Call again with topic=<name> to fetch that policy.",
        }

    if topic not in registry:
        return {
            "found": False,
            "reason": f"unknown policy topic {topic!r}",
            "available_topics": sorted(registry.keys()),
        }

    data, source_key = registry[topic]
    return {
        "found": True,
        "topic": topic,
        "data": data,
        "source": source_key,
        "source_url": p.SOURCES.get(source_key),
        "verified_at": "2026-05-29",
    }


def _tool_get_student_profile(*, context: dict) -> dict:
    user_id = context.get("user_id", "")
    result = db.get_student_profile(user_id)
    # Decorate with preferences from the memory layer (db doesn't
    # expose them directly because they're not catalog data).
    if result.get("found"):
        try:
            from app.memory import get_memory_manager
            mem = get_memory_manager()
            result["preferences"] = mem.get_preferences(user_id)
        except Exception as e:
            logger.debug("preferences enrichment skipped: %s", e)
    return result


def _tool_propose_recommendation(
    items: list[dict],
    *,
    context: dict,
    term: Optional[str] = None,
) -> dict:
    """
    Stage a structured set of course cards for the frontend.

    LLM passes a small ranked list of {course_id, category, reason}; we
    enrich each one with catalog title/units/sections/grades/prereq
    using the existing db.* helpers, stash the enriched payload on the
    tool context (read by the agent loop), and return a short ack to
    the LLM so it knows what it just staged.

    Schema-side validation (minItems/maxItems/enum/required) already
    runs at the LLM tool-call layer; here we re-cap defensively because
    schema enforcement isn't bulletproof across model versions.

    Cards' field names mirror what the existing renderCard() in
    static/index.html consumes — that's intentional, so the legacy and
    new paths share one rendering surface.
    """
    effective_term = term or context.get("term")
    if not isinstance(items, list) or not items:
        return {"ok": False, "reason": "items list was empty"}
    if not effective_term:
        # Section codes are term-scoped. Without a term we can't even
        # validate that the proposed courses are offerable, so refuse
        # outright — better than staging cards the student can't enroll
        # in.
        return {"ok": False,
                "reason": "term is required (no session term and no `term` arg)"}

    # ── HARD RULE: don't stage cards for a term whose add-window has
    # already closed (today is past Week 6 / late_add_drop_end). The
    # student can't realistically enroll into the recommendation, so
    # we refuse and signal the LLM to pivot to the next available term.
    from app.data import policies as _policies
    expired = _policies.is_term_past_deadline(
        effective_term, deadline_key="late_add_drop_end",
    )
    if expired is True:
        cal = _policies.get_term_calendar(effective_term) or {}
        return {
            "ok": False,
            "reason": (
                f"{effective_term}'s late add/drop window already closed on "
                f"{cal.get('late_add_drop_end', 'an earlier date')}. The "
                f"registrar will not accept new adds for this term. "
                f"Pivot to the next term and call propose_recommendation again."
            ),
            "term": effective_term,
            "deadline_passed": cal.get("late_add_drop_end"),
            "pivot_hint": "Use propose_recommendation with term=<next available quarter>.",
        }
    # expired is None (unknown term — Summer, mistyped, etc.) → allow.
    # Better to stage cards with possibly-stale dates than to silently
    # refuse and confuse the user. The student's term selector is the
    # source of truth for whether they can actually enroll.

    # Student profile drives the prereq check. If we can't read it we
    # don't fail the whole call — just skip the prereq enrichment.
    prof_resp = db.get_student_profile(context.get("user_id", ""))
    profile = (prof_resp.get("profile") if prof_resp.get("found") else {}) or {}
    completed = profile.get("completed_courses", []) or []
    in_progress = profile.get("selected_courses", []) or []
    student_units = _estimate_student_units(profile.get("year"))

    cards: list[dict] = []
    skipped: list[dict] = []     # ack-only; LLM sees this so it knows what fell off

    # Cached per-call — same for every card in this batch since they
    # share `effective_term`. Cards get a copy attached so the frontend
    # can render the "Drop without W: X · Withdraw: Y" footer without
    # a second lookup.
    term_calendar = _policies.get_term_calendar(effective_term) or {}
    term_deadlines = {
        "free_window_end":    term_calendar.get("free_window_end"),
        "late_add_drop_end":  term_calendar.get("late_add_drop_end"),
        "change_grading_end": term_calendar.get("change_grading_end"),
        "finals_begin":       term_calendar.get("finals_begin"),
    } if term_calendar else None

    for it in items[:8]:                       # schema caps at 8; mirror it
        cid_raw = (it.get("course_id") or "").strip()
        if not cid_raw:
            continue

        info = db.get_course_info(cid_raw)
        if not info.get("found"):
            # Unknown course — drop entirely. Cards can't be "selected"
            # if they don't exist; surfacing a stub would only confuse
            # the user. The LLM gets to know via the skipped list.
            skipped.append({
                "course_id": cid_raw,
                "reason": f"not in catalog: {info.get('reason', 'unknown')}",
            })
            continue

        course = info["course"]
        cid_canon = course.get("course_id") or cid_raw

        # ── Section codes (HARD RULE) ─────────────────────
        # The 5-digit registrar code is what students input to enroll;
        # different terms have different codes for the same course
        # (Spring '26 ICS 6B ≠ Fall '26 ICS 6B). A recommendation card
        # WITHOUT a code is unusable — drop it.
        sec_resp = db.get_sections(cid_canon, effective_term)
        sections = sec_resp.get("sections", []) if sec_resp.get("found") else []

        # "Active" = section actually exists. From here we apply two
        # progressively stricter filters:
        #   1) bookable: not cancelled, has a section_code
        #   2) enrollable: ALSO not FULL, AND the student's class level
        #      isn't excluded by a Rstr code (see RESTRICTION_CODES).
        # If no enrollable section remains, the card is dropped with a
        # specific skipped reason so the LLM can swap in a replacement.
        bookable = [s for s in sections
                    if not s.get("is_cancelled") and s.get("section_code")]
        if not bookable:
            skipped.append({
                "course_id": cid_canon,
                "reason": f"no sections in {effective_term}",
            })
            continue

        enrollable = [s for s in bookable if _section_enrollable(s, student_units)]
        if not enrollable:
            # Diagnose why everything was filtered out so the LLM can
            # explain it to the user (and pick a different course).
            why = _diagnose_unenrollable(bookable, student_units)
            skipped.append({
                "course_id": cid_canon,
                "reason": f"no enrollable section in {effective_term}: {why}",
            })
            continue

        # All subsequent classification operates on the ENROLLABLE set
        # — the user only gets credit for things they can actually add.
        active = enrollable

        # Classify sections into primary vs secondary. UCI WebReg
        # treats Lec (and standalone Sem) as primary enrollment
        # surfaces; Dis / Lab / Stu / Act / Tut are "secondary" sections
        # that must be paired with a primary on most courses. The
        # registrar rejects schedules that have only one half of the
        # pair — see ENROLLMENT_RULES in policies.py.
        primaries, secondaries = _split_primary_secondary(active)
        # If there's no primary at all (rare: studio-only courses,
        # some labs), treat all sections as primary so the student
        # at least gets a code.
        if not primaries:
            primaries, secondaries = active, []

        # If the course originally had Lec+Dis pairing but every
        # secondary got filtered out (e.g. all Dis sections full), the
        # registrar rejects the Lec enrollment — drop the card.
        orig_primaries, orig_secondaries = _split_primary_secondary(bookable)
        if orig_primaries and orig_secondaries and not secondaries:
            skipped.append({
                "course_id": cid_canon,
                "reason": (f"all Dis/Lab sections of {cid_canon} are FULL — "
                           f"per registrar, the Lec becomes unenrollable when "
                           f"every secondary is full"),
            })
            continue

        primary = next(
            (s for s in primaries if (s.get("section_type") or "").lower().startswith("lec")),
            primaries[0],
        )
        primary_code = primary.get("section_code")
        # Reorder so primary comes first — the frontend treats
        # enrollment_codes[0] as the headline chip.
        ordered = [primary] + [s for s in primaries if s is not primary] + secondaries
        enrollment_codes = [{
            "code":        s.get("section_code"),
            "type":        s.get("section_type"),
            "days":        s.get("days"),
            "time":        s.get("time_display") or _format_section_time(s),
            "instructors": s.get("instructors") or [],
            "seats_open":  s.get("seats_open"),
            "status":      s.get("status"),
        } for s in ordered]

        # Secondary-required signal: this is the hard rule. When a
        # course has BOTH primary and secondary sections, the student
        # must pick one of each. Surface the bookable secondary codes
        # so the frontend can show "+ Dis required (34251 / 34252 / ...)".
        requires_secondary = bool(secondaries) and bool(primaries)
        secondary_type = None
        secondary_codes_list: list[dict] = []
        if requires_secondary:
            # Take the most-common type letter so the card label is
            # specific ("+ Dis required" vs "+ Lab required") rather
            # than the generic "secondary".
            type_counts: dict[str, int] = {}
            for s in secondaries:
                st = (s.get("section_type") or "").strip()
                type_counts[st] = type_counts.get(st, 0) + 1
            secondary_type = max(type_counts, key=type_counts.get) if type_counts else None
            secondary_codes_list = [{
                "code":        s.get("section_code"),
                "type":        s.get("section_type"),
                "days":        s.get("days"),
                "time":        s.get("time_display") or _format_section_time(s),
                "instructors": s.get("instructors") or [],
                "seats_open":  s.get("seats_open"),
            } for s in secondaries]

        # Prereq status — silent fail returns "met" (don't flag false-
        # positive missing prereqs just because the lookup failed).
        prereq = db.check_prerequisites_met(
            cid_canon,
            completed_courses=completed,
            in_progress_courses=in_progress,
        )
        if prereq.get("found"):
            prereq_met     = prereq.get("met", True)
            prereq_missing = prereq.get("missing", []) or []
        else:
            prereq_met, prereq_missing = True, []

        grade_resp = db.get_grade_distribution(cid_canon)
        grade_dist = grade_resp.get("grades") if grade_resp.get("found") else None

        # Restriction chips: non-blocking warnings about WHAT it takes
        # to enroll (auth code from instructor / major restriction /
        # forced grading basis / course fee). Aggregated from the
        # primary's restriction string. Class-level codes are NOT
        # surfaced as chips — they're either honored (allowed →
        # don't show) or excluded (drop card → never reaches chips).
        restriction_chips = _restriction_chips_for_section(primary)

        # Section groups: full Lec-by-Lec layout so the card can show
        # an expandable sub-card panel with all enrollable Lec options
        # and their paired Dis/Lab sections. UCI's registrar groups by
        # section-LETTER (Lec A ↔ Dis A1/A2/A3 ↔ Lab A1/...). One
        # course with 3 Lec sections × different professors renders
        # as 3 sub-cards.
        #
        # Before building the groups, batch-RMP every unique non-STAFF
        # instructor (~3 lookups for a typical 1-Lec + several-Dis
        # course; ~6-10 for a multi-Lec writing course). One round-trip
        # to the prof index per name; results cached by db.py so
        # re-staging the same course is cheap.
        unique_instructors = _gather_unique_instructors(primaries, secondaries)
        rmp_by_instructor = _rmp_lookup_map(
            unique_instructors,
            department=course.get("department"),
        )
        section_groups = _group_sections_by_letter(
            primaries, secondaries, rmp_by_instructor=rmp_by_instructor,
        )

        cards.append({
            "course_id":   cid_canon,
            "title":       course.get("title"),
            "units":       course.get("units"),
            "department":  course.get("department"),
            "ge_category": course.get("ge_category"),
            "category":    it.get("category", "elective"),
            "priority":    it.get("priority", "medium"),
            "reason":      _truncate_reason(it.get("reason")),
            "term":        effective_term,
            "found":       True,
            "prereq_met":     prereq_met,
            "prereq_missing": prereq_missing,
            "primary_code":         primary_code,         # e.g. "35640" (Lec)
            "primary_type":         primary.get("section_type"),
            "requires_secondary":   requires_secondary,   # must pair Lec + Dis/Lab on WebReg
            "secondary_type":       secondary_type,       # "Dis" / "Lab" / "Stu" / etc.
            "secondary_codes":      secondary_codes_list, # list of bookable secondaries
            "enrollment_codes":     enrollment_codes,     # all bookable sections (legacy)
            "sections":             sections,             # full payload for future use
            "grade_distribution":   grade_dist,
            "term_deadlines":       term_deadlines,       # for the card footer
            "restriction_chips":    restriction_chips,    # [{code, label, tooltip}, ...]
            "primary_restrictions": primary.get("restrictions"),  # raw "AB" / "EJL"
            "section_groups":       section_groups,       # [{letter, primary, secondaries}, ...]
        })

    pending_sections = resolve_pending_schedule_sections(
        context.get("pending_schedule") or [],
        term=effective_term,
        section_lookup=db.get_sections,
    )
    schedule_validation = validate_schedule_bundle(
        cards,
        pending_sections=pending_sections,
    )
    for card in cards:
        card["schedule_validation"] = schedule_validation

    # Side channel: the agent loop reads this AFTER dispatch and emits
    # a `cards_proposed` event for the SSE consumer. We don't put
    # cards in the LLM-visible return value because (a) it'd waste a
    # lot of tokens and (b) the LLM doesn't need the enriched data —
    # it only needs to know its proposal landed.
    context["_proposed_cards"] = cards

    # Cards that need Lec + Dis/Lab pairing — surface so the LLM can
    # mention this in its prose (e.g. "CS 161 needs a Lec + a Dis").
    paired = [{
        "course_id":      c["course_id"],
        "primary_code":   c.get("primary_code"),
        "secondary_type": c.get("secondary_type"),
    } for c in cards if c.get("requires_secondary")]

    return {
        "ok": True,
        "staged_count": len(cards),
        "skipped_count": len(skipped),
        "skipped": skipped,
        "term": effective_term,
        "course_ids": [c["course_id"] for c in cards],
        "primary_codes": [c.get("primary_code") for c in cards],
        "requires_secondary": paired,   # [{course_id, primary_code, secondary_type}, ...]
        "schedule_validation": schedule_validation,
    }


def _format_section_time(s: dict) -> Optional[str]:
    """Compact 'MWF 10:00-10:50' fallback when the db layer didn't
    fill in time_display itself."""
    days = s.get("days")
    start = s.get("start_time")
    end = s.get("end_time")
    if days and start and end:
        return f"{days} {start}-{end}"
    if days and start:
        return f"{days} {start}"
    return None


_REASON_MAX_CHARS = 80    # hard cap matching the tool schema's maxLength

def _truncate_reason(reason: Optional[str]) -> str:
    """
    Trim a card's `reason` to one short phrase even if the LLM
    over-runs. We try clean cuts in this order:
      1) Already short enough — return as-is (stripped).
      2) End at the first sentence boundary (. ; — — etc.) within budget.
      3) End at the last whitespace within budget, then add an ellipsis.

    Backstop, not the primary length control — the schema's
    maxLength + the system prompt do most of the work.
    """
    if not reason:
        return ""
    r = reason.strip().replace("\n", " ").replace("  ", " ")
    if len(r) <= _REASON_MAX_CHARS:
        return r
    # Prefer a sentence break inside the budget.
    head = r[:_REASON_MAX_CHARS]
    for ch in (". ", "; ", " — ", " · "):
        i = head.rfind(ch)
        if i >= 30:                              # don't cut on the very first phrase
            return head[:i].rstrip()
    # Fall back to word boundary + ellipsis.
    i = head.rfind(" ")
    if i >= 30:
        head = head[:i]
    return head.rstrip(" ,;") + "…"


def _estimate_student_units(year: Optional[str]) -> Optional[float]:
    """
    Map the profile's `year` label ("Sophomore" / "Junior" / "Senior" /
    "Freshman" / "Graduate" / …) to a representative UCI unit count.
    Returns the midpoint of the class-level band per IR 380 + the
    Registrar's class-level restrictions (E/F/H/I). Grad / unknown
    returns None so the dispatcher's class-level filter goes off.

    We pick the midpoint rather than the lower bound so a student who's
    a "sophomore" right at the Lec-A cutoff (45 units) isn't tagged
    as freshman-eligible only.
    """
    if not year:
        return None
    y = year.strip().lower()
    if y.startswith(("fresh", "1st", "first")):    return 20.0
    if y.startswith(("soph", "2nd", "second")):    return 65.0
    if y.startswith(("jun",  "3rd", "third")):     return 110.0
    if y.startswith(("sen",  "4th", "fourth")):    return 155.0
    if y.startswith(("5th", "fifth")):             return 175.0
    if "grad" in y or "phd" in y or "master" in y or y == "g":
        return None    # graduate — UG class-level codes don't apply
    return None


def _section_allowed_for_class_level(restrictions: str,
                                     units: Optional[float]) -> bool:
    """
    False ONLY when one of this section's class-level restrictions
    (E/F/G/H/I/J in RESTRICTION_CODES["class_level_codes"]) excludes
    the student. Unknown units → assume allowed (don't false-positive).
    """
    if not restrictions or units is None:
        return True
    from app.data import policies as _policies
    code_table = _policies.RESTRICTION_CODES["class_level_codes"]
    for ch in restrictions:
        if ch in code_table:
            lo, hi = code_table[ch]
            if units < lo or (hi is not None and units >= hi):
                return False
    return True


def _section_enrollable(s: dict, units: Optional[float]) -> bool:
    """
    A section is 'enrollable' iff a student could meaningfully attempt
    to add it. That means:
      - has a bookable code
      - not cancelled
      - status != FULL (Waitl OK — student can join the waitlist)
      - class-level restrictions don't exclude the student
    """
    if not s.get("section_code") or s.get("is_cancelled"):
        return False
    if s.get("status") == "FULL":
        return False
    if not _section_allowed_for_class_level(s.get("restrictions") or "", units):
        return False
    return True


def _diagnose_unenrollable(bookable: list[dict],
                           units: Optional[float]) -> str:
    """Why are none of these sections enrollable? Aggregate the reasons
    so the LLM can explain it to the student in one sentence."""
    n = len(bookable)
    full = sum(1 for s in bookable if s.get("status") == "FULL")
    blocked = sum(1 for s in bookable
                  if not _section_allowed_for_class_level(
                      s.get("restrictions") or "", units))
    bits = []
    if full == n:
        bits.append(f"all {n} sections FULL")
    elif full:
        bits.append(f"{full}/{n} sections FULL")
    if blocked == n:
        bits.append(f"class-level restriction excludes the student "
                    f"(profile units ≈ {units})")
    elif blocked:
        bits.append(f"{blocked}/{n} sections have class-level restrictions")
    return "; ".join(bits) or "all sections filtered out"


# Card-level restriction chip labels for the frontend. Class-level
# codes (E/F/G/H/I/J) are intentionally excluded — those are already
# enforced by the hard filter; surfacing them as chips would just
# clutter cards that survived the filter.
_RESTRICTION_CHIP_LABELS = {
    "A": ("Prereq required",     "A prerequisite course must be completed before "
                                  "enrolling."),
    "B": ("Auth code required",  "Get a 4-digit authorization code from the "
                                  "instructor or department before enrolling."),
    "C": ("Course fee",          "An additional course fee is billed to your "
                                  "ZOTAccount, typically end of week 1."),
    "D": ("P/NP only",           "This course is offered Pass/Not Pass only."),
    "K": ("Graduate only",       "Restricted to graduate students."),
    "L": ("Major restricted",    "Open only to specific majors authorized by the "
                                  "department."),
    "M": ("Non-major only",      "Open only to students outside the offering "
                                  "department's major."),
    "N": ("School major only",   "Open only to majors in the offering school."),
    "O": ("Non-school major",    "Open only to students outside the offering "
                                  "school."),
    "R": ("S.O.M. P/F only",     "School of Medicine Pass/Fail course."),
    "S": ("S/U only",             "Course offers only Satisfactory/Unsatisfactory "
                                  "grading."),
    "X": ("Auth code per add",   "A unique authorization code is needed per "
                                  "transaction (add / drop / change)."),
}


def _restriction_chips_for_section(section: dict) -> list[dict]:
    """Decode a section's `restrictions` string into chip-friendly
    dicts the frontend can render directly. Class-level codes
    (E/F/G/H/I/J) are filtered out — those are honored upstream."""
    rstr = (section.get("restrictions") or "")
    out = []
    for ch in rstr:
        if ch in _RESTRICTION_CHIP_LABELS:
            label, tooltip = _RESTRICTION_CHIP_LABELS[ch]
            out.append({"code": ch, "label": label, "tooltip": tooltip})
    return out


# Section-type prefixes WebReg treats as "primary" (the surface a
# student adds directly). Lec is the typical case; Sem (seminar) is
# also a standalone primary for seminar-only courses.
_PRIMARY_PREFIXES = ("lec", "sem")
# Section-type prefixes treated as "secondary" — must pair with a
# primary on most courses (the registrar rejects schedules missing
# the pair). See policies.ENROLLMENT_RULES.
_SECONDARY_PREFIXES = ("dis", "lab", "stu", "act", "tut", "fld")


def _section_letter(s: dict) -> str:
    """
    Pull the section-LETTER (group key) out of a section dict.

    UCI's pairing convention from the registrar: "Lec A pairs with
    Dis A1/A2/A3; Lec B pairs with B1/B2/B3; …". The letter prefix
    of `sectionNum` is the grouping signal. We strip any digits and
    upper-case it. Sections with no `section_num` (CSV fallback) all
    collapse to a single "?" group so the frontend still has structure.
    """
    num = (s.get("section_num") or "").strip()
    if not num:
        return "?"
    # Take the leading non-digit prefix. "A" → "A"; "A1" → "A"; "C3" → "C";
    # weird payloads like "1A" → "?" (don't guess).
    out = []
    for ch in num:
        if ch.isalpha():
            out.append(ch.upper())
        else:
            break
    return "".join(out) or "?"


def _summarize_section(s: dict, rmp_by_instructor: Optional[dict] = None) -> dict:
    """Compact, JSON-friendly section payload for the frontend
    section-groups sub-card. Optionally attaches per-instructor RMP
    rating + a final-exam summary for primary sections — the sub-card
    needs these to render professor name + score + final-exam time
    + room.

    `rmp_by_instructor` is a pre-warmed cache mapping the raw
    Anteater instructor string ("THORNTON, A.") to its rating dict —
    we batch-build this once per propose_recommendation turn so we
    don't fan out hundreds of RMP lookups in a 6-card response.
    """
    instructors = s.get("instructors") or []
    ratings = []
    if rmp_by_instructor:
        for name in instructors:
            r = rmp_by_instructor.get(name)
            # The rating dict from db.get_professor_rating nests the
            # actual numbers under an "instructor" sub-dict; reading
            # `r["avg_rating"]` directly was returning None on every
            # call (the bug behind "no ratings showing on cards").
            inst = (r or {}).get("instructor") or {}
            tier = inst.get("tier") or {}
            if r and r.get("found") and inst.get("avg_rating") is not None:
                ratings.append({
                    "instructor":  name,
                    "avg_rating":  inst.get("avg_rating"),
                    "num_ratings": inst.get("num_ratings"),
                    "difficulty":  inst.get("avg_difficulty"),
                    "would_take_again_pct": inst.get("would_take_again_pct"),
                    "tier":        tier.get("tier"),         # slug — drives the frontend color class
                    "tier_label":  tier.get("label_en"),     # human-readable for the chip text
                })
            else:
                ratings.append({"instructor": name})

    return {
        "code":         s.get("section_code"),
        "num":          s.get("section_num"),
        "type":         s.get("section_type"),
        "days":         s.get("days"),
        "time":         s.get("time_display") or _format_section_time(s),
        "location":     s.get("location"),
        "instructors":  instructors,
        "ratings":      ratings,                            # [{instructor, avg_rating, num_ratings, tier}]
        "seats_open":   s.get("seats_open"),
        "max_capacity": s.get("max_capacity"),
        "status":       s.get("status"),
        "restrictions": s.get("restrictions"),
        "final_exam":   _summarize_final_exam(s.get("final_exam")),
    }


def _summarize_final_exam(fx: Optional[dict]) -> Optional[dict]:
    """Format Anteater's finalExam payload into a frontend-friendly
    {status, label, location} triple. Status is one of
    "scheduled" / "tba" / "none" / None (unknown)."""
    if not isinstance(fx, dict):
        return None
    code = (fx.get("examStatus") or "").upper()
    if code == "NO_FINAL":
        return {"status": "none", "label": "No final exam", "location": None}
    if code == "TBA_FINAL":
        return {"status": "tba", "label": "Final TBA", "location": None}
    if code != "SCHEDULED_FINAL":
        return None
    dow = fx.get("dayOfWeek")
    mon = fx.get("month")
    day = fx.get("day")
    st  = fx.get("startTime") or {}
    et  = fx.get("endTime") or {}
    bldg = fx.get("bldg") or []
    parts = []
    months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    if dow and mon and day:
        parts.append(f"{dow} {months[mon] if 1 <= mon <= 12 else mon} {day}")
    if st.get("hour") is not None and et.get("hour") is not None:
        parts.append(f"{st['hour']:02d}:{st.get('minute',0):02d}–"
                     f"{et['hour']:02d}:{et.get('minute',0):02d}")
    return {
        "status":   "scheduled",
        "label":    " · ".join(parts) if parts else "Scheduled",
        "location": ", ".join(bldg) if bldg else None,
    }


def _gather_unique_instructors(*section_lists: list[dict]) -> list[str]:
    """Across one or more section lists, return the dedup'd, ordered
    list of raw instructor strings (the form Anteater uses, "LAST, F.")
    we need to RMP-rate. Filters out "STAFF" / "TBA" / empty entries
    since those won't have ratings."""
    seen: set[str] = set()
    out: list[str] = []
    skip = {"staff", "tba", "to be announced", ""}
    for sections in section_lists:
        for s in sections:
            for raw in (s.get("instructors") or []):
                n = (raw or "").strip()
                if n.lower() in skip or n in seen:
                    continue
                seen.add(n)
                out.append(n)
    return out


def _rmp_lookup_map(instructor_names: list[str], department: Optional[str]) -> dict:
    """Batch RMP lookups for the unique instructor names. Returns
    `{raw_name: rating_dict}`. Misses fall back to a sentinel so
    `_summarize_section` doesn't keep retrying.

    `department` is the offering course's department — passed to the
    DB layer to narrow the prof match (Thornton appears in CHEM and
    CS; the rating endpoint disambiguates on dept)."""
    out: dict[str, dict] = {}
    for name in instructor_names:
        try:
            r = db.get_professor_rating(name, department=department)
        except Exception as e:
            logger.debug("RMP lookup for %r failed: %s", name, e)
            r = {"found": False}
        out[name] = r
    return out


def _group_sections_by_letter(primaries: list[dict],
                              secondaries: list[dict],
                              rmp_by_instructor: Optional[dict] = None) -> list[dict]:
    """
    Partition the enrollable sections into LEC-led groups.

    Each output entry is one Lec (primary) plus its paired Dis/Lab/etc.
    secondaries — courses like ICS SCI 139W that have 3 Lec sections
    × multiple discussions render as 3 groups.

    Edge cases:
      - A primary with no matching secondaries (e.g. Lec-only writing
        course): still emitted, secondaries=[].
      - Secondaries whose letter has no matching primary (e.g. all
        Lec A sections got filtered out but Dis A1 survived): grouped
        under a synthetic "?" letter with no primary. Frontend can
        hide these or surface them as orphans.
      - Only one Lec section (single-group course): still rendered as
        a group of 1 for consistency.
    """
    by_letter: dict[str, dict] = {}
    for p in primaries:
        letter = _section_letter(p)
        if letter not in by_letter:
            by_letter[letter] = {
                "letter":      letter,
                "primary":     _summarize_section(p, rmp_by_instructor),
                "secondaries": [],
            }
        else:
            # Two primaries share a letter — extremely rare (cross-listed
            # / co-taught Lec A). Keep the first; surface the duplicate
            # as a secondary so we don't drop data silently.
            by_letter[letter]["secondaries"].append(_summarize_section(p, rmp_by_instructor))

    # Attach each secondary to its letter group. Orphans (no matching
    # Lec) land in a synthetic group so the renderer can still show them.
    for s in secondaries:
        letter = _section_letter(s)
        if letter not in by_letter:
            by_letter[letter] = {
                "letter":      letter,
                "primary":     None,
                "secondaries": [],
            }
        # Secondaries usually have STAFF as instructor — pass the RMP
        # map anyway so any named instructor (Dis taught by a known
        # prof) still gets a rating.
        by_letter[letter]["secondaries"].append(_summarize_section(s, rmp_by_instructor))

    # Stable order: A, B, C, …, then "?" last so orphans don't lead.
    # Within each group, sort secondaries by their section_num so the
    # user sees Dis A1, A2, A3, … in natural order instead of WebSoc's
    # registrar-assigned order.
    def _sec_sort_key(s):
        n = s.get("num") or ""
        # Pull trailing digits so "A2" sorts before "A10".
        m = []
        digit = ""
        for ch in n:
            if ch.isdigit():
                digit += ch
            elif digit:
                m.append(digit); digit = ""
        if digit:
            m.append(digit)
        digit_val = int(m[-1]) if m else 0
        return (n[:1] if n else "", digit_val)
    for g in by_letter.values():
        g["secondaries"].sort(key=_sec_sort_key)
    def _sort_key(g):
        L = g["letter"]
        return (L == "?", L)
    return sorted(by_letter.values(), key=_sort_key)


def _split_primary_secondary(active_sections: list[dict]) -> tuple[list[dict], list[dict]]:
    """Partition a course's active sections into primary (Lec/Sem)
    vs secondary (Dis/Lab/Stu/...). Unknown section types fall into
    primary so the student isn't left without a code."""
    primaries, secondaries = [], []
    for s in active_sections:
        st = (s.get("section_type") or "").lower()
        if st.startswith(_PRIMARY_PREFIXES):
            primaries.append(s)
        elif st.startswith(_SECONDARY_PREFIXES):
            secondaries.append(s)
        else:
            primaries.append(s)
    return primaries, secondaries


DISPATCH: dict[str, Callable[..., dict]] = {
    "get_course_info":          _tool_get_course_info,
    "get_sections":             _tool_get_sections,
    "get_grade_distribution":   _tool_get_grade_distribution,
    "get_professor_rating":     _tool_get_professor_rating,
    "get_professor_reviews":    _tool_get_professor_reviews,
    "get_professor_tags":       _tool_get_professor_tags,
    "summarize_professor_reviews": _tool_summarize_professor_reviews,
    "check_prerequisites_met":  _tool_check_prerequisites_met,
    "search_courses":           _tool_search_courses,
    "check_section_conflict":   _tool_check_section_conflict,
    "get_student_profile":      _tool_get_student_profile,
    "get_policy":               _tool_get_policy,
    "propose_recommendation":   _tool_propose_recommendation,
}


# ══════════════════════════════════════════════════════════
#  Dispatch entry point
# ══════════════════════════════════════════════════════════

def dispatch(name: str, args: dict, *, context: dict):
    """Run a tool by name.

    Returns either a JSON-serializable dict (sync tools) or a
    coroutine that resolves to one (async tools like
    summarize_professor_reviews). The caller — typically the agent
    loop — is responsible for awaiting coroutine results.

    On synchronous failure, returns {"error": "..."} rather than
    raising. Async failures must be caught by the awaiter.
    """
    fn = DISPATCH.get(name)
    if not fn:
        return {"error": f"unknown tool: {name}"}
    try:
        sig = inspect.signature(fn)
        if "context" in sig.parameters:
            return fn(context=context, **(args or {}))
        return fn(**(args or {}))
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:
        logger.warning("tool %s failed: %s: %s", name, type(e).__name__, e)
        return {"error": f"{type(e).__name__}: {e}"}


def humanize_tool_call(name: str, args: dict) -> str:
    """Short user-facing label for a tool call (chip text)."""
    a = args or {}
    term_suffix = f" · {a['term']}" if a.get("term") else ""
    if name == "get_course_info":
        return f"查询 {a.get('course_id', '')} 课程信息"
    if name == "get_sections":
        return f"查询 {a.get('course_id', '')} 排课{term_suffix}"
    if name == "get_grade_distribution":
        return f"查询 {a.get('course_id', '')} 历年成绩"
    if name == "get_professor_rating":
        return f"查询教授 {a.get('instructor_name', '')}"
    if name == "get_professor_reviews":
        course = a.get("course")
        suffix = f" · {course}" if course else ""
        return f"读取学生评论 · {a.get('instructor_name', '')}{suffix}"
    if name == "get_professor_tags":
        return f"汇总教授标签 · {a.get('instructor_name', '')}"
    if name == "summarize_professor_reviews":
        course = a.get("course")
        suffix = f" · {course}" if course else ""
        return f"提炼教授口碑 · {a.get('instructor_name', '')}{suffix}"
    if name == "check_prerequisites_met":
        return f"检查 {a.get('course_id', '')} 先修要求"
    if name == "search_courses":
        bits = [v for v in (a.get("department"), a.get("ge_category")) if v]
        return f"搜索课程（{', '.join(bits) or '全部'}）{term_suffix}"
    if name == "check_section_conflict":
        return f"对比 {a.get('course_a', '')} 与 {a.get('course_b', '')} 时间冲突{term_suffix}"
    if name == "get_student_profile":
        return "读取学生画像"
    if name == "get_policy":
        topic = a.get("topic")
        return f"查询学校政策 · {topic}" if topic else "列出学校政策主题"
    if name == "propose_recommendation":
        n = len(a.get("items") or [])
        return f"准备 {n} 张推荐卡片{term_suffix}"
    return f"调用 {name}"
