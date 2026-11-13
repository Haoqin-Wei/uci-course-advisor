"""Small, lossless WebSoc payload helpers shared by source adapters.

These helpers preserve source evidence. They do not interpret comments as
course equivalence, enrollment permission, or rules for unrelated courses.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from app.catalog.departments import resolve_department
from app.catalog.normalization import iter_course_mentions


def course_comment_context(school: dict, department: dict, course: dict) -> dict:
    """Copy every parent comment, keeping its original scope and range text."""
    return deepcopy({
        "school": {
            "name": school.get("schoolName"),
            "comment": school.get("schoolComment"),
        },
        "department": {
            "code": department.get("deptCode"),
            "name": department.get("deptName"),
            "comment": department.get("deptComment"),
            "course_number_range_comments": department.get("courseNumberRangeComments"),
            "section_code_range_comments": department.get("sectionCodeRangeComments"),
        },
        "course": {
            "course_id": course.get("courseId"),
            "title": course.get("courseTitle"),
            "comment": course.get("courseComment"),
        },
    })


def cancellation_fields(value: Any) -> dict:
    """Keep source booleans, and distinguish absent/invalid evidence from False.

    In particular, bool("false") is True in Python and must not be used to
    interpret a source flag. Seat statuses such as FULL are independent of
    whether a section was cancelled.
    """
    cancelled = value if isinstance(value, bool) else None
    return {
        "is_cancelled": cancelled,
        "cancellation_status": (
            "cancelled" if cancelled is True
            else "not_cancelled" if cancelled is False
            else "unknown"
        ),
    }


def _register_comment_context(context: dict, contexts: dict) -> str:
    fingerprint = hashlib.sha256(
        json.dumps(context, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    context_id = f"comments_{fingerprint[:16]}"
    # Preserve unrelated pre-existing contexts even in a hash collision.
    if context_id in contexts and contexts[context_id] != context:
        context_id = f"comments_{fingerprint}"
    contexts[context_id] = deepcopy(context)
    return context_id


def websoc_comment_metadata(data: dict) -> dict:
    """Collect parent notices independently of whether any sections exist.

    The containing response supplies source and term provenance. Scope-only
    entries also retain school/department notices in otherwise empty branches.
    """
    contexts: dict = {}
    notices: list[dict] = []

    def retain(school, department, course, scope):
        context = course_comment_context(school, department, course)
        # Empty structural branches without any source notice need no extra
        # envelope fields. Section-bearing branches still keep their complete
        # context on each section, including explicitly blank source comments.
        comment_values = [context[level]["comment"] for level in ("school", "department", "course")]
        comment_values += [context["department"][key] for key in (
            "course_number_range_comments", "section_code_range_comments",
        )]
        if not any(value for value in comment_values):
            return
        notices.append({
            "scope": scope,
            "department": department.get("deptCode"),
            "course_id": course.get("courseId"),
            "title": course.get("courseTitle"),
            "comment_context_id": _register_comment_context(context, contexts),
        })

    for school in data.get("schools") or []:
        if not isinstance(school, dict):
            continue
        departments = [item for item in school.get("departments") or [] if isinstance(item, dict)]
        if not departments:
            retain(school, {}, {}, "school")
        for department in departments:
            courses = [item for item in department.get("courses") or [] if isinstance(item, dict)]
            if not courses:
                retain(school, department, {}, "department")
            for course in courses:
                retain(school, department, course, "course")
    return {"comment_contexts": contexts, "course_notices": notices}


def compact_comment_contexts(result: dict) -> dict:
    """Deduplicate scoped comments for tool output without changing evidence.

    Data-layer sections retain their full ``comments`` context for storage and
    reuse. Tools may call this on their final envelope so large school and
    department notices are not repeated once per section in the model input.
    A section's own comment remains on that section, and range notices retain
    their scope and original text. Known department abbreviations in notices
    gain separate normalization evidence; notice applicability is not inferred.
    """
    compacted = deepcopy(result)
    existing = compacted.get("comment_contexts")
    contexts = existing if isinstance(existing, dict) else {}

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            # This field is derived by this helper, never accepted from source
            # data or a previously compacted nested envelope.
            value.pop("course_code_normalizations", None)
            context = value.get("comments")
            if isinstance(context, dict) and any(
                scope in context for scope in ("school", "department", "course")
            ):
                value.pop("comments")
                value["comment_context_id"] = _register_comment_context(context, contexts)
            for key, child in list(value.items()):
                if key != "comment_contexts":
                    visit(child)

    # Search tools nest sections under courses, while cards also have nested
    # primary/secondary sections. All share the same outer evidence bank.
    visit(compacted)
    if contexts:
        compacted["comment_contexts"] = contexts
    normalizations = _comment_code_normalizations(compacted)
    if normalizations:
        compacted["course_code_normalizations"] = normalizations
    return compacted


def _comment_code_normalizations(result: dict) -> list[dict]:
    """Expose existing code spelling rules, without interpreting notice policy.

    Each JSON pointer identifies the original, unchanged comment supplying the
    spelling. In particular INF -> IN4MATX is an existing department alias;
    this helper does not establish any link from IN4MATX to SWE, enrollment
    permission, a credit consequence, or the effective date of a change.
    """
    notes: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def pointer(parent: str, key: Any) -> str:
        return parent + "/" + str(key).replace("~", "~0").replace("/", "~1")

    def exact_spelling(spelling: str, ref) -> bool:
        # The shared free-text parser also tolerates unrelated words after a
        # known department ("ICS COURSES 5"). Evidence requires a complete,
        # exact department spelling directly followed by the course number.
        return spelling.upper().endswith(ref.course_number) and resolve_department(
            spelling[:-len(ref.course_number)].strip()
        ) == ref.department

    def scan(value: Any, path: str) -> None:
        if isinstance(value, list):
            for index, child in enumerate(value):
                scan(child, pointer(path, index))
        elif isinstance(value, str):
            for ref, start, end in iter_course_mentions(value):
                spelling = value[start:end]
                # The shared matcher may accept a leading word via its
                # documented fallback ("for INF 43"). Trim only when the suffix
                # has an exact known department spelling and course number.
                while len(parts := spelling.split(maxsplit=1)) == 2:
                    tail = spelling[len(parts[0]):].lstrip()
                    if not exact_spelling(tail, ref):
                        break
                    spelling = tail
                if not exact_spelling(spelling, ref):
                    continue
                normalized = ref.display()
                if "".join(spelling.split()).upper() == "".join(normalized.split()):
                    continue
                key = (path, spelling, normalized)
                if key not in seen:
                    seen.add(key)
                    notes.append({
                        "source_text": spelling,
                        "normalized_course_id": normalized,
                        "basis": "existing_department_alias_rules",
                        "source_path": path,
                    })

    def visit(value: Any, path: str) -> None:
        if isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, pointer(path, index))
        elif isinstance(value, dict):
            for key, child in value.items():
                child_path = pointer(path, key)
                if key == "comment_contexts" and isinstance(child, dict):
                    for context_id, context in child.items():
                        if not isinstance(context, dict):
                            continue
                        context_path = pointer(child_path, context_id)
                        for scope in ("school", "department", "course"):
                            scoped = context.get(scope)
                            if not isinstance(scoped, dict):
                                continue
                            scope_path = pointer(context_path, scope)
                            scan(scoped.get("comment"), pointer(scope_path, "comment"))
                            if scope == "department":
                                for field in ("course_number_range_comments", "section_code_range_comments"):
                                    scan(scoped.get(field), pointer(scope_path, field))
                elif key == "section_comment":
                    scan(child, child_path)
                elif key != "course_code_normalizations":
                    visit(child, child_path)

    visit(result, "")
    return notes
