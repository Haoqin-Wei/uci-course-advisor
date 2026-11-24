"""Courses that were renumbered or moved to another department.

UCI's degree audit lists a renamed course as "IN4MATX 43 Or SWE 43": either
ID is the same course. Requirement lists, prerequisite trees and exam-credit
charts are not all updated at once, so each consumer asks here instead of
comparing IDs directly. Add a pair when a rename is confirmed by the
catalogue or a Schedule of Classes notice.
"""

from __future__ import annotations

import re

# Each tuple is one course under all the IDs it has had.
RENAMED_COURSES: tuple[tuple[str, ...], ...] = (
    ("IN4MATX 43", "SWE 43"),      # ICS school notice, Winter 2027: IN4MATX 43 is now listed under SWE
    ("PSCI 9", "PSY 9"),           # Psychological Science became Psychology (PSCI → PSY)
)


def _key(course_id: str) -> str:
    return re.sub(r"\s+", "", str(course_id or "")).upper()


_GROUPS = {_key(course): group for group in RENAMED_COURSES for course in group}


def equivalents(course_id: str) -> tuple[str, ...]:
    """Every ID of this course, the given one first. A course never renamed is returned alone."""
    group = _GROUPS.get(_key(course_id))
    if not group:
        return (course_id,)
    given = next(course for course in group if _key(course) == _key(course_id))
    return (given, *(course for course in group if course != given))


def expand(course_ids) -> list[str]:
    """The IDs given plus every other ID of the same courses, without duplicates."""
    seen: dict[str, str] = {}
    for course_id in course_ids or []:
        for name in equivalents(course_id):
            seen.setdefault(_key(name), name)
    return list(seen.values())
