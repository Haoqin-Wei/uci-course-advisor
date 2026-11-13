"""Request-scoped, source-attributed historical grade evidence.

Only confirmed empty professor-course results permit the all-course
fallback. Transport failures and unresolved names do not establish absence.
"""
from __future__ import annotations

import re

from app.catalog.departments import resolve_department
from app.catalog.normalization import normalize_instructor, parse_course_mention
from app.data import db


def normalized_query(course_id=None, instructor_name=None, department=None) -> dict:
    ref = parse_course_mention(course_id or "")
    return {
        "course_id": ref.display() if ref else " ".join((course_id or "").upper().split()) or None,
        "instructor_name": normalize_instructor(instructor_name or "") or None,
        "department": resolve_department(department or "")
        or " ".join((department or "").upper().split())
        or (ref.department if ref else None),
    }


def _same_person(name, record) -> bool:
    requested = record["query"].get("instructor_name")
    if not name:
        return requested is None
    key = lambda value: re.sub(r"[.\s]+", "", normalize_instructor(value or ""))
    aliases = [requested, record["result"].get("resolved_instructor_name")]
    return any(alias and key(name) == key(alias) for alias in aliases)


def _query(context, course_id=None, instructor_name=None, department=None) -> dict:
    query = normalized_query(course_id, instructor_name, department)
    results = context.setdefault("_zotistics_results", [])
    for previous in results:
        previous_query = previous["query"]
        if (previous_query["course_id"] == query["course_id"]
                and previous_query["department"] == query["department"]
                and _same_person(query["instructor_name"], previous)):
            return previous["result"]
    result = db.get_zotistics_grades(course_id=course_id,
                                   instructor_name=instructor_name, department=department)
    results.append({"query": query, "result": result})
    return result


def get_history(context, course_id=None, instructor_name=None, department=None) -> dict:
    result = _query(context, course_id, instructor_name, department)
    if (course_id and instructor_name and result.get("status") == "no_data"
            and result.get("scope") == "course_instructor"):
        canonical_name = result.get("resolved_instructor_name") or instructor_name
        fallback = _query(context, instructor_name=canonical_name)
        if fallback.get("found") and fallback.get("scope") == "instructor":
            return {**fallback, "fallback_from_scope": "course_instructor",
                    "requested_course_id": normalized_query(course_id)["course_id"],
                    "pair_query": result.get("query") or normalized_query(course_id, instructor_name, department),
                    "pair_result": {"status": "no_data", "query": result.get("query")}}
    return result


def _is_success(result: dict) -> bool:
    return (result.get("found") is True and result.get("source") == "zotistics"
            and result.get("status", "ok") == "ok" and isinstance(result.get("grades"), dict))


def ranking_history(history):
    """Canonicalize source aliases for validation without altering display data."""
    if not isinstance(history, dict):
        return history
    grades = dict(history.get("grades") or {})
    for key in ("course_id",):
        ref = parse_course_mention(grades.get(key) or "")
        if ref:
            grades[key] = ref.display()
    result = {**history, "grades": grades}
    ref = parse_course_mention(history.get("requested_course_id") or "")
    if ref:
        result["requested_course_id"] = ref.display()
    if history.get("fallback_from_scope") == "course_instructor" and history.get("pair_query"):
        result["query"] = dict(history["pair_query"])
    return result


def find_history(context, course_id=None, instructor_name=None, department=None, *, include_unavailable=False):
    """Read previously queried evidence, keeping course/person scopes separate."""
    query = normalized_query(course_id, instructor_name, department)
    pair_missing = None
    unavailable = None
    for record in context.get("_zotistics_results", []):
        previous = record["query"]
        if previous["course_id"] != query["course_id"] or not _same_person(query["instructor_name"], record):
            continue
        if query["department"] and previous.get("department") not in {None, query["department"]}:
            continue
        result = record["result"]
        expected_scope = "course_instructor" if instructor_name else "course"
        if _is_success(result) and result.get("scope") == expected_scope:
            return result
        if result.get("source") == "zotistics" and result.get("scope") == expected_scope:
            unavailable = result
        if result.get("status") == "no_data" and result.get("scope") == "course_instructor":
            pair_missing = result
    if pair_missing and instructor_name:
        canonical = pair_missing.get("resolved_instructor_name") or instructor_name
        person = normalized_query(instructor_name=canonical)
        for record in context.get("_zotistics_results", []):
            if (record["query"]["course_id"] is None and record["query"]["department"] is None
                    and _same_person(person["instructor_name"], record)
                    and _is_success(record["result"]) and record["result"].get("scope") == "instructor"):
                return {**record["result"], "fallback_from_scope": "course_instructor",
                        "requested_course_id": query["course_id"],
                        "pair_query": pair_missing.get("query") or query}
    return unavailable if include_unavailable else None


def add_chart(context, result, course_id=None, instructor_name=None) -> None:
    if result.get("source") != "zotistics":
        return
    charts = context.setdefault("_grade_charts", [])
    display_name = result.get("resolved_instructor_name") or instructor_name or result.get("instructor_name")
    identity = (normalized_query(course_id)["course_id"], normalize_instructor(display_name or ""))
    if any((chart.get("course_id"), normalize_instructor(chart.get("instructor") or "")) == identity
           for chart in charts):
        return
    charts.append({**result, "course_id": identity[0],
                   "instructor": display_name})


def attach_history(card: dict, context: dict) -> None:
    """Attach only a primary lecturer's pair (or verified empty-pair fallback)."""
    course_id, department = card["course_id"], card.get("department")
    card["zotistics_grades"] = find_history(context, course_id, department=department, include_unavailable=True)
    for group in card.get("section_groups", []):
        primary = group.get("primary")
        if not isinstance(primary, dict):
            continue
        primary["grade_history"] = []
        if not str(primary.get("type") or primary.get("section_type") or "").lower().startswith(("lec", "sem")):
            continue
        for name in primary.get("instructors", []):
            history = find_history(context, course_id, name, department, include_unavailable=True)
            if history:
                primary["grade_history"].append({"instructor": name, **history})


def prepare_recommendation_grades(items: list[dict], term: str, context: dict,
                                  section_selector) -> list[dict]:
    """Collect current primary-pair evidence before card construction, once."""
    reports = []
    sections_cache = context.setdefault("_recommendation_sections", {})
    for item in (items or [])[:8]:
        course_id = item.get("course_id") or ""
        ref = parse_course_mention(course_id)
        if not ref:
            continue
        cache_key = (ref.display(), term)
        if cache_key not in sections_cache:
            sections_cache[cache_key] = db.get_sections(course_id, term)
        section_result = sections_cache[cache_key]
        sections = section_result.get("sections", []) if section_result.get("found") else []
        primaries = section_selector(sections)
        names = list(dict.fromkeys(name for section in primaries
                                   if str(section.get("section_type") or "").lower().startswith(("lec", "sem"))
                                   for name in section.get("instructors", [])
                                   if name and name.upper() not in {"STAFF", "TBA", "TBD"}))
        for name in names:
            result = get_history(context, course_id, name, ref.department)
            reports.append({"course_id": ref.display(), "instructor": name, **result})
    return reports
