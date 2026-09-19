"""Prerequisite tree evaluator.

The UCI course dump stores prerequisite logic as nested JSON:
AND / OR / NOT groups plus leaves such as
{"prereqType": "course", "courseId": "I&C SCI 33", "minGrade": "C"}.

This module evaluates that tree against a student's completed and
in-progress courses and returns a tri-state result:
met / not_met / unknown. Unknown is intentionally first-class; missing
grades, exams, malformed leaves, and missing tree data must not be
silently treated as satisfied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.catalog.normalization import parse_course_mention
from app.catalog.types import CourseRef


STATUS_MET = "met"
STATUS_NOT_MET = "not_met"
STATUS_UNKNOWN = "unknown"

_STATUS_ORDER = {
    STATUS_MET: 0,
    STATUS_UNKNOWN: 1,
    STATUS_NOT_MET: 2,
}

_GRADE_POINTS = {
    "A+": 12,
    "A": 12,
    "A-": 11,
    "B+": 10,
    "B": 9,
    "B-": 8,
    "C+": 7,
    "C": 6,
    "C-": 5,
    "D+": 4,
    "D": 3,
    "D-": 2,
    "F": 1,
    "NP": 0,
}

_GRADE_RE = re.compile(r"^(A\+?|A-|B\+?|B-|C\+?|C-|D\+?|D-|F|NP|P)$", re.I)


@dataclass
class CourseAttempt:
    ref: CourseRef
    grade: Optional[str] = None


@dataclass
class EvalResult:
    status: str
    missing: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    satisfied: list[str] = field(default_factory=list)
    in_progress_used: list[str] = field(default_factory=list)

    @property
    def met(self) -> bool:
        return self.status == STATUS_MET


@dataclass
class PrerequisiteContext:
    completed: dict[CourseRef, CourseAttempt]
    in_progress: dict[CourseRef, CourseAttempt]
    allow_in_progress: bool = True


def evaluate_prerequisite_tree(
    tree: Any,
    *,
    completed_courses: Optional[list[Any]] = None,
    in_progress_courses: Optional[list[Any]] = None,
    flat_prerequisites: Optional[list[str]] = None,
    prerequisite_text: Optional[str] = None,
    allow_in_progress: bool = True,
) -> EvalResult:
    """Evaluate a prerequisite tree and return met/not_met/unknown.

    `flat_prerequisites` is only used as a compatibility fallback when
    the structured tree is absent. If the catalog has prerequisite text
    but neither a tree nor flat refs, the result is unknown.
    """

    ctx = PrerequisiteContext(
        completed=_attempts_by_ref(completed_courses or []),
        in_progress=_attempts_by_ref(in_progress_courses or []),
        allow_in_progress=allow_in_progress,
    )

    if _has_structured_tree(tree):
        return _dedupe_result(_eval_node(tree, ctx))

    flat_refs = [ref for ref in (_parse_ref(p) for p in (flat_prerequisites or [])) if ref]
    if flat_refs:
        fallback_tree = {"AND": [
            {"prereqType": "course", "coreq": False, "courseId": ref.display()}
            for ref in flat_refs
        ]}
        return _dedupe_result(_eval_node(fallback_tree, ctx))

    if (prerequisite_text or "").strip():
        return EvalResult(
            STATUS_UNKNOWN,
            unknown=["prerequisite text exists but no structured prerequisite tree is available"],
        )

    return EvalResult(STATUS_MET)


def summarize_node(node: Any) -> str:
    if isinstance(node, dict):
        if "AND" in node:
            return " and ".join(summarize_node(child) for child in _children(node["AND"]))
        if "OR" in node:
            return "one of: " + " / ".join(summarize_node(child) for child in _children(node["OR"]))
        if "NOT" in node:
            return "must not have: " + " / ".join(summarize_node(child) for child in _children(node["NOT"]))
        prereq_type = (node.get("prereqType") or "").lower()
        if prereq_type == "course":
            return _course_label(node)
        if prereq_type == "exam":
            return _exam_label(node)
    return "unrecognized prerequisite"


def _eval_node(node: Any, ctx: PrerequisiteContext) -> EvalResult:
    if isinstance(node, dict):
        if "AND" in node:
            return _eval_and(_children(node["AND"]), ctx)
        if "OR" in node:
            return _eval_or(_children(node["OR"]), ctx)
        if "NOT" in node:
            return _eval_not(_children(node["NOT"]), ctx)

        prereq_type = (node.get("prereqType") or "").lower()
        if prereq_type == "course":
            return _eval_course_leaf(node, ctx)
        if prereq_type == "exam":
            return EvalResult(
                STATUS_UNKNOWN,
                unknown=[f"{_exam_label(node)} cannot be verified from the student profile"],
            )

    return EvalResult(
        STATUS_UNKNOWN,
        unknown=[f"unrecognized prerequisite node: {node!r}"],
    )


def _eval_and(children: list[Any], ctx: PrerequisiteContext) -> EvalResult:
    if not children:
        return EvalResult(STATUS_MET)

    parts = [_eval_node(child, ctx) for child in children]
    out = _merge_parts(parts)
    if any(part.status == STATUS_NOT_MET for part in parts):
        out.status = STATUS_NOT_MET
    elif any(part.status == STATUS_UNKNOWN for part in parts):
        out.status = STATUS_UNKNOWN
    else:
        out.status = STATUS_MET
    return out


def _eval_or(children: list[Any], ctx: PrerequisiteContext) -> EvalResult:
    if not children:
        return EvalResult(
            STATUS_UNKNOWN,
            unknown=["empty OR prerequisite branch"],
        )

    parts = [_eval_node(child, ctx) for child in children]
    met_parts = [part for part in parts if part.status == STATUS_MET]
    if met_parts:
        return _merge_parts(met_parts, status=STATUS_MET)

    out = _merge_parts(parts)
    if any(part.status == STATUS_UNKNOWN for part in parts):
        out.status = STATUS_UNKNOWN
        if not out.unknown:
            out.unknown.append("one prerequisite alternative could not be verified")
    else:
        out.status = STATUS_NOT_MET
        out.missing = ["one of: " + " / ".join(summarize_node(child) for child in children)]
    return out


def _eval_not(children: list[Any], ctx: PrerequisiteContext) -> EvalResult:
    if not children:
        return EvalResult(STATUS_MET)

    parts = [_eval_node(child, ctx) for child in children]
    met = [summarize_node(child) for child, part in zip(children, parts) if part.status == STATUS_MET]
    if met:
        return EvalResult(
            STATUS_NOT_MET,
            missing=[f"must not already have credit for {item}" for item in met],
        )
    if any(part.status == STATUS_UNKNOWN for part in parts):
        out = _merge_parts(parts, status=STATUS_UNKNOWN)
        if not out.unknown:
            out.unknown.append("cannot verify exclusion prerequisite")
        return out
    return EvalResult(STATUS_MET)


def _eval_course_leaf(node: dict, ctx: PrerequisiteContext) -> EvalResult:
    ref = _parse_ref(node.get("courseId"))
    if not ref:
        return EvalResult(
            STATUS_UNKNOWN,
            unknown=[f"could not parse prerequisite course {node.get('courseId')!r}"],
        )

    label = _course_label(node)
    min_grade = _normalize_grade(node.get("minGrade"))
    attempt = ctx.completed.get(ref)
    if attempt:
        return _evaluate_attempt(attempt, label=label, min_grade=min_grade)

    in_progress_attempt = ctx.in_progress.get(ref)
    if in_progress_attempt and (ctx.allow_in_progress or node.get("coreq") is True):
        if min_grade:
            return EvalResult(
                STATUS_UNKNOWN,
                unknown=[f"{label} is in progress; final grade is needed to verify minimum {min_grade}"],
                in_progress_used=[ref.display()],
            )
        return EvalResult(
            STATUS_MET,
            satisfied=[label],
            in_progress_used=[ref.display()],
        )

    return EvalResult(STATUS_NOT_MET, missing=[label])


def _evaluate_attempt(
    attempt: CourseAttempt,
    *,
    label: str,
    min_grade: Optional[str],
) -> EvalResult:
    if not min_grade:
        return EvalResult(STATUS_MET, satisfied=[label])

    if not attempt.grade:
        return EvalResult(
            STATUS_UNKNOWN,
            unknown=[f"{label} completed, but recorded grade is missing for minimum {min_grade}"],
        )

    if _grade_meets(attempt.grade, min_grade):
        return EvalResult(STATUS_MET, satisfied=[label])

    return EvalResult(
        STATUS_NOT_MET,
        missing=[f"{label}; recorded grade {attempt.grade} is below minimum {min_grade}"],
    )


def _merge_parts(parts: Iterable[EvalResult], *, status: Optional[str] = None) -> EvalResult:
    parts = list(parts)
    if status is None:
        status = max((part.status for part in parts), key=lambda s: _STATUS_ORDER.get(s, 1))
    out = EvalResult(status)
    for part in parts:
        out.missing.extend(part.missing)
        out.unknown.extend(part.unknown)
        out.satisfied.extend(part.satisfied)
        out.in_progress_used.extend(part.in_progress_used)
    return _dedupe_result(out)


def _dedupe_result(result: EvalResult) -> EvalResult:
    result.missing = _dedupe(result.missing)
    result.unknown = _dedupe(result.unknown)
    result.satisfied = _dedupe(result.satisfied)
    result.in_progress_used = _dedupe(result.in_progress_used)
    return result


def _dedupe(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _children(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _has_structured_tree(tree: Any) -> bool:
    return isinstance(tree, dict) and bool(tree)


def _parse_ref(value: Any) -> Optional[CourseRef]:
    if not isinstance(value, str):
        return None
    return parse_course_mention(value)


def _attempts_by_ref(entries: list[Any]) -> dict[CourseRef, CourseAttempt]:
    attempts: dict[CourseRef, CourseAttempt] = {}
    for entry in entries:
        attempt = _parse_attempt(entry)
        if attempt:
            attempts[attempt.ref] = attempt
    return attempts


def _parse_attempt(entry: Any) -> Optional[CourseAttempt]:
    if isinstance(entry, dict):
        course_id = (
            entry.get("course_id")
            or entry.get("courseId")
            or entry.get("id")
            or entry.get("course")
        )
        ref = _parse_ref(course_id)
        if not ref:
            return None
        return CourseAttempt(ref=ref, grade=_normalize_grade(entry.get("grade")))

    if not isinstance(entry, str):
        return None

    text = entry.strip()
    grade = None
    grade_match = re.search(
        r"(?:\(|:|\bgrade\s+)(A\+?|A-|B\+?|B-|C\+?|C-|D\+?|D-|F|NP|P)\)?\s*$",
        text,
        re.I,
    )
    if grade_match:
        grade = _normalize_grade(grade_match.group(1))
        text = text[:grade_match.start()].strip(" :-()")

    ref = _parse_ref(text)
    if not ref:
        return None
    return CourseAttempt(ref=ref, grade=grade)


def _normalize_grade(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    match = _GRADE_RE.fullmatch(text)
    return match.group(1).upper() if match else text


def _grade_meets(grade: str, minimum: str) -> bool:
    grade_norm = _normalize_grade(grade)
    minimum_norm = _normalize_grade(minimum)
    if not grade_norm or not minimum_norm:
        return False
    if grade_norm == "P":
        return True
    if minimum_norm == "P":
        return grade_norm == "P"
    return _GRADE_POINTS.get(grade_norm, -1) >= _GRADE_POINTS.get(minimum_norm, 99)


def _course_label(node: dict) -> str:
    course_id = str(node.get("courseId") or "unknown course").strip()
    bits = [course_id]
    if node.get("minGrade"):
        bits.append(f"min {node.get('minGrade')}")
    if node.get("coreq"):
        bits.append("coreq allowed")
    return " (".join([bits[0], "; ".join(bits[1:]) + ")"]) if len(bits) > 1 else bits[0]


def _exam_label(node: dict) -> str:
    exam = str(node.get("examName") or "exam").strip()
    if node.get("minGrade"):
        return f"{exam} score {node.get('minGrade')}+"
    return exam
