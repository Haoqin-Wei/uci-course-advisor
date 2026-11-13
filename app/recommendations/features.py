"""Adapt supplied grade/RMP snapshots without fetching or inferring instructors."""
from __future__ import annotations

from collections.abc import Mapping

from app.recommendations.ranking import _number


def _key(value) -> str:
    return "".join(str(value or "").upper().split()).replace("_", "").replace(".", "")


def _missing(reason: str, **metadata) -> dict:
    return {"value": None, "sample_count": None, "missing_reason": reason, **metadata}


def _source_instructor_matches(candidate, source_instructor, data: Mapping) -> bool:
    if not candidate or not source_instructor:
        return False
    if _key(candidate) == _key(source_instructor):
        return True
    query = data.get("query")
    query = query if isinstance(query, Mapping) else {}
    requested = query.get("instructor_name")
    resolved = data.get("resolved_instructor_name")
    # Only the source adapter's explicitly verified request -> resolved
    # association bridges full names and initials; never infer such aliases.
    return bool(requested and resolved and _key(candidate) == _key(requested)
                and _key(source_instructor) == _key(resolved))


def _primary_instructor(instructor_name, primary_section) -> tuple:
    if primary_section is None:
        return instructor_name, None
    if not isinstance(primary_section, Mapping) or not str(primary_section.get("section_type") or primary_section.get("type") or "").lower().startswith(("lec", "sem")):
        return instructor_name, "not_a_primary_instructor"
    names = primary_section.get("instructors") or []
    if instructor_name is None and len(names) == 1:
        instructor_name = names[0]
    if not instructor_name or _key(instructor_name) not in {_key(name) for name in names}:
        return instructor_name, "instructor_not_in_primary_section"
    return instructor_name, None


def _grade_features(data, course_id, instructor_name, primary_section) -> dict:
    if not isinstance(data, Mapping):
        return _missing("grade_data_missing", scope="unknown")
    grades = data.get("grades", data)
    metadata = {key: data.get(key) for key in ("source", "source_url") if data.get(key) is not None}
    if data.get("found") is False or data.get("status") not in (None, "ok") or not isinstance(grades, Mapping):
        return _missing(str(data.get("status") or "grade_data_unavailable"), **metadata, scope=data.get("scope", "unknown"))
    source_course = grades.get("course_id") or data.get("course_id")
    source_instructor = grades.get("instructor_name") or data.get("instructor_name")
    scope = data.get("scope") or grades.get("scope") or ("course_instructor" if source_course and source_instructor else "course" if source_course else "unknown")
    metadata.update({"scope": scope, "course_id": source_course, "instructor_name": source_instructor})
    if scope in {"course_instructor", "instructor"}:
        instructor_name, primary_reason = _primary_instructor(instructor_name, primary_section)
        if primary_reason:
            return _missing(primary_reason, **metadata)
    if course_id and source_course and _key(course_id) != _key(source_course):
        return _missing("grade_course_mismatch", **metadata)
    if course_id and scope == "instructor":
        fallback_from = data.get("fallback_from_scope") or grades.get("fallback_from_scope")
        requested_course = data.get("requested_course_id") or grades.get("requested_course_id")
        if (fallback_from != "course_instructor" or not requested_course
                or _key(requested_course) != _key(course_id)
                or not _source_instructor_matches(instructor_name, source_instructor, data)):
            return _missing("instructor_history_not_course_scoped", **metadata)
        metadata.update({"fallback": True, "fallback_from_scope": fallback_from,
                         "requested_course_id": requested_course,
                         "uncertain_reason": "instructor_wide_fallback"})
    if scope == "course_instructor" and not _source_instructor_matches(instructor_name, source_instructor, data):
        return _missing("grade_instructor_mismatch", **metadata)
    # A course aggregate remains explicitly course-scoped even when the
    # candidate also carries a professor; it is never relabeled professor data.
    if scope == "course":
        metadata["instructor_name"] = None
    counts = grades.get("letter_counts")
    counts = counts if isinstance(counts, Mapping) else {}
    a_count = counts.get("A", grades.get("gradeACount"))
    graded = grades.get("graded")
    if graded is None:
        all_counts = [counts.get(letter, grades.get(f"grade{letter}Count")) for letter in ("A", "B", "C", "D", "F")]
        if all(_number(value) is not None and _number(value) >= 0 for value in all_counts):
            graded = sum(float(value) for value in all_counts)
    value = None
    if a_count is not None:
        count, n = _number(a_count), _number(graded)
        if count is None or count < 0:
            return _missing("invalid_a_count", **metadata)
        if graded is None:
            return _missing("missing_sample_count", **metadata)
        if n is None:
            return _missing("invalid_sample_count", sample_count=graded, **metadata)
        if n <= 0:
            return _missing("nonpositive_sample_count", sample_count=graded, **metadata)
        if count > n:
            return _missing("inconsistent_grade_counts", sample_count=graded, **metadata)
        value = count / n
    else:
        percentages = grades.get("letter_pct")
        percentages = percentages if isinstance(percentages, Mapping) else {}
        pct = grades.get("pct_A", percentages.get("A"))
        numeric = _number(pct)
        if pct is not None and (numeric is None or not 0 <= numeric <= 100):
            return _missing("invalid_a_percentage", **metadata)
        if numeric is not None:
            value = numeric / 100.0
    return {"value": value, "sample_count": graded, **metadata}


def _professor_features(data, instructor_name, primary_section) -> dict:
    instructor_name, primary_reason = _primary_instructor(instructor_name, primary_section)
    if primary_reason:
        return _missing(primary_reason, scope="instructor", instructor_name=instructor_name)
    if not isinstance(data, Mapping):
        return _missing("professor_data_missing", scope="instructor", instructor_name=instructor_name)
    profile = data.get("instructor", data)
    metadata = {key: data.get(key) for key in ("source", "source_url") if data.get(key) is not None}
    metadata.update({"scope": "instructor", "instructor_name": instructor_name})
    if data.get("found") is False or data.get("status") not in (None, "ok") or not isinstance(profile, Mapping):
        return _missing(str(data.get("status") or "professor_data_unavailable"), **metadata)
    source_name = data.get("instructor_name") or profile.get("instructor_name")
    if instructor_name and source_name and not _source_instructor_matches(instructor_name, source_name, data):
        return _missing("professor_instructor_mismatch", **metadata)
    return {"value": profile.get("avg_rating"), "sample_count": profile.get("num_ratings"), **metadata}


def build_candidate_features(
    grade_data: Mapping | None = None,
    professor_data: Mapping | None = None,
    *,
    course_id: str | None = None,
    instructor_name: str | None = None,
    primary_section: Mapping | None = None,
) -> dict:
    """Return scoring features from envelopes or inner source records.

    Counts A/graded take precedence over 0–100 pct_A; P/NP/W samples never
    substitute for graded. Caller supplies the selected lecturer's RMP data;
    primary_section, when supplied, verifies lecture/seminar membership.
    Source GPA is intentionally unused. Identifiers must share canonical form.
    Instructor-wide fallback requires explicit matching requested-course and
    instructor metadata; it stays instructor-scoped and flagged as uncertain.
    """
    return {
        "grade_a_rate": _grade_features(grade_data, course_id, instructor_name, primary_section),
        "professor_rating": _professor_features(professor_data, instructor_name, primary_section),
    }
