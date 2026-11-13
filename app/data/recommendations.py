"""Explicit full-pool ranking over term-scoped catalog and local snapshots.

Live grade collection is a separate step. Ranking never starts one request
per catalog course, and cache misses remain missing evidence.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Callable, Optional

from app.catalog.cache import get_catalog
from app.catalog.normalization import parse_course_mention
from app.agent.grade_evidence import normalized_query, ranking_history
from app.data import db, professors
from app.recommendations.features import build_candidate_features
from app.recommendations.ranking import rank_candidates


def _local_rating(name: str, department: str) -> dict:
    record = professors.lookup_professor(name, department=department)
    if not record:
        return {"found": False, "source": "rmp_snapshot"}
    return professors.verify_rating_identity(name, {
        "found": True, "source": "rmp_snapshot", "instructor": professors.build_profile(record),
    })


def rank_courses(
    term: str,
    department: Optional[str] = None,
    ge_category: Optional[str] = None,
    exclude_ids: Optional[list[str]] = None,
    *,
    top_k: Optional[int] = 30,
    weights: Optional[dict] = None,
    reliability: Optional[dict] = None,
    grade_lookup: Optional[Callable] = None,
    professor_lookup: Optional[Callable] = None,
    section_selector: Optional[Callable] = None,
    candidate_pool: Optional[dict] = None,
    sections_by_course: Optional[dict] = None,
) -> dict:
    """Rank the complete matching pool, then select distinct courses.

    A caller can inject verified features via lookup functions and reuse
    its existing section validator through ``section_selector``. Default
    lookup functions read only source caches and the local RMP snapshot.
    Co-taught sections keep their metadata but receive no invented joint
    professor score. This does not certify prerequisites or degree credit.
    """
    if top_k is not None and (isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 0):
        raise ValueError("top_k must be a nonnegative integer or None")
    pool = candidate_pool if candidate_pool is not None else db.retrieve_course_candidates(term, department, ge_category, exclude_ids)
    if not pool.get("found"):
        return {**pool, "ranking_enabled": True}
    catalog = get_catalog(db._to_term(term))
    if grade_lookup is None:
        from app.data.zotistics import get_cached_query_result
        grade_lookup = get_cached_query_result
    professor_lookup = professor_lookup or _local_rating
    grades_by_pair: dict[tuple, dict] = {}
    ratings_by_person: dict[tuple, dict] = {}
    candidates: list[dict] = []
    for course in pool["courses"]:
        ref = (catalog.ref_for_display(course["course_id"]) if catalog else None) or parse_course_mention(course["course_id"])
        if ref is None:
            continue  # An ID this catalog cannot identify has no sections to rank.
        sections = (sections_by_course.get(ref.display(), []) if sections_by_course is not None
                    else [db._section_record_to_dict(s) for s in catalog.get_sections(ref)])
        if section_selector is not None:
            primaries = section_selector(sections)
        else:
            primaries = [s for s in sections if not s.get("is_cancelled")
                         and s.get("section_code")
                         and (s.get("section_type") or "").lower().startswith(("lec", "sem"))]
        if candidate_pool is not None and not primaries:
            # Dynamic prefilter deliberately retains unknown eligibility and
            # unpublished sections. Keep these as unranked course evidence;
            # lack of a score must not silently turn into an exclusion.
            candidates.append({**course, "term": pool["term"], "section": None,
                               "section_code": None, "instructor_name": None, "instructors": [],
                               "zotistics_grades": None,
                               "features": build_candidate_features(course_id=ref.display())})
        for primary in primaries:
            names = [n for n in primary.get("instructors", [])
                     if n and n.upper() not in {"STAFF", "TBA", "TBD"}]
            if not str(primary.get("section_type") or "").lower().startswith(("lec", "sem")):
                names = []
            name = names[0] if len(names) == 1 else None
            grade_data = rating_data = None
            if name:
                key = (ref.display(), name.upper(), ref.department)
                if key not in grades_by_pair:
                    result = grade_lookup(course_id=ref.display(), instructor_name=name,
                                          department=ref.department)
                    if result.get("status") == "no_data" and result.get("scope") == "course_instructor":
                        fallback = grade_lookup(instructor_name=result.get("resolved_instructor_name") or name)
                        if fallback.get("found") and fallback.get("scope") == "instructor":
                            result = {**fallback, "fallback_from_scope": "course_instructor",
                                      "pair_query": result.get("query") or normalized_query(ref.display(), name, ref.department),
                                      "requested_course_id": ref.display()}
                    grades_by_pair[key] = result
                grade_data = grades_by_pair[key]
                person = (name.upper(), ref.department)
                if person not in ratings_by_person:
                    ratings_by_person[person] = professor_lookup(name, ref.department)
                rating_data = ratings_by_person[person]
            candidates.append({
                **course, "term": pool["term"], "section": primary,
                "section_code": primary.get("section_code"),
                "instructor_name": name, "instructors": names,
                "zotistics_grades": grade_data,
                "features": build_candidate_features(
                    ranking_history(grade_data), rating_data, course_id=ref.display(),
                    instructor_name=name, primary_section=primary,
                ),
            })
    # Score slim rows: rank_candidates deep-copies its input, and a full-term
    # pool carries every section with its parent notices. Only the rows that
    # are returned get the same independent copy as before.
    ranked = rank_candidates(
        [{"_index": index, "features": candidate["features"]} for index, candidate in enumerate(candidates)],
        weights=weights, reliability=reliability,
    )
    courses: list[tuple[int, dict]] = []
    seen: set[str] = set()
    for row in ranked:
        course_id = candidates[row["_index"]]["course_id"]
        if course_id not in seen:
            seen.add(course_id)
            courses.append((row["_index"], row["ranking"]))
    chosen = courses if top_k is None else courses[:top_k]
    selected = [{**deepcopy(candidates[index]), "ranking": ranking} for index, ranking in chosen]
    courses = [{"ranking": ranking} for _, ranking in courses]  # coverage counts below
    return {**pool, "courses": selected, "ranking_enabled": True,
            "total_found": len(courses), "catalog_candidates": len(pool["courses"]),
            "scored_candidates": len(candidates), "truncated": len(selected) < len(courses),
            "feature_coverage": {
                "scored": sum(c["ranking"]["final_score"] is not None for c in courses),
                "unranked": sum(c["ranking"]["final_score"] is None for c in courses),
            },
            "ranking_scope": "complete_matching_catalog_with_current_primary_sections",
            "grade_data_mode": "snapshot_or_cache_only"}
