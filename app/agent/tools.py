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
we inject the backend-resolved query term from the tool
context as a safety net so we never silently return data from the
wrong term.

`dispatch(name, args, context)` is the single entry point. `context`
carries per-request state: user_id + validated query scope.
"""

from __future__ import annotations

import inspect
import logging
import re
from copy import deepcopy
from typing import Any, Callable, Optional

from app import config, observability
from app.agent import grade_evidence
from app.agent import prefilter
from app.response_language import display_term
from app.agent.deep_search_state import DeepSearchRunState
from app.catalog.departments import resolve_department
from app.catalog.normalization import parse_course_mention
from app.data import db
from app.data import web_search as web_search_data
from app.data import websoc_workflow
from app.data.restriction_timeline import (
    RestrictionQuery,
    RestrictionType,
    build_restriction_evidence_bundle,
    build_verified_restriction_facts,
    compact_linked_restriction_result,
)
from app.data.restriction_notices import read_notice_restrictions
from app.terms import parse_term_key
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
            "name": "get_course_offerings",
            "description": (
                "Check whether a course was/is offered and its lecture professors across "
                "one or multiple query terms. Preferred for cross-term comparisons, "
                "offering patterns and future-term offering questions. Returns one row "
                "per term, distinguishing not_offered, unpublished and unavailable. "
                "Automatically checks official WebSoc on every local miss or stale "
                "snapshot, then the secondary API on official lookup failure. "
                "Only an authoritative official no-match establishes not_offered. "
                "For an unpublished future term, automatically checks the previous two "
                "same-season terms as historical reference and gives a possible-offering "
                "prediction; never predicts the future professor. Pass all query_terms."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "course_id": {"type": "string"},
                    "terms": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 12},
                },
                "required": ["course_id", "terms"],
            },
        },
    },
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
                "Also preserves all meetings and scoped comments (use comment_context_id "
                "to read comment_contexts), including former-code notices where present. "
                "course_code_normalizations identifies known department aliases mentioned "
                "in comments; the original comment establishes any course relationship. "
                "When local sections are missing or stale, this tool uses the fixed "
                "official Registrar WebSoc POST workflow for historical offering "
                "facts. `offering_status=not_offered` with `authoritative=true` "
                "is a definitive official no-match; do not follow it with "
                "web_search or a model-built WebSoc URL. "
                "ALWAYS pass `term` — never assume the term from "
                "context. Empty sections alone do not establish no offering: "
                "inspect offering_status and authoritative. Network or parse "
                "failures return unavailable, never not_offered."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "course_id": {"type": "string"},
                    "term": {
                        "type": "string",
                        "description": "Required canonical form: '2026 Spring', '2026 Fall', '2025 Spring'.",
                    },
                },
                "required": ["course_id", "term"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_live_sections",
            "description": (
                "Live UCI WebSoc availability for a course in a SPECIFIC "
                "term via Anteater API. Use this instead of get_sections "
                "when the student asks whether a course/section is OPEN, "
                "FULL, Waitl, has seats left, waitlist size/capacity, "
                "New Only Reserved/NOR seats, or current restriction codes. "
                "Also use for original WebSoc course comments, former-code and prior-credit "
                "notices even when no seat check is requested. "
                "Returns live status, enrolled/capacity, seats_open, "
                "waitlist, NOR, restrictions, updated_at, and retrieved_at. "
                "Includes full meetings, section_comment and scoped comment_contexts; "
                "course_code_normalizations supplies known aliases for comment mentions. "
                "cancellation_status=unknown is not confirmed active. "
                "ALWAYS pass `term`; use force_refresh=true only when the "
                "student explicitly asks for latest/now/refresh."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "course_id": {
                        "type": "string",
                        "description": "Course code in any common form: 'CS161', 'COMPSCI 161', 'ICS33'.",
                    },
                    "term": {
                        "type": "string",
                        "description": "Required canonical form: '2026 Spring', '2026 Fall', '2025 Spring'.",
                    },
                    "section_codes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional specific 5-digit WebSoc section codes to check.",
                    },
                    "force_refresh": {
                        "type": "boolean",
                        "description": "Bypass the 5-minute live WebSoc cache only when the user asks for the latest/current status.",
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
                "Anteater historical grade distribution for a course (A/B/C "
                "percentages + average GPA), aggregated across past offerings. "
                "This is course-wide evidence, not one professor's outcomes or "
                "the target term's grades. Keep it separate from Zotistics; "
                "overlapping records must not be added together. Use for "
                "historical grading/GPA questions; grades alone do not verify workload."
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
            "name": "get_zotistics_grades",
            "description": (
                "Zotistics historical grade outcomes for a course, an instructor, "
                "or a specific course-instructor pair. Pass at least one of "
                "course_id or instructor_name; when course context is known, "
                "pass both to avoid attributing a course-wide average to one professor. "
                "Returns historical GPA, letter-grade percentages, sample counts, "
                "coverage and source URL. Label source_reported_gpa separately from "
                "the approximate count-based avg_gpa (no +/-). pct_A uses A-F "
                "graded records; site_percentages include P/NP. samples and graded "
                "have different denominators. class_count counts offerings, not "
                "distinct terms; read terms for actual term coverage. Only verified "
                "course-instructor no_data permits a fallback to that instructor's "
                "all-course, all-department history, which must retain instructor "
                "scope. unavailable, not_found, ambiguous_instructor, invalid_query "
                "and cache_miss do not authorize that fallback or a grading conclusion. "
                "Use for grading/GPA impact, easy-class and worth-taking evaluations "
                "alongside RMP evidence for teaching experience. It does not identify "
                "future instructors or measure teaching quality/workload. Do not use "
                "for simple offering or cross-term instructor comparisons unless "
                "the student also asks about grades."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "course_id": {
                        "type": "string",
                        "description": "Course in a common form, e.g. 'ICS33' or 'COMPSCI 161'.",
                    },
                    "instructor_name": {
                        "type": "string",
                        "description": "Instructor name, e.g. 'THORNTON, A.'; unresolved or ambiguous matches are reported explicitly.",
                    },
                    "department": {
                        "type": "string",
                        "description": "UCI department code when known, especially for common instructor names.",
                    },
                },
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
                "  - unrated (暂无评分) — no usable rating in the returned source\n"
                "DB-first (local RMP snapshot), falls back to Anteater for "
                "instructors without local RMP coverage. Use the returned tier "
                "when assessing reputation, and cite avg_rating and num_ratings "
                "when present. Missing local coverage or an unrated tier does not "
                "prove the instructor has no RMP page; API catalog metadata alone "
                "is not an RMP rating."
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
                "cache miss may require an LLM call. A summary without a course "
                "filter covers the instructor's historical reviews across courses, "
                "not a single class. Unknown fields and empty theme lists mean "
                "insufficient review evidence. Returns English fields; translate "
                "as needed, preserving scope and uncertainty."
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
                "Returns status=met/not_met/unknown; unknown means the "
                "catalog or profile lacks enough information to verify. A met "
                "result that used in-progress courses is provisional future-term "
                "planning evidence, not proof those courses have passed or that "
                "registration is authorized. Respect the separate dynamic "
                "prefilter's verified-eligibility decision."
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
                    "allow_in_progress": {
                        "type": "boolean",
                        "description": "Whether current in-progress courses count provisionally for future-term planning. Defaults true; inspect in_progress_used/in_progress_policy and do not describe in-progress work as passed.",
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
                "Retrieve local catalog candidates for a specific term, optionally "
                "filtered by department or GE category. Returns available catalog "
                "metadata plus prefilter/ranking evidence when enabled; request "
                "get_course_info only for missing details needed for a decision. "
                "The local pool may be partial, and a candidate may have no verified "
                "target-term sections. An empty local result does not establish "
                "that no matching course is offered; verify serious candidates "
                "with target-term offering tools. Retrieval is DB-only. With both "
                "features off, legacy output is limited to the first 30 candidates. "
                "Fixed ranking is independent of dynamic prefilter: when enabled, "
                "the backend scores the complete matching pool before its output "
                "limit, using only grade caches and the local RMP snapshot. Cache "
                "misses are missing evidence, not verified source no_data. Default "
                "configured weights are A-rate=0.6/RMP=0.4; use returned configured "
                "and effective weights, including redistribution over valid "
                "dimensions. Never invent weights, scores or missing features. "
                "The sample-adjusted recommendation score is not an A rate or "
                "expected grade; raw grade counts/percentages and charts stay unchanged."
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
                        "description": "Course IDs explicitly requested for exclusion or supported by verified ineligibility. A prior attempt alone does not prove a repeat is ineligible; retain it for repeat-eligibility verification unless exclusion is authorized.",
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
                "overlap. any_compatible_combination=true only reports that "
                "at least one individual section pair was not marked as "
                "overlapping. It does not verify a complete Lec/Dis/Lab bundle, "
                "prerequisites, seats or enrollment eligibility. Unknown/TBA "
                "meeting times are not proof of compatibility; use full "
                "schedule validation and verify missing times."
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
            "name": "update_academic_details",
            "description": (
                "Save academic details the student has just stated in their own words in this "
                "conversation: courses they are taking right now, their official major, a GE category "
                "they say is already satisfied or still open, the UCI course a transfer credit counts "
                "as, or that they would rather not answer one of these. Never call it for anything "
                "inferred, planned or wished for, or read from a tool result, memory or the schedule "
                "draft: a course the student will take or wants to take is not in progress. When a "
                "course mention could be more than one course (for example 'social 1'), ask which "
                "course instead of guessing. Lists add to what is stored unless replace is true. "
                "The result says what was saved and what was not found; tell the student both, and "
                "ask them to confirm a course that was not found."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "in_progress_courses": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Course IDs the student says they are enrolled in right now, e.g. ['SWE 43'].",
                    },
                    "in_progress_term": {
                        "type": "string",
                        "description": "The term those courses are taken in, e.g. '2026 Fall'. Omit when the student did not say.",
                    },
                    "official_major": {
                        "type": "string",
                        "description": "The major on record with the university now, as the student states it.",
                    },
                    "ge_confirmed": {
                        "type": "array", "items": {"type": "string"},
                        "description": "GE categories the student says are already satisfied, e.g. ['VI'].",
                    },
                    "ge_not_met": {
                        "type": "array", "items": {"type": "string"},
                        "description": "GE categories the student says are still open, e.g. ['VI'] after answering no.",
                    },
                    "transfer_courses": {
                        "type": "array", "items": {"type": "string"},
                        "description": "UCI course IDs the student says their transfer credit counts as.",
                    },
                    "declined": {
                        "type": "array",
                        "items": {"type": "string",
                                  "enum": ["in_progress", "official_major", "ge_language", "transfer_courses"]},
                        "description": "Questions the student chose not to answer; they are not asked again.",
                    },
                    "replace": {
                        "type": "boolean",
                        "description": "True only when the student is correcting a list: the given lists replace the stored ones.",
                    },
                },
            },
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
            "name": "get_department_restrictions",
            "description": (
                "Fixed Registrar WebSoc workflow for department/school "
                "enrollment restriction comments. Use for major restriction "
                "release dates, New Only Restrictions/NORS dates, and "
                "department-specific add/drop/change or restriction-policy "
                "questions. This always starts from "
                "https://www.reg.uci.edu/perl/WebSoc for the given term and "
                "department, parses the comments above the course table, and "
                "may deep-read official UCI links that WebSoc comments point "
                "to. Do not use general web_search first for these questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {
                        "type": "string",
                        "description": "Required. Form: 'Spring 2026', 'Fall 2026', 'Spring 2025'.",
                    },
                    "department": {
                        "type": "string",
                        "description": "Canonical UCI WebSoc department code, e.g. 'ART', 'I&C SCI', 'COMPSCI'.",
                    },
                    "course_id": {
                        "type": "string",
                        "description": "Optional course code such as 'CS161' or 'ICS33'. Use when the student names a course but not the department; the tool resolves the department before querying WebSoc.",
                    },
                    "restriction_type": {
                        "type": "string",
                        "description": "Optional hint such as 'major_restriction', 'nors', or 'add_drop_change'.",
                    },
                    "follow_links": {
                        "type": "boolean",
                        "description": "Whether to deep-read official UCI links explicitly present in WebSoc comments. Defaults true.",
                    },
                },
                "required": ["term"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Controlled web search for cases where local DB tools are "
                "missing, partial, stale, unavailable, or the user explicitly "
                "asks to search/look up current external information. Use "
                "local DB tools first whenever they can verify the fact. "
                "Returns search-result records only (title, URL, domain, "
                "snippet, source_class, trust_level); does not fetch full pages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Concrete search query. Do not include sensitive profile details.",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Required. One sentence explaining why web search is "
                            "needed instead of relying only on local DB data."
                        ),
                    },
                    "preferred_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional domain filters/hints such as 'uci.edu', "
                            "'reg.uci.edu', or 'catalogue.uci.edu'."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return, 1-10. Defaults to 5.",
                    },
                },
                "required": ["query", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": (
                "Fetch one public HTTP(S) page for model-directed deep search. "
                "Returns only title, summary, key passages, page links, source "
                "positions, domain, retrieval time, and trust metadata. It does "
                "not recursively follow links. To follow a link returned by a "
                "previous fetch_page call, pass that page as parent_url; the "
                "server calculates depth. A run may fetch at most 8 unique pages "
                "and reach depth 8. Never retry already_visited URLs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Public http/https URL to fetch.",
                    },
                    "parent_url": {
                        "type": "string",
                        "description": (
                            "The already-fetched page whose links contained this "
                            "URL. Omit only for a search result, workflow-provided "
                            "URL, or other new depth-1 starting point."
                        ),
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_recommendation",
            "description": (
                "Use when actually recommending a SET of target-term course "
                "options (e.g. a schedule or elective shortlist), after candidate "
                "verification. When the dynamic recommendation gate is enabled, "
                "it must be ready before calling this tool. Stages "
                "a structured course-card list that the frontend renders as "
                "click-to-add cards next to your prose. Prose alone does not "
                "surface cards. Do not force a card call while clarification is "
                "needed, filtering is blocked, or only unverified offering "
                "candidates can be discussed; explain that status instead. "
                "Backend enriches each item with catalog title / units / "
                "sections / grades / prereq status, so you only pass IDs + "
                "category + rationale.\n"
                "CARD READINESS: courses with no usable target-term sections "
                "cannot be staged (UCI enrollment uses a 5-digit code which "
                "is term-scoped — no code = nothing for the student to type into "
                "WebReg). This does not prove not_offered or ineligibility: "
                "missing/unavailable sections remain uncertain candidates. Inspect "
                "`skipped`, preserve unknown reasons, and verify alternatives "
                "within the resolved scope instead of fabricating replacements. "
                "A deadline or other tool failure does not authorize changing "
                "the target term; explain or ask before changing scope.\n"
                "When fixed ranking is enabled independently of prefilter, "
                "backend scores determine card/primary order. Use returned "
                "configured/effective weights (default A-rate=0.6/RMP=0.4), "
                "including missing dimensions. The score is not an A probability "
                "or GPA forecast. Do not adjust raw grade charts to match it. "
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
                        "description": "Courses to stage as cards. Input order is preserved by the legacy path; when ranking is enabled, backend scores determine display order.",
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
                                        "Give a course-level rationale only. Do not put "
                                        "professors, section labels/codes, seats, meeting times, "
                                        "or unscoped percentages/GPA/ratings here: backend "
                                        "ranking may select a different primary section. "
                                        "Verified facts belong in structured section/source "
                                        "fields, not this freeform rationale. "
                                        "Good: 'CSE core, OS principles' / 'GE category needs verification' / "
                                        "'algorithms — interview prep'. "
                                        "Do not invent GE categories, degree credit, lower risk, or "
                                        "pairing guarantees. In the controlled recommendation path "
                                        "the backend generates the public rationale from verified fields. "
                                        "BAD: 'Linear Algebra — foundational for CSE; essential "
                                        "for computer graphics, ML, and upper-div systems courses. "
                                        "Lec A (Lu) has 210 seats open...' (too long and "
                                        "binds facts to a potentially different section). "
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
                            "If omitted, the single backend-resolved query term is used."
                        ),
                    },
                    "note": {
                        "type": "string",
                        "maxLength": 400,
                        "description": (
                            "Only when a degree-path tray was provided: one or two sentences, in the "
                            "student's language, on why this mix fits them. Shown above the cards if the "
                            "server's check leaves your picks unchanged. No section codes, times or numbers "
                            "that are not in the tray."
                        ),
                    },
                    "left_out": {
                        "type": "array",
                        "maxItems": 4,
                        "description": (
                            "Only when a degree-path tray was provided: courses from suggested_plan you "
                            "chose not to include, each with the reason."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {"course_id": {"type": "string"},
                                           "reason": {"type": "string", "maxLength": 160}},
                            "required": ["course_id", "reason"],
                        },
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

def _tool_get_course_info(course_id: str, *, context: Optional[dict] = None) -> dict:
    result = db.get_course_info(course_id)
    term = (context or {}).get("term")
    course = result.get("course") if isinstance(result, dict) else None
    if term and isinstance(course, dict) and course.get("ge_labels_verified") is True:
        # Adds official_check to the provenance, so a difference between
        # Anteater's label and the official GE list reaches the answer.
        try:
            from app.data.ge_evidence import enrich_course_ge_evidence
            result = {**result, "course": {**course, **enrich_course_ge_evidence(course, term)}}
        except Exception:
            logger.debug("GE cross-check unavailable", exc_info=True)
    return result


async def _tool_get_course_offerings(course_id: str, terms: list[str], *, context: dict) -> dict:
    from app.data.offerings import get_course_offerings
    from app.data.course_changes import attach_related_notices
    import asyncio
    result = await get_course_offerings(course_id, terms)
    if len(terms) == 1 and "offerings" in result:
        result["offerings"] = [await asyncio.to_thread(attach_related_notices, row, course_id, row["term"])
                               if row.get("term") else row for row in result["offerings"]]
    return result


def _tool_get_sections(
    course_id: str,
    *,
    context: dict,
    term: Optional[str] = None,
) -> dict:
    # Safety net: if the model forgot to pass term, fall back to the
    # resolved query term rather than returning unscoped data.
    effective_term = term or context.get("term")
    if not effective_term:
        return {"found": False, "source": "none", "sections": [],
                "reason": "term is required and no term was provided"}
    from app.data.websoc_payload import compact_comment_contexts
    if prefilter.active(context):
        return compact_comment_contexts(prefilter.section_response(context, course_id, effective_term))
    result = db.get_sections(course_id, effective_term)
    from app.data.course_changes import attach_related_notices
    result = attach_related_notices(result, course_id, effective_term)
    from app.recommendations.pairing import contract_enabled
    result = db.enrich_sections_pairing(course_id, effective_term, result) if contract_enabled() else result
    return compact_comment_contexts(result)


def _tool_get_live_sections(
    course_id: str,
    *,
    context: dict,
    term: Optional[str] = None,
    section_codes: Optional[list[str]] = None,
    force_refresh: bool = False,
) -> dict:
    # Same term safety net as get_sections, but this path is reserved
    # for live availability/status snapshots.
    effective_term = term or context.get("term")
    if not effective_term:
        return {"found": False, "source": "none", "sections": [],
                "is_live": False,
                "reason": "term is required and no term was provided"}
    result = db.get_live_sections(
        course_id,
        effective_term,
        section_codes=section_codes,
        force_refresh=force_refresh,
    )
    if not section_codes:
        from app.data.course_changes import attach_related_notices
        result = attach_related_notices(result, course_id, effective_term)
    from app.data.websoc_payload import compact_comment_contexts
    if prefilter.active(context):
        return compact_comment_contexts(prefilter.apply_section_response(context, course_id, effective_term, result))
    from app.recommendations.pairing import contract_enabled
    if contract_enabled():
        result = db.enrich_sections_pairing(course_id, effective_term, result, force_refresh=force_refresh)
    return compact_comment_contexts(result)


def _tool_get_grade_distribution(course_id: str) -> dict:
    return db.get_grade_distribution(course_id)


def _tool_get_zotistics_grades(
    course_id: Optional[str] = None,
    instructor_name: Optional[str] = None,
    department: Optional[str] = None,
    *,
    context: dict,
) -> dict:
    result = grade_evidence.get_history(context, course_id, instructor_name, department)
    grade_evidence.add_chart(context, result, course_id, instructor_name)
    return result


def _tool_get_professor_rating(
    instructor_name: str,
    department: Optional[str] = None,
) -> dict:
    from app.data.professors import verify_rating_identity
    return verify_rating_identity(instructor_name, db.get_professor_rating(instructor_name, department=department))


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
    allow_in_progress: bool = True,
) -> dict:
    # Default to the student's actual lists if the model didn't pass them.
    if completed_courses is None or in_progress_courses is None:
        prof = db.get_student_profile(context.get("user_id", ""))
        if prof.get("found"):
            p = prof["profile"]
            if completed_courses is None:
                completed_courses = p.get("completed_course_attempts", p.get("completed_courses", []))
            if in_progress_courses is None:
                in_progress_courses = p.get("selected_courses", [])
    return db.check_prerequisites_met(
        course_id,
        completed_courses=completed_courses or [],
        in_progress_courses=in_progress_courses or [],
        allow_in_progress=allow_in_progress,
    )


def _tool_search_courses(
    *,
    context: dict,
    term: Optional[str] = None,
    department: Optional[str] = None,
    ge_category: Optional[str] = None,
    exclude_ids: Optional[list[str]] = None,
) -> dict:
    from app.data.websoc_payload import compact_comment_contexts
    effective_term = term or context.get("term")
    if not effective_term:
        return {"found": False, "source": "none", "courses": [],
                "reason": "term is required and no term was provided"}
    if prefilter.active(context):
        pool = prefilter.search_candidates(context, effective_term, department, ge_category, exclude_ids)
        if not config.recommendation_ranking_enabled() or not pool.get("found"):
            return compact_comment_contexts(prefilter.model_pool_view(pool))
        from app.data.recommendations import rank_courses
        pool = prefilter.model_pool_view(pool)
        return compact_comment_contexts(rank_courses(
            effective_term, department, ge_category, exclude_ids,
            weights=config.recommendation_ranking_weights(),
            reliability=config.recommendation_ranking_reliability(),
            section_selector=lambda sections: _ranking_primary_sections(sections, context),
            candidate_pool=pool,
            sections_by_course={row["course_id"]: row.get("sections") or [] for row in pool["courses"]},
        ))
    if config.recommendation_ranking_enabled():
        from app.data.recommendations import rank_courses
        return compact_comment_contexts(rank_courses(
            effective_term, department, ge_category, exclude_ids,
            weights=config.recommendation_ranking_weights(),
            reliability=config.recommendation_ranking_reliability(),
            section_selector=lambda sections: _ranking_primary_sections(sections, context),
        ))
    return compact_comment_contexts(db.search_courses(
        term=effective_term,
        department=department,
        ge_category=ge_category,
        exclude_ids=exclude_ids,
    ))


def _ranking_primary_sections(sections: list[dict], context: dict, pairing=None) -> list[dict]:
    """Reuse the recommendation's existing section restrictions, without scoring eligibility."""
    if "_ranking_profile" not in context:
        response = db.get_student_profile(context.get("user_id", ""))
        context["_ranking_profile"] = (response.get("profile") if response.get("found") else {}) or {}
    units = _estimate_student_units(context["_ranking_profile"].get("year"))
    bookable = [s for s in sections if not s.get("is_cancelled") and s.get("section_code")]
    active = [s for s in bookable if _section_enrollable(s, units)]
    if pairing is not None:
        from app.recommendations.pairing import available_pairing_sections
        hard = ((context.get("_prefilter_state") or {}).get("plan") or {}).get("hard_filters") or {}
        formal = bool(hard.get("excluded_weekdays") or hard.get("earliest_start_is_hard"))
        active, _ = available_pairing_sections(pairing, active, verified_only=formal)
    primaries, secondaries = _split_primary_secondary(active)
    original_primary, original_secondary = _split_primary_secondary(bookable)
    if pairing is None and original_primary and original_secondary and not secondaries:
        return []
    return primaries or active


def prepare_recommendation_grades(items: list[dict], term: str, context: dict) -> list[dict]:
    gate = prefilter.recommendation_gate(context)
    if gate.get("enabled") and not gate.get("ready"):
        return []
    from app.data import policies
    if policies.is_term_past_deadline(term, deadline_key="late_add_drop_end") is True:
        return []
    items, _ = prefilter.filter_items(context, items, term)
    if prefilter.active(context):
        for item in items:
            prefilter.section_response(context, item["course_id"], term)
        items, _ = prefilter.filter_items(context, items, term)
        guarded, rejected = prefilter.guard_call("propose_recommendation", {"items": items, "term": term}, context)
        items = [] if rejected else guarded["items"]
    elif config.recommendation_ranking_enabled():
        # Ranking-only mode also obtains lightweight rules before grade/RMP
        # requests; retain the complete official rules alongside the rows.
        for item in items:
            ref = parse_course_mention(item.get("course_id") or "")
            if ref:
                key = (ref.display(), term)
                response = context.setdefault("_recommendation_sections", {}).get(key)
                response = response if response is not None else db.get_sections(ref.display(), term)
                response = db.enrich_sections_pairing(ref.display(), term, response)
                from app.recommendations.pairing import available_pairing_sections
                sections, _ = available_pairing_sections(response.get("pairing"), response.get("sections") or [])
                context["_recommendation_sections"][key] = {**response, "sections": sections}
    result = []
    for item in items[:8]:
        ref = parse_course_mention(item.get("course_id") or "")
        response = context.get("_recommendation_sections", {}).get((ref.display(), term), {}) if ref else {}
        pairing = response.get("pairing")
        result += grade_evidence.prepare_recommendation_grades(
            [item], term, context, lambda sections, proof=pairing: _ranking_primary_sections(sections, context, proof),
        )
    prefilter.note_details(context, "automatic_grade_lookup", len(result))
    return result


def prepare_professor_grades(args: dict, context: dict) -> list[dict]:
    """Enrich professor evaluations, while leaving offering-only comparisons focused."""
    import re
    query = context.get("user_query") or ""
    grade_request = re.search(r"给分|成绩|等第|gpa|grading|grades?|easy\s*a|值得|怎么样|好不好|worth|compare|比较", query, re.I)
    if not grade_request:
        return []
    if context.get("query_term_source") in {"comparison", "history"} and not re.search(
            r"给分|成绩|等第|gpa|grading|grades?|easy\s*a|值得|怎么样|好不好|worth", query, re.I):
        return []
    name = args.get("instructor_name")
    if not name:
        return []
    if prefilter.active(context):
        _, rejection = prefilter.guard_call("get_professor_rating", args, context)
        if rejection:
            return []
    courses = list(dict.fromkeys(context.get("query_course_ids") or []))
    if prefilter.active(context) and not courses:
        courses = list(dict.fromkeys(row["course_id"] for (row_term, _), row
                                    in context["_prefilter_state"]["candidates"].items()
                                    if row_term == context.get("term") and prefilter._retained_person(name, [row])))
    if not courses:
        courses = [None]
    results = []
    for course in courses[:8]:
        ref = parse_course_mention(course or "")
        department = args.get("department") or (ref.department if ref else None)
        if prefilter.active(context) and course:
            _, rejection = prefilter.guard_call("get_zotistics_grades", {
                "course_id": course, "instructor_name": name,
            }, context)
            if rejection:
                continue
        result = grade_evidence.get_history(context, course, name, department)
        grade_evidence.add_chart(context, result, course, name)
        results.append({"course_id": course, "instructor": name, **result})
    return results


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


def _tool_get_department_restrictions(
    term: str,
    *,
    context: dict,
    department: Optional[str] = None,
    course_id: Optional[str] = None,
    restriction_type: Optional[str] = None,
    follow_links: bool = True,
) -> dict:
    resolved_department = (department or "").strip()
    resolved_from_course: Optional[str] = None
    if not resolved_department and course_id:
        ref = parse_course_mention(course_id)
        if ref:
            resolved_department = ref.department
            resolved_from_course = ref.display()
    if resolved_department:
        # WebSoc only knows registrar codes. "ECONOMICS" or "Art History" is
        # the same department as ECON or ART HIS; without this the lookup
        # failed and the model had to guess the code and call again.
        resolved_department = websoc_workflow.registrar_department_code(term, resolved_department)

    if not resolved_department:
        return {
            "ok": False,
            "workflow_id": "websoc_department_restrictions",
            "error_code": "department_required",
            "message": "department is required when course_id cannot be parsed",
            "term": term,
            "department": None,
            "course_id": course_id,
            "restriction_type": restriction_type,
        }

    normalized_course_id = None
    if course_id:
        ref = parse_course_mention(course_id)
        normalized_course_id = ref.display() if ref else course_id

    raw_restriction_type = restriction_type or "ambiguous"
    if raw_restriction_type == "major_restriction":
        raw_restriction_type = RestrictionType.SCHOOL_MAJOR.value
    try:
        resolved_restriction_type = RestrictionType(raw_restriction_type)
    except ValueError:
        resolved_restriction_type = RestrictionType.AMBIGUOUS

    student_major = None
    student_school = None
    user_id = context.get("user_id")
    if user_id:
        profile_result = db.get_student_profile(user_id)
        if profile_result.get("found"):
            profile = profile_result.get("profile") or {}
            student_major = profile.get("major")
            student_school = profile.get("school")

    query = RestrictionQuery(
        term=term,
        department=resolved_department,
        course_id=normalized_course_id,
        restriction_type=resolved_restriction_type,
        student_major=student_major,
        student_school=student_school,
        academic_level=str(context.get("academic_level") or "undergraduate"),
    )
    observability.log_event(
        logger,
        logging.INFO,
        "restriction_query_classified",
        workflow_id="websoc_department_restrictions",
        restriction_type=resolved_restriction_type.value,
        term=term,
        department=resolved_department,
        has_course=bool(normalized_course_id),
    )

    # Notices come from the synced term snapshot when it is recent; the live
    # Registrar page is the fallback. Linked pages are still read on demand.
    result = websoc_workflow.department_restrictions_from_snapshot(
        term=term,
        department=resolved_department,
    ) or websoc_workflow.fetch_websoc_department_restrictions(
        term=term,
        department=resolved_department,
    )
    if normalized_course_id:
        result["course_id"] = normalized_course_id
    if resolved_from_course:
        result["department_resolved_from"] = "course_id"
    result["restriction_type"] = resolved_restriction_type.value
    result["restriction_query"] = query.to_dict()
    linked_result: dict = {}
    if result.get("ok") and follow_links:
        linked_result = websoc_workflow.fetch_linked_official_pages(result)
        result["linked_pages"] = compact_linked_restriction_result(linked_result)
    elif result.get("ok"):
        result["linked_pages"] = {}
    if result.get("ok"):
        bundle = build_restriction_evidence_bundle(
            query,
            websoc_result=result,
            linked_result=linked_result,
        )
        # The notices' own restriction sentences, word for word: quoted when
        # no date can be verified, and beside a date read from one of them.
        _notice_events, notice_statements = read_notice_restrictions(result, query)
        result["notice_statements"] = notice_statements
        result["evidence_bundle"] = bundle.to_dict()
        result["verified_facts"] = build_verified_restriction_facts(bundle, notice_statements)
        result["fetch_summary"] = _restriction_fetch_summary(
            result,
            linked_result,
            bundle.to_dict(),
        )
        observability.log_event(
            logger,
            logging.INFO,
            "restriction_evidence_gate",
            workflow_id="websoc_department_restrictions",
            restriction_type=resolved_restriction_type.value,
            evidence_status=bundle.evidence_status.value,
            missing_fields=bundle.missing_required_fields,
            event_count=len(bundle.events),
            conflict_count=len(bundle.conflicts),
        )

        for key in (
            "school_comments",
            "department_comments",
            "comment_blocks",
            "links",
            "fetches",
        ):
            result.pop(key, None)
    return result


def _restriction_fetch_summary(
    websoc_result: dict,
    linked_result: dict,
    evidence_bundle: dict,
) -> list[dict]:
    evidence_urls = {
        _fetch_url_key(event.get("source_url"))
        for event in evidence_bundle.get("events") or []
        if event.get("source_url")
    }
    summary = []
    for fetch in [
        *(websoc_result.get("fetches") or []),
        *(linked_result.get("fetches") or []),
    ]:
        item = {
            key: fetch.get(key)
            for key in (
                "method",
                "url",
                "final_url",
                "host",
                "source_role",
                "status_code",
                "ok",
                "bytes",
                "duration_ms",
                "depth",
                "parent_url",
                "error",
                "local",
                "snapshot_synced_at",
                "cached",
                "cached_at",
            )
            if key in fetch or key not in {"local", "snapshot_synced_at", "cached", "cached_at"}
        }
        item["provides_evidence"] = bool(
            item.get("source_role") != "registrar_websoc_form"
            and _fetch_url_key(item.get("final_url") or item.get("url"))
            in evidence_urls
        )
        summary.append(item)
    return summary


def _fetch_url_key(value: Optional[str]) -> str:
    return (value or "").split("#", 1)[0].rstrip("/").lower()


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


def _tool_update_academic_details(*, context: dict, in_progress_courses=None, in_progress_term=None,
                                  official_major=None, ge_confirmed=None, ge_not_met=None,
                                  transfer_courses=None, declined=None, replace: bool = False) -> dict:
    from app.agent import profile_followup

    result = profile_followup.save_statement(
        context.get("user_id", ""), in_progress_courses=in_progress_courses, in_progress_term=in_progress_term,
        official_major=official_major, ge_confirmed=ge_confirmed, ge_not_met=ge_not_met,
        transfer_courses=transfer_courses, declined=declined, replace=replace is True)
    if not result.get("ok"):
        return result
    update = {key: result[key] for key in ("saved", "declined", "not_found")}
    # Saving an answer is the whole job of such a turn; without this the model
    # went on to search and rank courses nobody had asked for again.
    result["next_step"] = ("Tell the student in one or two sentences what was saved and what was not found, then stop. "
                           "Call other tools only if this message from the student also asks for something else.")
    context.setdefault("_academic_updates", []).append(update)
    # A recommendation later in this turn must use the record as it now stands.
    update["applied_this_turn"] = prefilter.refresh_profile(context)
    if update["applied_this_turn"]:
        result["recommendation_context"] = (
            "Candidates and the graduation path were rebuilt from the updated record; "
            "earlier prefilter and graduation-path context in this turn is out of date.")
        path = (context.get("_prefilter_state") or {}).get("graduation_path")
        if path:
            result["graduation_path"] = {key: value for key, value in path.items() if key != "reasons"}
    return result


def _tool_web_search(
    query: str,
    reason: str,
    *,
    context: dict,
    preferred_domains: Optional[list[str]] = None,
    max_results: Optional[int] = None,
) -> dict:
    """Controlled web-search tool.

    This wrapper keeps agent context out of the query itself. The data
    layer owns provider choice, rate limiting, URL classification, and
    structured errors.
    """

    subject = str(context.get("user_id") or "anonymous")
    result = web_search_data.search_web(
        query=query,
        reason=reason,
        preferred_domains=preferred_domains,
        max_results=max_results,
        subject=subject,
    )
    state = _deep_search_state(context)
    return state.register_search_result(result)


def _tool_fetch_page(
    url: str,
    *,
    context: dict,
    parent_url: Optional[str] = None,
) -> dict:
    """Fetch one page through the current run's dedupe and budget state."""

    return _deep_search_state(context).fetch(url, parent_url=parent_url)


def _deep_search_state(context: dict) -> DeepSearchRunState:
    state = context.get("deep_search_state")
    if isinstance(state, DeepSearchRunState):
        return state
    state = DeepSearchRunState(query=str(context.get("user_query") or ""))
    context["deep_search_state"] = state
    return state


def _tool_propose_recommendation(
    items: list[dict],
    *,
    context: dict,
    term: Optional[str] = None,
    note: Optional[str] = None,          # read by the plan check in the prefilter guard
    left_out: Optional[list] = None,
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
    # Refuse enrollment cards after the deadline without authorizing a
    # different query term; scope changes still require the user's request.
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
                f"{cal.get('late_add_drop_end', 'an earlier date')}. This tool "
                f"cannot stage enrollment cards after that deadline. Keep the "
                f"resolved target term and explain the limitation; ask the "
                f"student before changing the query scope."
            ),
            "term": effective_term,
            "deadline_passed": cal.get("late_add_drop_end"),
            "pivot_hint": "Keep the resolved target term; a different term requires the student's request or clarification.",
        }
    # expired is None (unknown term — Summer, mistyped, etc.) → allow.
    # Better to stage cards with possibly-stale dates than to silently
    # refuse and confuse the user. The resolved query scope is the
    # source of truth for whether they can actually enroll.

    # Student profile drives the prereq check. If we can't read it we
    # don't fail the whole call — just skip the prereq enrichment.
    prof_resp = db.get_student_profile(context.get("user_id", ""))
    profile = (prof_resp.get("profile") if prof_resp.get("found") else {}) or {}
    completed = profile.get("completed_course_attempts", profile.get("completed_courses", [])) or []
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

    # An empty/fully skipped proposal must still return a valid acknowledgement.
    from app.recommendations.pairing import (contract_enabled, normalize_pairing_evidence,
                                            available_pairing_sections, verify_selection, pairing_for_primary)
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
        ref = parse_course_mention(cid_canon)
        cached_sections = context.get("_recommendation_sections", {}).get(
            (ref.display(), effective_term) if ref else None,
        )
        sec_resp = cached_sections if cached_sections is not None else db.get_sections(cid_canon, effective_term)
        pairing_contract = None
        if contract_enabled():
            sec_resp = db.enrich_sections_pairing(cid_canon, effective_term, sec_resp)
            from app.data.ge_evidence import enrich_course_ge_evidence
            course = {**course, **enrich_course_ge_evidence(course, effective_term)}
            candidate = (context.get("_prefilter_state") or {}).get("candidates", {}).get(
                (effective_term, ref.display()) if ref else None, {})
            pairing_contract = normalize_pairing_evidence({
                **course, "pairing": sec_resp.get("pairing") or candidate.get("pairing") or course.get("pairing"),
            }, term=effective_term)
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
                "reason": (f"no usable target-term section code available for {effective_term}; "
                           "this absence alone does not establish not_offered or ineligibility"),
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
        if pairing_contract is not None:
            active, pairing_availability = available_pairing_sections(pairing_contract, active)
            if pairing_availability == "unsatisfied":
                skipped.append({"course_id": cid_canon, "reason": "verified required pairing has no complete available combination"})
                continue

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
        if pairing_contract is None and orig_primaries and orig_secondaries and not secondaries:
            skipped.append({
                "course_id": cid_canon,
                "reason": (f"all Dis/Lab sections of {cid_canon} are FULL — "
                           f"per registrar, the Lec becomes unenrollable when "
                           f"every secondary is full"),
            })
            continue

        primary_rankings = {}
        primary_ratings = {}
        if config.recommendation_ranking_enabled():
            from app.recommendations.features import build_candidate_features
            from app.recommendations.ranking import rank_candidates
            primary_ratings = _rmp_lookup_map(
                _gather_unique_instructors(primaries), department=course.get("department"),
            )
            prefilter.note_details(context, "card_professor_rating", len(primary_ratings))
            ranking_rows = []
            for section in primaries:
                names = section.get("instructors") or []
                name = names[0] if len(names) == 1 else None
                history = grade_evidence.find_history(context, cid_canon, name, course.get("department")) if name else None
                ranking_rows.append({"section": section, "features": build_candidate_features(
                    grade_evidence.ranking_history(history), primary_ratings.get(name),
                    course_id=ref.display() if ref else cid_canon,
                    instructor_name=name, primary_section=section,
                )})
            ranked_sections = rank_candidates(
                ranking_rows, weights=config.recommendation_ranking_weights(),
                reliability=config.recommendation_ranking_reliability(),
            )
            primaries = [row["section"] for row in ranked_sections]
            primary_rankings = {row["section"].get("section_code"): row["ranking"] for row in ranked_sections}

        primary = primaries[0] if config.recommendation_ranking_enabled() else next(
            (s for s in primaries if (s.get("section_type") or "").lower().startswith("lec")),
            primaries[0],
        )
        primary_code = primary.get("section_code")
        selected_pairing = pairing_for_primary(pairing_contract, primary_code) if pairing_contract is not None else None
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
            **_section_evidence_fields(s),
        } for s in ordered]

        # Secondary-required signal: this is the hard rule. When a
        # course has BOTH primary and secondary sections, the student
        # must pick one of each. Surface the bookable secondary codes
        # so the frontend can show "+ Dis required (34251 / 34252 / ...)".
        requires_secondary = bool(secondaries) and bool(primaries)
        if pairing_contract is not None:
            requires_secondary = {"required": True, "not_required": False, "unknown": None}[selected_pairing["requirement"]]
        secondary_type = None
        secondary_codes_list: list[dict] = []
        if requires_secondary or (pairing_contract is not None and secondaries):
            # Take the most-common type letter so the card label is
            # specific ("+ Dis required" vs "+ Lab required") rather
            # than the generic "secondary".
            type_counts: dict[str, int] = {}
            for s in secondaries:
                st = (s.get("section_type") or "").strip()
                type_counts[st] = type_counts.get(st, 0) + 1
            secondary_type = max(type_counts, key=type_counts.get) if type_counts else None
            if pairing_contract is not None:
                secondary_type = "+".join(selected_pairing["required_types"]) or None
            secondary_codes_list = [{
                "code":        s.get("section_code"),
                "type":        s.get("section_type"),
                "days":        s.get("days"),
                "time":        s.get("time_display") or _format_section_time(s),
                "instructors": s.get("instructors") or [],
                "seats_open":  s.get("seats_open"),
                **_section_evidence_fields(s),
            } for s in secondaries]

        # Prereq status — unknown is not treated as satisfied. The
        # engine returns a tri-state status so cards can surface exact
        # missing branches or unverifiable grade/exam data.
        prereq = db.check_prerequisites_met(
            cid_canon,
            completed_courses=completed,
            in_progress_courses=in_progress,
        )
        if prereq.get("found"):
            prereq_status = prereq.get("status", "unknown")
            prereq_met = prereq_status == "met"
            prereq_missing = prereq.get("missing", []) or []
            prereq_unknown = prereq.get("unknown", []) or []
            prereq_required = prereq.get("required", []) or []
        else:
            prereq_status = "unknown"
            prereq_met = False
            prereq_missing = []
            prereq_unknown = [
                prereq.get("reason", "prerequisite status could not be verified")
            ]
            prereq_required = []

        grade_resp = db.get_grade_distribution(cid_canon)
        prefilter.note_details(context, "card_legacy_grade_distribution")
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
        prefilter.note_details(context, "card_professor_rating", len(unique_instructors))
        rmp_by_instructor = {**primary_ratings, **_rmp_lookup_map(
            [name for name in unique_instructors if name not in primary_ratings],
            department=course.get("department"),
        )}
        section_groups = _group_sections_by_letter(
            primaries, secondaries, rmp_by_instructor=rmp_by_instructor,
        ) if pairing_contract is None else _group_sections_by_pairing(
            primaries, secondaries, pairing_contract, rmp_by_instructor)

        card_reason, model_reason = _course_card_reason(it.get("reason"), context, sections)
        card = {
            "course_id":   cid_canon,
            "title":       course.get("title"),
            "units":       course.get("units"),
            "department":  course.get("department"),
            "ge_category": course.get("ge_category"),
            "category":    it.get("category", "elective"),
            "priority":    it.get("priority", "medium"),
            "reason":      card_reason,
            "term":        effective_term,
            "found":       True,
            "prereq_status":  prereq_status,
            "prereq_met":     prereq_met,
            "prereq_missing": prereq_missing,
            "prereq_unknown": prereq_unknown,
            "prereq_required": prereq_required,
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
            "course_source":        info.get("source"),
            "course_provenance":    course.get("provenance"),
            "section_source":       sec_resp.get("source"),
            "section_retrieved_at": sec_resp.get("retrieved_at"),
            "pairing_source_proof": sec_resp.get("pairing_source_proof") or sec_resp.get("source_proof"),
            "data_coverage":        sec_resp.get("data_coverage"),
        }
        if pairing_contract is not None:
            card["pairing"] = pairing_contract
            card["pairing_selected"] = selected_pairing
            selection = [primary]
            by_code = {str(section.get("section_code")): section for section in active}
            for bundle in selected_pairing["allowed_bundles"] if pairing_contract["version"] == 1 else []:
                if bundle["primary_code"] == str(primary_code) and all(code in by_code for code in bundle["secondary_codes"]):
                    selection += [by_code[code] for code in bundle["secondary_codes"]]
                    break
            card["selection_verification"] = verify_selection(pairing_contract, selection, primary_code=primary_code)
        if model_reason is not None:
            # Keep the model rationale for diagnostics, never as current-section evidence.
            card["model_reason"] = model_reason
        grade_evidence.attach_history(card, context)
        if prefilter.active(context):
            card["prefilter"] = prefilter.card_metadata(context, cid_canon, effective_term)
            from app.agent import graduation
            path_reason = graduation.card_reason(context, cid_canon)
            if path_reason:
                card["graduation_path"] = path_reason
            if selected_pairing is not None and selected_pairing["requirement"] != "unknown":
                card["prefilter"]["warnings"] = [warning for warning in card["prefilter"]["warnings"]
                    if warning.get("code") != "secondary_pairing_unknown"]
                card["prefilter"]["status"] = "needs_verification" if card["prefilter"]["warnings"] else "keep"
            for group in section_groups:
                if isinstance(group.get("primary"), dict):
                    # Course-wide GE/audit warnings are shown once. Pairing is
                    # displayed from the group's own evidence, not copied text.
                    group["primary"]["prefilter"] = {"enabled": True, "status": "keep", "warnings": []}
        if primary_rankings:
            card["ranking"] = primary_rankings.get(primary_code)
            for group in section_groups:
                if isinstance(group.get("primary"), dict):
                    group["primary"]["ranking"] = primary_rankings.get(group["primary"].get("code"))
            section_groups.sort(key=lambda group: (
                (((group.get("primary") or {}).get("ranking") or {}).get("final_score") is None),
                -((((group.get("primary") or {}).get("ranking") or {}).get("final_score")) or 0),
            ))
        if config.recommendation_ranking_enabled() or config.dynamic_prefilter_enabled():
            from app.recommendations.presentation import normalize_recommendation_card
            from app.recommendations.filter_plan import trusted_ge_audit

            # Keep the model's rationale private. Public cards are generated
            # only after the selected primary, GE state and grades are known.
            context.setdefault("_recommendation_model_reasons", {})[(effective_term, cid_canon)] = _truncate_reason(it.get("reason"))
            card.update(_recommendation_ge_fields(context, cid_canon, effective_term, course))
            audit = trusted_ge_audit(profile)
            if audit is not None:
                card["personal_ge_audit"] = {key: audit[key] for key in (
                    "source", "verified", "authorized", "unmet_categories", "projected_categories",
                ) if key in audit}
            card = normalize_recommendation_card(
                card, language=context.get("response_language") or "en",
            )
        cards.append(card)

    if config.recommendation_ranking_enabled():
        cards.sort(key=lambda card: (
            (card.get("ranking") or {}).get("final_score") is None,
            -((card.get("ranking") or {}).get("final_score") or 0),
        ))
    review = context.get("_plan_review")
    if review:
        # A term plan reads required courses first; a score does not reorder a degree path.
        position = {course: index for index, course in enumerate(review["courses"])}
        cards.sort(key=lambda card: position.get(card.get("course_id"), len(position)))

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
    context["_recommendation_proposed"] = context.get("_recommendation_proposed", False) or bool(cards)

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
        **({"pairing": [{"course_id": c["course_id"], "pairing": c["pairing"],
                         "selection_verification": c["selection_verification"]} for c in cards if "pairing" in c]}
           if contract_enabled() else {}),
        "schedule_validation": schedule_validation,
        "historical_grades": [
            {"course_id": card["course_id"], "instructor": history["instructor"],
             "scope": history.get("scope"), "grades": history.get("grades"),
             "coverage": history.get("coverage"), "source_url": history.get("source_url")}
            for card in cards for group in card.get("section_groups", [])
            for history in (group.get("primary") or {}).get("grade_history", [])
        ],
        "ranking_enabled": config.recommendation_ranking_enabled(),
        **({"ge_evidence": [{"course_id": c["course_id"], **c["ge_evidence"]} for c in cards]}
           if config.recommendation_ranking_enabled() or config.dynamic_prefilter_enabled() else {}),
        **({"prefilter": prefilter.model_summary(context)} if prefilter.active(context) else {}),
        **({"rankings": [{"course_id": c["course_id"], **(c.get("ranking") or {})} for c in cards]}
           if config.recommendation_ranking_enabled() else {}),
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


def _recommendation_ge_fields(context, course_id, term, course):
    """Read target-term GE provenance without altering the legacy catalog API.

    get_course_info can return a different term's catalog record. Its raw
    labels alone therefore cannot establish the recommendation's GE state.
    A missing local verification flag is unknown, never confirmed non-GE.
    """
    key = prefilter.course_key(course_id)
    row = (context.get("_prefilter_state") or {}).get("candidates", {}).get((term, key))
    source = dict(row) if row is not None else None
    if source is None:
        try:
            ref = parse_course_mention(course_id)
            catalog = db.get_catalog(db._to_term(term))
            record = catalog.get_course(ref) if ref else None
            if record:
                source = {**db._course_record_to_dict(record),
                          "ge_labels_verified": record.ge_categories_verified is True}
        except Exception:
            logger.debug("Target-term GE metadata unavailable", exc_info=True)
    source = source or (course if course.get("ge_provenance") else {})
    source = {"course_id": course_id, **source}
    from app.data.ge_evidence import enrich_course_ge_evidence
    source = {**source, **enrich_course_ge_evidence(source, term)}
    return {
        "ge_categories": list(source.get("ge_categories") or []),
        "ge_labels_verified": source.get("ge_labels_verified") is True,
        "ge_provenance": source.get("ge_provenance") or source.get("provenance") or {},
        "ge_evidence_course_id": source.get("ge_evidence_course_id") or source.get("course_id"),
        "ge_evidence_term": source.get("ge_evidence_term") or term,
    }

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


_SECTION_FACT_IN_REASON = re.compile(
    r"\b(?:lec(?:ture)?|dis(?:cussion)?|lab|section)(?:\s+|[:#]\s*)(?:[A-Z]\d{0,2}|\d{5})\b"
    r"|(?<![A-Za-z0-9])[A-Z]\d{0,2}\s*班"
    r"|(?:讲座|讨论课|实验课|教学班)\s*[A-Z]\d{0,2}(?![A-Za-z0-9])"
    r"|(?:班次|课号|代码|班号)\s*[:：#]?\s*\d{5}(?!\d)"
    r"|(?:余|剩余?|剩下|空位|席位|座位|名额)\s*\d+"
    r"|\d+\s*(?:席|个空位|个名额|seats?\b|spots?\b)"
    r"|\d+(?:\.\d+)?\s*(?:%|％|\bpercent\b)"
    r"|\b(?:[01]?\d|2[0-3]):[0-5]\d\b"
    r"|\b\d{1,2}\s*[ap]\.?m\.?\b"
    r"|(?:\b(?:gpa|rating|score)|均分|推荐分|评分)\s*[:：=]?\s*\d+(?:\.\d+)?",
    re.IGNORECASE,
)


def _course_card_reason(reason, context, sections):
    """Prevent unbound short rationale facts surviving a backend primary change.

    This is a narrow opt-in rendering guard, not a general factual validator.
    Ordinary course codes, years and course-level subjects remain unchanged.
    """
    rendered = _truncate_reason(reason)
    if not (config.recommendation_ranking_enabled() or config.dynamic_prefilter_enabled()):
        return rendered, None
    risky = bool(_SECTION_FACT_IN_REASON.search(rendered))
    for section in sections or []:
        for name in section.get("instructors") or []:
            if not isinstance(name, str) or name.upper() in {"STAFF", "TBA", "TBD"}:
                continue
            tokens = re.findall(r"[^\W\d_]+", name)
            known = (r"(?<!\w)" + r"[\s,.\-]+".join(re.escape(token) for token in tokens) + r"(?!\w)"
                     if tokens else None)
            surname = name.split(",", 1)[0].strip()
            if (known and re.search(known, rendered, re.IGNORECASE)) or (surname and re.search(
                    r"(?:\b(?:prof(?:essor)?\.?|instructor)\s+|(?:教授|老师)\s*)"
                    + re.escape(surname) + r"\b|" + re.escape(surname) + r"\s*(?:教授|老师)",
                    rendered, re.IGNORECASE)):
                risky = True
    if not risky:
        return rendered, None
    neutral = ("课程候选；请核对各班次与历史证据。"
               if str(context.get("response_language") or "").startswith("zh")
               else "Course option; compare verified section and historical evidence.")
    return neutral, rendered


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


def _section_evidence_fields(section: dict) -> dict:
    """Keep complete source evidence across compact card/section views."""
    return {key: deepcopy(section[key]) for key in (
        "meetings", "comments", "section_comment", "is_cancelled", "cancellation_status",
    ) if key in section}


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
        **_section_evidence_fields(s),
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
            r = _tool_get_professor_rating(name, department=department)
        except Exception as e:
            logger.debug("RMP lookup for %r failed: %s", name, e)
            r = {"found": False}
        out[name] = r
    return out


def _group_sections_by_pairing(primaries, secondaries, pairing, rmp_by_instructor=None):
    """Group only officially related codes; keep unassigned components visible."""
    from app.recommendations.pairing import pairing_for_primary
    groups, assigned = [], set()
    for primary in primaries:
        code = str(primary.get("section_code"))
        proof = pairing_for_primary(pairing, code)
        codes = {str(secondary) for bundle in proof.get("allowed_bundles", [])
                 if str(bundle.get("primary_code")) == code
                 for secondary in bundle.get("secondary_codes", [])}
        related = [section for section in secondaries if str(section.get("section_code")) in codes]
        assigned.update(str(section.get("section_code")) for section in related)
        summary = _summarize_section(primary, rmp_by_instructor)
        summary["pairing_selected"] = proof
        groups.append({"letter": primary.get("section_num") or "?", "primary": summary,
                       "secondaries": [_summarize_section(section, rmp_by_instructor) for section in related]})
    unassigned = [section for section in secondaries if str(section.get("section_code")) not in assigned]
    if unassigned:
        groups.append({"letter": "?", "primary": None, "relationship": "unknown",
                       "secondaries": [_summarize_section(section, rmp_by_instructor) for section in unassigned]})
    return groups


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
    "get_course_offerings":     _tool_get_course_offerings,
    "get_live_sections":        _tool_get_live_sections,
    "get_grade_distribution":   _tool_get_grade_distribution,
    "get_zotistics_grades":     _tool_get_zotistics_grades,
    "get_professor_rating":     _tool_get_professor_rating,
    "get_professor_reviews":    _tool_get_professor_reviews,
    "get_professor_tags":       _tool_get_professor_tags,
    "summarize_professor_reviews": _tool_summarize_professor_reviews,
    "check_prerequisites_met":  _tool_check_prerequisites_met,
    "search_courses":           _tool_search_courses,
    "check_section_conflict":   _tool_check_section_conflict,
    "get_student_profile":      _tool_get_student_profile,
    "update_academic_details":  _tool_update_academic_details,
    "get_policy":               _tool_get_policy,
    "get_department_restrictions": _tool_get_department_restrictions,
    "web_search":               _tool_web_search,
    "fetch_page":               _tool_fetch_page,
    "propose_recommendation":   _tool_propose_recommendation,
}


# ══════════════════════════════════════════════════════════
#  Dispatch entry point
# ══════════════════════════════════════════════════════════

TERM_SCOPED_TOOLS = {
    "get_course_offerings",
    "get_sections",
    "get_live_sections",
    "search_courses",
    "check_section_conflict",
    "get_department_restrictions",
    "propose_recommendation",
}


def enforce_query_term_scope(
    name: str,
    args: dict,
    *,
    context: dict,
) -> tuple[dict, str | None]:
    """Enforce the backend-resolved immutable term allowlist."""
    resolved = dict(args or {})
    if name not in TERM_SCOPED_TOOLS:
        return resolved, None

    allowed: list[str] = []
    for raw in context.get("allowed_query_terms") or []:
        parsed = parse_term_key(str(raw))
        if parsed.kind != "single":
            return resolved, "backend query-term scope is invalid"
        canonical = parsed.terms[0].canonical_name
        if canonical not in allowed:
            allowed.append(canonical)
    if not allowed:
        observability.increment("term.guard_reject", tool=name)
        return resolved, "no query term is authorized for this tool call"

    if name == "get_course_offerings":
        requested = resolved.get("terms")
        if not isinstance(requested, list) or not 1 <= len(requested) <= 12:
            return resolved, "terms must contain 1 to 12 canonical query terms"
        canonical_terms = []
        for raw in requested:
            parsed = parse_term_key(str(raw))
            if parsed.kind != "single" or not parsed.terms[0].is_regular:
                return resolved, "only Fall, Winter and Spring are supported"
            canonical = parsed.terms[0].canonical_name
            if canonical not in allowed:
                return resolved, f"{canonical} is outside the allowed query-term scope; allowed terms: {', '.join(allowed) or 'none'}"
            if canonical not in canonical_terms:
                canonical_terms.append(canonical)
        resolved["terms"] = canonical_terms
        return resolved, None

    model_term = resolved.get("term")
    if len(allowed) == 1:
        executed = allowed[0]
        if not model_term:
            resolved["term"] = executed
            return resolved, None
        parsed_model = parse_term_key(str(model_term))
        if parsed_model.kind != "single":
            observability.increment("term.guard_reject", tool=name)
            return resolved, (
                parsed_model.error.message
                if parsed_model.error is not None
                else "term must resolve to one canonical UCI term"
            )
        model_canonical = parsed_model.terms[0].canonical_name
        if model_canonical != executed:
            observability.increment("term.guard_reject", tool=name)
            observability.log_event(
                logger,
                logging.WARNING,
                "term_guard_reject",
                tool=name,
                model_term=model_canonical,
                allowed_query_terms=allowed,
            )
            return resolved, f"{model_canonical} is outside the allowed query-term scope; allowed terms: {', '.join(allowed) or 'none'}"
        resolved["term"] = model_canonical
        return resolved, None

    if not model_term:
        observability.increment("term.guard_reject", tool=name)
        return resolved, "term is required for a multi-term query"
    parsed_model = parse_term_key(str(model_term))
    if parsed_model.kind != "single":
        observability.increment("term.guard_reject", tool=name)
        return resolved, (
            parsed_model.error.message
            if parsed_model.error is not None
            else "term must resolve to one canonical UCI term"
        )
    executed = parsed_model.terms[0].canonical_name
    if executed not in allowed:
        observability.increment("term.guard_reject", tool=name)
        observability.log_event(
            logger,
            logging.WARNING,
            "term_guard_reject",
            tool=name,
            model_term=executed,
            allowed_query_terms=allowed,
        )
        return resolved, f"{executed} is outside the allowed query-term scope; allowed terms: {', '.join(allowed) or 'none'}"
    resolved["term"] = executed
    return resolved, None


def resolve_tool_arguments(name: str, args: dict, *, context: dict) -> tuple[dict, str | None]:
    """Compatibility entry point for the single authoritative term guard."""
    return enforce_query_term_scope(name, args, context=context)

_DEPARTMENT_ARGUMENT_TOOLS = {"get_department_restrictions", "search_courses"}
_COURSE_ARGUMENT_TOOLS = {"get_sections", "get_live_sections", "get_course_offerings"}


def _department_not_in_term(name: str, args: dict, context: dict) -> Optional[dict]:
    """Refuse a lookup whose department code the queried term does not have.

    Without this the call went out anyway and came back as "no notices" or
    "0 courses", which reads like an answer; the model then tried the same
    retired code on tool after tool. The refusal carries the term's roster so
    the next call can be right.
    """
    from app.catalog import term_departments

    args = args or {}
    department = None
    if name in _DEPARTMENT_ARGUMENT_TOOLS:
        department = args.get("department")
    if not department and name in _COURSE_ARGUMENT_TOOLS | {"get_department_restrictions"}:
        ref = parse_course_mention(str(args.get("course_id") or ""))
        department = ref.department if ref else None
    if not department:
        return None
    terms = args.get("terms") if name == "get_course_offerings" else [args.get("term") or context.get("term")]
    terms = [str(term) for term in terms or [] if term]
    if not terms:
        return None
    refusals = []
    for term in terms:
        code = websoc_workflow.registrar_department_code(term, str(department))
        refusal = term_departments.unknown_department(term, code)
        if refusal is None:
            # One term that has (or cannot rule out) the department is enough:
            # history lookups legitimately span terms with different codes.
            return None
        refusals.append(refusal)
    return refusals[0]


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
        observability.increment("agent.tool_failures", tool=name or "unknown")
        observability.log_event(
            logger,
            logging.WARNING,
            "agent_tool_failure",
            tool=name,
            reason="unknown_tool",
        )
        return {"error": f"unknown tool: {name}"}
    args, term_error = resolve_tool_arguments(name, args, context=context)
    if term_error:
        observability.increment("agent.tool_failures", tool=name)
        observability.log_event(
            logger,
            logging.WARNING,
            "agent_tool_failure",
            tool=name,
            reason="invalid_term",
            error=term_error,
        )
        return {"error": f"invalid term for {name}: {term_error}"}
    unknown = _department_not_in_term(name, args, context)
    if unknown:
        observability.increment("agent.tool_failures", tool=name)
        observability.log_event(
            logger,
            logging.WARNING,
            "agent_tool_failure",
            tool=name,
            reason=unknown["error_code"],
            department=unknown["department"],
            term=unknown["term"],
        )
        return unknown
    try:
        args, prefilter_error = prefilter.guard_call(name, args, context)
        if prefilter_error:
            observability.increment("prefilter.blocked_tools", tool=name)
            return prefilter_error
        sig = inspect.signature(fn)
        if "context" in sig.parameters:
            return fn(context=context, **(args or {}))
        return fn(**(args or {}))
    except TypeError as e:
        observability.increment("agent.tool_failures", tool=name)
        observability.log_event(
            logger,
            logging.WARNING,
            "agent_tool_failure",
            tool=name,
            reason="bad_arguments",
            error=str(e),
        )
        return {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:
        logger.warning("tool %s failed: %s: %s", name, type(e).__name__, e)
        observability.increment("agent.tool_failures", tool=name)
        observability.log_event(
            logger,
            logging.WARNING,
            "agent_tool_failure",
            tool=name,
            reason=type(e).__name__,
            error=str(e),
        )
        return {"error": f"{type(e).__name__}: {e}"}


def humanize_tool_call(name: str, args: dict, response_language: str = "en") -> str:
    """User-facing tool progress in the same language as this turn's answer."""
    a = args or {}
    zh = response_language == "zh"
    term = display_term(str(a.get("term") or ""), response_language)
    term_suffix = f" · {term}" if term else ""
    course_id = a.get("course_id", "")
    instructor = a.get("instructor_name", "")
    course_suffix = f" · {a['course']}" if a.get("course") else ""
    if name == "get_course_info":
        return f"查询 {course_id} 课程信息" if zh else f"Looking up {course_id} course details"
    if name == "get_course_offerings":
        terms = ", ".join(display_term(t, response_language) for t in a.get("terms") or [])
        action = "查询开课与教授" if zh else "Checking offerings and instructors"
        return f"{action} · {course_id} · {terms}"
    if name == "get_sections":
        return (f"查询 {course_id} 排课" if zh else f"Checking {course_id} sections") + term_suffix
    if name == "get_live_sections":
        action = "实时查询 WebSoc" if zh else "Checking live WebSoc"
        return f"{action} · {course_id}{term_suffix}"
    if name == "get_grade_distribution":
        return f"查询 {course_id} 历年成绩" if zh else f"Checking {course_id} grade history"
    if name == "get_zotistics_grades":
        target = " · ".join(str(value) for value in (course_id, instructor) if value)
        action = "查询 Zotistics 历史给分" if zh else "Checking Zotistics historical grades"
        return f"{action} · {target}" if target else action
    if name == "get_professor_rating":
        return f"查询教授 {instructor}" if zh else f"Looking up professor {instructor}"
    if name == "get_professor_reviews":
        action = "读取学生评论" if zh else "Reading student reviews"
        return f"{action} · {instructor}{course_suffix}"
    if name == "get_professor_tags":
        action = "汇总教授标签" if zh else "Summarizing professor tags"
        return f"{action} · {instructor}"
    if name == "summarize_professor_reviews":
        action = "提炼教授口碑" if zh else "Summarizing professor reviews"
        return f"{action} · {instructor}{course_suffix}"
    if name == "check_prerequisites_met":
        return f"检查 {course_id} 先修要求" if zh else f"Checking {course_id} prerequisites"
    if name == "search_courses":
        bits = [v for v in (a.get("department"), a.get("ge_category")) if v]
        scope = ", ".join(bits) or ("全部" if zh else "all")
        return (f"搜索课程（{scope}）" if zh else f"Searching courses ({scope})") + term_suffix
    if name == "check_section_conflict":
        first, second = a.get("course_a", ""), a.get("course_b", "")
        action = f"对比 {first} 与 {second} 时间冲突" if zh else f"Checking conflicts between {first} and {second}"
        return action + term_suffix
    if name == "get_student_profile":
        return "读取学生画像" if zh else "Reading student profile"
    if name == "update_academic_details":
        return "保存学业档案" if zh else "Saving academic details"
    if name == "get_policy":
        topic = a.get("topic")
        if not topic:
            return "列出学校政策主题" if zh else "Listing university policy topics"
        return ("查询学校政策" if zh else "Checking university policy") + f" · {topic}"
    if name == "get_department_restrictions":
        action = "读取 WebSoc 部门说明" if zh else "Reading WebSoc department notes"
        return f"{action} · {a.get('department') or ''}{term_suffix}"
    if name == "web_search":
        query = (a.get("query") or "").strip()
        action = "联网搜索" if zh else "Searching the web"
        return action + (f" · {query}" if query else "")
    if name == "fetch_page":
        url = (a.get("url") or "").strip()
        action = "深读网页" if zh else "Reading webpage"
        return action + (f" · {url}" if url else "")
    if name == "propose_recommendation":
        n = len(a.get("items") or [])
        action = f"准备 {n} 张推荐卡片" if zh else f"Preparing {n} recommendation cards"
        return action + term_suffix
    return f"调用 {name}" if zh else f"Calling {name}"
