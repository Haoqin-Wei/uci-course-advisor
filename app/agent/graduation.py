"""Wires the graduation path into a recommendation turn.

When the student asks what to take in a term, the server works out the
degree-path tray once (program requirements, recorded courses, this term's
takeable courses). The model picks inside that tray and the server checks
the picks, instead of the model searching department listings.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from app import observability
from app.catalog.ge import parse_ge_categories
from app.recommendations.filter_plan import empty_filter_plan
from app.recommendations.graduation_path import build_graduation_path, compact

logger = logging.getLogger(__name__)

_catalogue_cache: tuple[Optional[tuple], dict[str, dict]] = (None, {})


def path_blocker(plan: dict, query: str) -> Optional[str]:
    """Why this request does not take the graduation path; None when it does.

    The path is the default. A request leaves it only on a signal the server
    can establish itself: a GE-only scope the validator matched to the
    student's words, or a department or course named in the message. A time
    condition does not change the kind: the plan is built from the sections
    that fit it.

    Nothing the parser merely labels is consulted. For one plain sentence its
    goal came back as gpa_optimization on one run and high_a_rate as true on
    another, and asking it to classify the request made it emit keys the
    schema rejects. "Easy A" requests therefore get a term plan until the list
    flow exists; the answer says how to ask for a list.
    """
    if not isinstance(plan, dict):
        return "no_plan"
    if plan.get("course_scope") != "any":
        return "course_scope"
    if (plan.get("hard_filters") or {}).get("only_unmet_ge") is True:
        return "hard_filters"
    from app.agent.workflow_router import _extract_departments
    from app.catalog.normalization import parse_course_mention

    text = query or ""
    if _extract_departments(text):
        return "department_named"
    if any(parse_course_mention(token) for token in text.replace("，", " ").replace(",", " ").split()):
        return "course_named"
    return None


def is_default_request(plan: dict, query: str) -> bool:
    """True when the student asked for a term plan, not a list of one kind of course."""
    return path_blocker(plan, query) is None


def tray(context: dict, term: Optional[str] = None) -> Optional[dict]:
    """This turn's degree-path tray, when one was built for ``term``."""
    path = (context.get("_prefilter_state") or {}).get("graduation_path")
    if not path or (term and path.get("term") != term):
        return None
    return path


def review_proposal(context: dict, args: dict, term: str) -> tuple[dict, Optional[dict]]:
    """Keep the model's picks inside the tray; the changes are kept for the answer.

    Returns the arguments to stage and, when no pick can stand, the refusal
    to hand back to the model instead.
    """
    path = tray(context, term)
    items = args.get("items")
    if not path or not isinstance(items, list):
        return args, None
    from app.recommendations.plan_check import review_picks

    state = context["_prefilter_state"]
    rows = {compact(row.get("course_id")): row for (row_term, _), row in state.get("candidates", {}).items()
            if row_term == term}
    given = {compact(item.get("course_id")): item for item in items if isinstance(item, dict)}
    # A plan the model asked for is a follow-up: the student may be swapping a
    # course out, so a removed pick is handed back to the model, not refilled.
    follow_up = bool(state.get("term_plan"))
    review = review_picks(path, [item.get("course_id") for item in items if isinstance(item, dict)], rows,
                          left_out=args.get("left_out"), note=args.get("note"), substitute=not follow_up)
    review["follow_up"] = follow_up
    if not review["courses"]:
        # Passing these on would stage courses the degree path never weighed.
        return args, {"ok": False, "error_code": "no_pick_in_tray",
                      "error": "None of the proposed courses can stand: each is outside the degree-path tray "
                               "or clashes in lecture time. Pick from required_now, choose alternatives and "
                               "ge candidates in the tray and propose again.",
                      "removed_outside_tray": review["removed_outside_tray"],
                      "removed_for_time_conflict": review["removed_for_time_conflict"]}
    review["required_outside_time_condition"] = list(path.get("required_outside_time_condition") or [])
    hard = (state.get("plan") or {}).get("hard_filters") or {}
    review["time_rules"] = {"earliest_start": hard.get("earliest_start") if hard.get("earliest_start_is_hard") else None,
                            "excluded_weekdays": list(hard.get("excluded_weekdays") or [])}
    logged = dict(term=term, proposed=len(items), staged=review["courses"], changed=review["changed"],
                  removed_outside_tray=review["removed_outside_tray"],
                  removed_for_time_conflict=review["removed_for_time_conflict"],
                  added_companions=review["added_companions"], substituted=review["substituted"],
                  left_out_required=[e["course_id"] for e in review["left_out_required"]],
                  time_rules=review["time_rules"])
    if context.get("_plan_review_logged") != logged:    # the same picks are checked again when the cards are built
        observability.log_event(logger, logging.INFO, "plan_review", **logged)
        context["_plan_review_logged"] = logged
    context["_plan_review"] = review
    # A checked term plan is answered in the server's words, also when the
    # model asked for the tray in the middle of the turn.
    context["_controlled_recommendation"] = True
    context.setdefault("_controlled_recommendation_cards", [])
    staged = []
    for course in review["courses"]:
        tier = (path.get("reasons") or {}).get(course, {}).get("tier")
        staged.append(given.get(compact(course)) or {
            "course_id": course, "category": "core" if tier != "ge" else "elective", "reason": ""})
    return {**args, "items": staged}, None


def _loads(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def _catalogue() -> dict[str, dict]:
    """compact course ID → the catalogue facts the path needs, cached per sync."""
    global _catalogue_cache
    from app.catalog import course_source

    token = course_source.course_rows_token()
    if token is not None and _catalogue_cache[0] == token:
        return _catalogue_cache[1]
    index: dict[str, dict] = {}
    for row in course_source.course_rows():
        department = " ".join(str(row.get("department") or "").split()).upper()
        number = str(row.get("course_number") or "").strip().upper()
        if not department or not number:
            continue
        try:
            low, high = float(row.get("min_units") or 0), float(row.get("max_units") or 0)
        except (TypeError, ValueError):
            low = high = 0.0
        try:
            numeric = int(float(row.get("course_numeric") or 0)) or None
        except (TypeError, ValueError):
            numeric = None
        index[compact(department + number)] = {
            "course_id": f"{department} {number}", "title": row.get("title") or None,
            "units": low if low and low == high else None, "numeric": numeric,
            "prerequisite_tree": _loads(row.get("prerequisite_tree_json")) or {},
            "terms_offered": _loads(row.get("terms_offered_json")) or [],
            "ge": parse_ge_categories(row.get("ge_list"))["categories"],
        }
    if token is not None:
        _catalogue_cache = (token, index)
    return index


def course_catalogue() -> dict[str, dict]:
    """compact course ID → catalogue facts, for callers outside the path."""
    return _catalogue()


def _evidence(term: str, rows: dict[str, dict]):
    """Cached A rate and local RMP rating per course; no request is made."""
    def lookup(courses: list[str]) -> dict[str, dict]:
        from app.data.recommendations import rank_courses

        wanted = [course for course in courses if course in rows]
        if not wanted:
            return {}
        pool = {"found": True, "term": term, "courses": [{"course_id": rows[c]["course_id"]} for c in wanted]}
        ranked = rank_courses(term, candidate_pool=pool, top_k=None,
                              sections_by_course={rows[c]["course_id"]: rows[c].get("sections") or [] for c in wanted})
        facts: dict[str, dict] = {}
        for row in ranked.get("courses") or []:
            features = row.get("features") or {}
            grade, rating = features.get("grade_a_rate") or {}, features.get("professor_rating") or {}
            facts.setdefault(compact(row.get("course_id")), {
                "a_rate": grade.get("value"), "a_rate_sample": grade.get("sample_count"),
                "a_rate_scope": grade.get("scope") if grade.get("value") is not None else None,
                "rating": rating.get("value"), "rating_sample": rating.get("sample_count"),
                "instructor": row.get("instructor_name"),
            })
        return facts
    return lookup


def build(context: dict, term: str) -> Optional[dict]:
    """The path for this turn, or None when it does not apply or cannot be built."""
    state = context.get("_prefilter_state") or {}
    profile = state.get("profile") or {}
    plan = state.get("plan") if isinstance(state.get("plan"), dict) else {}
    program_id = profile.get("program_id")
    # A plan the model asked for after reading the conversation is not second-guessed by wording.
    asked = bool(state.get("term_plan"))
    blocker = (None if asked else path_blocker(plan, context.get("user_query") or "")) or (
        None if program_id else "no_program") or (
        None if profile.get("completed_courses") else "no_recorded_courses")
    if blocker:
        # Without this line a turn that fell back to department searches
        # looked the same in the log as one where the path was never tried.
        observability.log_event(
            logger, logging.INFO, "graduation_path_skipped", reason=blocker, term=term,
            goal=plan.get("goal"), course_scope=plan.get("course_scope"),
            soft_preferences=plan.get("soft_preferences"), hard_filters=plan.get("hard_filters"),
        )
        return None
    return compute(state, term, program_id=str(program_id))


def compute(state: dict, term: str, *, program_id: str) -> Optional[dict]:
    """The degree path for one term from a filtered candidate pool, whatever the request was."""
    profile = state.get("profile") or {}
    try:
        from app.catalog import program_store
        from app.catalog.course_equivalents import equivalents
        program = program_store.get_program("major", program_id)
        requirements = (program or {}).get("requirements")
        if not requirements:
            observability.log_event(logger, logging.INFO, "graduation_path_skipped",
                                    reason="program_requirements_missing", term=term, program_id=program_id)
            return None
        started = observability.now()
        catalogue = _catalogue()
        rows = {compact(row.get("course_id")): row for (row_term, _), row in state.get("candidates", {}).items()
                if row_term == term}
        excluded = {compact(row.get("id")): row for (row_term, _), row in state.get("excluded", {}).items()
                    if row_term == term}
        path = build_graduation_path(
            requirements=requirements,
            # Transcript courses, mapped exam/transfer credit and courses in
            # progress: none of them is recommended again.
            completed=profile.get("courses_done") or profile.get("completed_courses") or [],
            in_progress=profile.get("in_progress_courses") or [],
            ge_basis=profile.get("ge_basis"), equivalents=equivalents,
            candidates=rows, excluded=excluded, course_info=catalogue.get, term=term,
            evidence=_evidence(term, rows), program_name=(program or {}).get("name"),
            exam_credit_count=int(profile.get("exam_credits_unmatched") or 0),
        )
    except Exception:
        logger.warning("graduation path could not be built", exc_info=True)
        return None
    observability.log_event(
        logger, logging.INFO, "graduation_path_built", term=term, program_id=program_id,
        required_now=len(path["required_now"]), choices=len(path["choose"]),
        ge_missing=len(path["ge"]["categories"]), blocked=len(path["blocked_by_prerequisite"]),
        planned=[item["course_id"] for item in path["suggested_plan"]["courses"]],
        elapsed_ms=observability.elapsed_ms(started),
    )
    return path


def progress_for_user(user_id: str, term: str) -> Optional[dict]:
    """Degree progress for one term outside a chat turn (profile pages, other agents).

    Runs the same prerequisite filter and path as a recommendation with no
    stated preference. None when the student has no program or no courses.
    """
    from app.agent import prefilter
    from app.data import db

    found = db.get_student_profile(user_id)
    profile = (found.get("profile") if found.get("found") else {}) or {}
    if not profile.get("program_id") or not profile.get("completed_courses"):
        return None
    plan = empty_filter_plan()
    context = {"term": term, "allowed_query_terms": [term], "user_id": user_id, "_ranking_profile": profile,
               "_prefilter_state": {
                   "enabled": True, "plan": plan, "plan_status": "validated", "requested_plan": plan,
                   "unresolved_constraints": [], "issues": [], "parse_metrics": {}, "profile": profile,
                   "pools": {}, "candidates": {}, "excluded": {}, "section_overrides": {},
                   "detail_query_attempts": 0, "ge_scope_requested": False}}
    pool = prefilter.search_candidates(context, term)
    if not pool.get("found"):
        return None
    return compute(context["_prefilter_state"], term, program_id=str(profile["program_id"]))


def instruction_messages(context: dict) -> list[dict]:
    path = (context.get("_prefilter_state") or {}).get("graduation_path")
    if not path:
        return []
    from app.llm.safety import untrusted_context_message

    model_view = {key: value for key, value in path.items() if key != "reasons"}
    return [{"role": "system", "content": (
        "A degree-path tray for this term is in the separate context data: the server built it because the "
        "student asked what to take, or you asked for it with plan_term. It already applies their recorded "
        "courses, courses in progress, prerequisites, program requirements and the time rules they stated. "
        "Do not call plan_term again, unless a time or day rule from an earlier message of the student still "
        "applies and has to be carried over. The tray is the whole candidate set for a term plan: "
        "search_courses is disabled this turn, and a course outside required_now, choose and ge candidates "
        "cannot be staged. If the student only asks about the plan (why a course is or is not in it), answer "
        "from the tray in your own words and do not propose again. A course marked all_sections_full stays a valid "
        "pick: the server marks its card as full, and whether to wait for a seat is the student's call, so do "
        "not avoid or drop a course for that mark alone. "
        "Otherwise your job is to pick and adjust "
        "inside it. Start from suggested_plan (about four courses, 16 units) and call propose_recommendation "
        "once. Change the mix only for a reason in the conversation or in the tray: drop or swap what the "
        "student asked to change, swap within a tier, compare alternatives inside one choose entry or one GE category "
        "by a_rate and rating as given (null means no cached data, not zero), or pick another course from the "
        "same GE category. You may leave out a course from suggested_plan, even the first required one, but "
        "then list it in left_out with the reason; the server adds what leaving it out delays. A required "
        "course is justified by its requirement and what it unlocks, never by its A rate. When the student asks "
        "for an easier plan, swap, do not cut: keep the number of courses and replace picks with courses whose "
        "a_rate or rating in the tray is better — another candidate of the same GE category or choose entry "
        "first, then another required_now course in place of a harder one (list the one you replaced in "
        "left_out). If the tray has no easier course for a slot, keep the pick and say so in note. Remove a "
        "course without replacing it only when the student asks for fewer courses or units. Put one or two "
        "sentences on why this mix fits the student in note; the server writes the per-course lines, the "
        "blocked and out-of-time-condition courses and any question about their record, so do not repeat "
        "those. The server then checks your picks: a course outside the tray or clashing in lecture time is "
        "removed, a lab that must accompany its lecture is added, and note is dropped if the picks changed. "
        "The server also chooses each course's lecture so that lectures do not overlap; you do not see section "
        "times, so do not state or promise any."
    )}, untrusted_context_message("graduation path", json.dumps(model_view, ensure_ascii=False, default=str))]


def card_reason(context: dict, course_id: str) -> Optional[dict]:
    """Why this course is on the path, for the card's deterministic reason line."""
    path = (context.get("_prefilter_state") or {}).get("graduation_path") or {}
    wanted = compact(course_id)
    for listed, reason in (path.get("reasons") or {}).items():
        if compact(listed) == wanted:
            return dict(reason)
    return None
