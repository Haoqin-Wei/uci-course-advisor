"""Request-scoped orchestration for conservative, intent-aware prefiltering.

The full pool uses local metadata; selected candidates obtain official GE and
pairing evidence before detailed grade/professor queries.
Grade, RMP and review lookups remain with the existing data/ranking components.
No model-authored rule or inferred degree audit is executable here.
"""
from __future__ import annotations

from copy import deepcopy
from collections import Counter
import json
import logging
import re

from app import config, observability
from app.catalog.normalization import normalize_instructor, parse_course_mention
from app.data import db
from app.data.prerequisites import evaluate_prerequisite_tree

logger = logging.getLogger(__name__)
DETAIL_TOOLS = frozenset({"get_course_info", "get_grade_distribution", "get_zotistics_grades",
                         "get_professor_rating", "get_professor_reviews", "get_professor_tags",
                         "summarize_professor_reviews"})
PROFESSOR_TOOLS = frozenset({"get_professor_rating", "get_professor_reviews",
                            "get_professor_tags", "summarize_professor_reviews"})


def is_recommendation_request(query, query_term_source="default"):
    from app.llm.intent_rules import (
        classify_by_rules, has_explicit_recommendation_request, is_factual_course_identity_query,
    )
    if is_factual_course_identity_query(query or ""):
        return False
    classification = classify_by_rules(query or "")
    explicit = has_explicit_recommendation_request(query or "")
    if query_term_source in {"comparison", "history"} and not explicit:
        return False
    return explicit or bool(classification and classification["intent"] == "course_recommendation")


def active(context):
    return config.dynamic_prefilter_enabled() and (context.get("_prefilter_state") or {}).get("enabled") is True


def recommendation_gate(context):
    """A parsed plan is distinct from a request that is ready for recommendation."""
    if not active(context):
        return {"enabled": False, "ready": True, "reasons": []}
    state = context["_prefilter_state"]
    reasons = list(state.get("unresolved_constraints") or [])
    if state.get("plan_status") == "fallback":
        reasons = ["filter_plan_unavailable", *reasons]
    return {"enabled": True, "ready": not reasons, "reasons": list(dict.fromkeys(reasons))}


def course_key(value):
    ref = parse_course_mention(str(value or ""))
    return ref.display() if ref else None


async def initialize(context, messages):
    if not config.dynamic_prefilter_enabled() or not is_recommendation_request(
            context.get("user_query"), context.get("query_term_source")):
        return
    if context.get("_prefilter_state"):
        return
    from app.llm.safety import student_turns
    # Only earlier human turns are supplementary context: profile and memory
    # data share the user role and are not the student's words. The validator
    # requires current-message evidence for every hard rule, never source/model prose.
    await _start(context, context.get("user_query") or "", student_turns(messages)[-3:-1])
    if recommendation_gate(context)["ready"]:
        await _in_thread(_build_pools, context)


async def _start(context, request_text, earlier, *, term_plan=None):
    """Read the conditions in ``request_text`` and open this turn's prefilter state.

    ``term_plan`` marks a plan the model asked for after reading the
    conversation: ``{"term", "carried"}``.
    """
    from app.llm.filter_plan import parse_filter_plan
    from app.recommendations.filter_plan import empty_filter_plan, unresolved_requested_constraints
    profile_result = await _in_thread(db.get_student_profile, context.get("user_id", ""))
    profile = (profile_result.get("profile") if profile_result.get("found") else {}) or {}
    if request_text.strip():
        envelope = await parse_filter_plan(request_text, {
            "recent_turns": [{"role": "user", "content": value} for value in earlier],
        }, profile)
    else:
        envelope = {"plan": empty_filter_plan(), "status": "validated", "issues": [],
                    "metrics": {"parse_elapsed_ms": 0.0, "llm_calls": 0, "failure_reason": None,
                                "skipped": "no_time_rule_stated"}}
    metrics = envelope.get("metrics") or {}
    observability.observe_ms("prefilter.parse_ms", metrics.get("parse_elapsed_ms", 0))
    observability.increment("prefilter.parser_llm_calls", metrics.get("llm_calls", 0))
    if envelope["status"] != "validated":
        observability.increment("prefilter.plan_downgrades", status=envelope["status"])
    plan, requested = envelope["plan"], envelope.get("requested_plan", envelope["plan"])
    unresolved = list(dict.fromkeys([
        *envelope.get("unresolved_constraints", []),
        *unresolved_requested_constraints(plan, request_text, profile),
    ]))
    if term_plan is not None:
        # Only time and day rules bear on a term plan. A GE-only scope
        # describes a list of courses, which the model decided this is not.
        plan = {**plan, "course_scope": "any", "hard_filters": {**plan["hard_filters"], "only_unmet_ge": False}}
        requested, unresolved = plan, _time_rules(unresolved)
    context["_prefilter_state"] = {
        "enabled": True, "plan": plan, "plan_status": envelope["status"],
        "requested_plan": requested,
        "unresolved_constraints": unresolved,
        "issues": list(envelope.get("issues") or []), "parse_metrics": envelope.get("metrics") or {},
        "profile": profile, "pools": {}, "candidates": {}, "excluded": {}, "section_overrides": {},
        "detail_query_attempts": 0,
        "ge_scope_requested": plan.get("course_scope") == "ge",
        **({"term_plan": dict(term_plan)} if term_plan is not None else {}),
    }
    context["_ranking_profile"] = profile


_TIME_RULE_CODES = frozenset({"excluded_weekday_not_applied", "partial_day_time_scope_unsupported",
                              "earliest_start_scope_unsupported", "earliest_start_not_applied"})


def _time_rules(codes):
    return [code for code in codes if str(code).split(":")[0] in _TIME_RULE_CODES]


async def start_term_plan(context, term, conditions=None):
    """Open, or reuse, the state for a term plan the model asked for.

    The model decides from the whole conversation that a plan is wanted; the
    wording of the current message is not classified here. Time and day
    rules still come from the student's own words. In the current message
    they are found by the detector that audits every parsed plan. From an
    earlier message they arrive as ``conditions``, the model's exact quotes,
    and count only when found in what the student typed. Returns
    ``not_found`` (quotes that are not the student's words), ``ignored``
    (quotes that state no time or day rule) and ``reused`` (this turn's tray
    already covered the request, so nothing was rebuilt).
    """
    from app.recommendations.filter_plan import empty_filter_plan, unresolved_requested_constraints

    def squeeze(text):
        return " ".join(str(text or "").split())

    def states_rule(text):
        return bool(_time_rules(unresolved_requested_constraints(empty_filter_plan(), text)))

    message = str(context.get("user_query") or "")
    current = squeeze(message)
    turns = [squeeze(turn) for turn in context.get("_student_turns") or []]
    carried, not_found, ignored = [], [], []
    quoted = [conditions] if isinstance(conditions, str) else conditions
    for raw in quoted if isinstance(quoted, (list, tuple)) else []:
        quote = squeeze(raw)
        if not quote or quote in current or quote in carried:
            continue                      # the current message is read below, whatever the model quoted
        if not any(quote in turn for turn in turns):
            not_found.append(quote[:200])
        elif not states_rule(quote):
            ignored.append(quote[:200])
        else:
            carried.append(quote)
    state = context.get("_prefilter_state") or {}
    path = state.get("graduation_path") or {}
    already = (state.get("term_plan") or {}).get("carried") or []
    outcome = {"not_found": not_found, "ignored": ignored, "reused": False}
    if path.get("term") == term and set(carried) <= set(already):
        return {**outcome, "reused": True}
    context.pop("_prefilter_state", None)
    parts = [text for text in (message, *carried) if states_rule(text)]
    await _start(context, "\n".join(parts), turns[-3:-1], term_plan={"term": term, "carried": carried})
    if recommendation_gate(context)["ready"]:
        await _in_thread(_build_pools, context)
    return outcome


def _build_pools(context):
    for term in context.get("allowed_query_terms") or [context.get("term")]:
        if term:
            search_candidates(context, term)
    # No stated preference means "what moves my degree forward": the server
    # derives that shortlist from the filtered pool it just built.
    from app.agent import graduation
    state = context["_prefilter_state"]
    terms = context.get("allowed_query_terms") or [context.get("term")]
    path_term = (state.get("term_plan") or {}).get("term") or (terms[0] if len(terms) == 1 else None)
    if path_term:
        state["graduation_path"] = graduation.build(context, path_term)


def refresh_profile(context):
    """Rebuild what this turn derived from the student's record after the record changed.

    Returns False when the turn has no prefilter state to rebuild.
    """
    if not active(context):
        return False
    state = context["_prefilter_state"]
    found = db.get_student_profile(context.get("user_id", ""))
    profile = (found.get("profile") if found.get("found") else {}) or {}
    state["profile"] = profile
    context["_ranking_profile"] = profile
    for key in ("pools", "candidates", "excluded"):
        state[key].clear()
    state.pop("graduation_path", None)
    if recommendation_gate(context)["ready"]:
        _build_pools(context)
    return True


async def _in_thread(fn, *args, **kwargs):
    import asyncio
    return await asyncio.to_thread(fn, *args, **kwargs)


def _concurrent_courses(tree):
    """Courses a prerequisite tree allows to be taken in the same term."""
    if isinstance(tree, list):
        return [course for child in tree for course in _concurrent_courses(child)]
    if not isinstance(tree, dict):
        return []
    found = [course for key in ("AND", "OR") for course in _concurrent_courses(tree.get(key))]
    if tree.get("coreq") is True and isinstance(tree.get("courseId"), str):
        found.append(tree["courseId"])
    return found


def _metadata_candidate(course, sections, context, term, *, offering=None):
    state = context["_prefilter_state"]
    profile = state["profile"]
    recorded = profile.get("completed_course_attempts", profile.get("completed_courses", [])) or []
    # The student's recorded courses are what they have taken (their decision):
    # a prerequisite absent from a non-empty record is not met, and the course
    # is excluded. With no record at all nothing is known, so nothing is
    # excluded; otherwise every course with a prerequisite would disappear.
    complete = bool(recorded) or (profile.get("academic_record_complete") is True
                                  and profile.get("academic_record_verified") is True)
    result = evaluate_prerequisite_tree(
        course.get("prerequisite_tree"),
        completed_courses=recorded,
        # "PHYSICS 7D (coreq allowed)" can be taken in the same term, so it is
        # not a missing prerequisite. Only coreq leaves accept these entries.
        # Courses the student says they are taking this term count: without a
        # minimum grade the prerequisite is met, with one it stays unknown
        # until the grade exists. Schedule drafts (selected_courses) are plans,
        # not enrollment, and are not passed here.
        in_progress_courses=[*(profile.get("in_progress_courses") or []),
                             *_concurrent_courses(course.get("prerequisite_tree"))],
        flat_prerequisites=course.get("prerequisites") or [],
        prerequisite_text=course.get("prerequisite_text"),
        allow_in_progress=True,
    )
    source = course.get("provenance") or {}
    proxy = source.get("is_historical_proxy") is True
    from app.data.ge_evidence import verified_ge_provenance
    annual_ge = verified_ge_provenance(course.get("ge_provenance"), term)
    row = {**course, "course_id": course_key(course.get("course_id")) or course.get("course_id"),
           "term": term, "sections": [] if proxy and not offering else sections,
           "ge_labels_verified": bool((not proxy or annual_ge) and course.get("ge_labels_verified") is True),
           "prerequisite_status": result.status,
           "prerequisite_authoritative": complete,
           "prerequisite_missing": list(result.missing[:4]),
           # Being taken right now: nothing to recommend, whatever the request.
           "in_progress_now": course_key(course.get("course_id")) in {
               course_key(value) for value in profile.get("in_progress_courses") or []},
           "offering_status": "unavailable" if proxy else "offered" if sections else "unavailable",
           "authoritative": False}
    if offering:
        coverage = offering.get("data_coverage") or {}
        response_proxy = offering.get("is_historical_proxy") is True or coverage.get("is_historical_proxy") is True
        response_term = db._to_term(offering.get("term"))
        wrong_term = response_term is not None and response_term != db._to_term(term)
        current_response = (response_term == db._to_term(term) and offering.get("found") is True
                            and not response_proxy and not wrong_term
                            and offering.get("offering_status") not in {"unavailable", "unpublished", "not_offered"})
        if response_proxy or wrong_term:
            # Never pass historical professor/time/section identities to a
            # target-term recommendation merely because a lookup returned rows.
            row["sections"] = []
        elif current_response:
            row.update(section_source=offering.get("source"), source_url=offering.get("source_url"),
                       retrieved_at=offering.get("retrieved_at"),
                       pairing_source_proof=deepcopy(offering.get("pairing_source_proof") or offering.get("source_proof")))
            row["sections"] = [
                {**section, "provenance": section.get("provenance") or {
                    "source_term": term, "target_term": term, "is_historical_proxy": False,
                }} for section in row["sections"]
            ]
    if offering:
        row.update(offering_status=offering.get("offering_status", row["offering_status"]),
                   authoritative=offering.get("authoritative") is True)
        if response_proxy or wrong_term:
            row.update(offering_status="unavailable", authoritative=False)
        else:
            row.update({key: deepcopy(offering[key]) for key in ("comment_contexts", "course_notices")
                        if key in offering})
    from app.recommendations.pairing import normalize_pairing_evidence
    pairing_source = {**row, "pairing": (offering or {}).get("pairing") or row.get("pairing")}
    row["pairing"] = normalize_pairing_evidence(pairing_source, term=term)
    return row


def _apply(candidates, context):
    from app.recommendations.prefilter import apply_dynamic_prefilter
    state = context["_prefilter_state"]
    result = apply_dynamic_prefilter(candidates, state["plan"], state["profile"])
    # Requesting GE courses constrains course scope; it does not authorize the
    # much stronger 'only unmet GE' rule. Missing tags are not confirmed non-GE.
    if state["ge_scope_requested"]:
        from app.recommendations.filter_plan import normalize_ge_category
        kept = []
        for row in result["kept"]:
            if row.get("ge_labels_verified") is True and not row.get("ge_categories"):
                result["excluded"].append({"id": row["course_id"], "reasons": ["not_a_ge_course"]})
            else:
                labels = row.get("ge_categories") or []
                if (row.get("ge_labels_verified") is not True or not labels
                        or any(normalize_ge_category(label) is None for label in labels)):
                    row["prefilter"]["status"] = "needs_verification"
                    row["prefilter"]["reasons"] = list(dict.fromkeys([*row["prefilter"]["reasons"], "ge_labels_unknown"]))
                    issue = next((issue for issue in result["needs_verification"]
                                  if issue["id"] == row["course_id"] and issue.get("term") == row.get("term")), None)
                    if issue is not None:
                        issue["reasons"] = row["prefilter"]["reasons"]
                    else:
                        result["needs_verification"].append({"id": row["course_id"], "term": row.get("term"),
                                                              "reasons": row["prefilter"]["reasons"]})
                kept.append(row)
        result["kept"] = kept
        retained = {row["course_id"] for row in kept}
        result["needs_verification"] = [row for row in result["needs_verification"] if row["id"] in retained]
    originals = {row.get("course_id"): row for row in candidates}
    for row in result["kept"]:
        source = originals.get(row.get("course_id"), {})
        kept_codes = {section.get("section_code") for section in row.get("sections") or []
                      if isinstance(section, dict)}
        evidence = _excluded_section_evidence(source.get("sections") or [], kept_codes)
        previous = source.get("excluded_section_evidence") or []
        if evidence or previous:
            row["excluded_section_evidence"] = list({json.dumps(item, sort_keys=True): item
                for item in deepcopy(previous) + evidence}.values())
    for row in result["excluded"]:
        source = originals.get(row.get("id"), {})
        row.update({key: deepcopy(source[key]) for key in ("comment_contexts", "course_notices") if key in source})
        evidence = _excluded_section_evidence(source.get("sections") or [], set())
        if evidence:
            row["excluded_section_evidence"] = evidence
    result["counts"] = {**result.get("counts", {}),
                        "before": len(candidates), "kept": len(result["kept"]),
                        "excluded": len(result["excluded"]),
                        "needs_verification": len(result["needs_verification"])}
    kept_sections = sum(len(row.get("sections") or []) for row in result["kept"])
    result["counts"].update(kept_candidates=len(result["kept"]), excluded_candidates=len(result["excluded"]),
                            kept_sections=kept_sections,
                            excluded_sections=result["counts"].get("input_sections", 0) - kept_sections)
    return result


def _excluded_section_evidence(sections, retained_codes):
    """Source notices remain readable even when a section is not selectable."""
    fields = ("section_code", "section_num", "section_type", "is_cancelled", "cancellation_status",
              "meetings", "comments", "section_comment", "source", "retrieved_at", "updated_at")
    return [{**{key: deepcopy(section[key]) for key in fields if key in section}, "selectable": False}
            for section in sections if isinstance(section, dict)
            and section.get("section_code") not in retained_codes
            and any(section.get(key) is not None for key in ("meetings", "comments", "section_comment"))]


def _store(result, context, term):
    state = context["_prefilter_state"]
    for row in result["kept"]:
        key = (term, course_key(row["course_id"]))
        state["candidates"][key] = row
        state["excluded"].pop(key, None)
        context.setdefault("_recommendation_sections", {})[key[::-1]] = {
            "found": bool(row.get("sections")), "source": row.get("section_source") or "prefilter_metadata",
            "sections": deepcopy(row.get("sections") or []), "term": term,
            "offering_status": row.get("offering_status"), "authoritative": row.get("authoritative", False),
            "pairing": deepcopy(row.get("pairing")),
            **({"excluded_section_evidence": deepcopy(row["excluded_section_evidence"])}
               if row.get("excluded_section_evidence") else {}),
            **{name: deepcopy(row[name]) for name in ("source_url", "retrieved_at", "pairing_source_proof",
                                                     "comment_contexts", "course_notices") if name in row},
        }
    for row in result["excluded"]:
        key = (term, course_key(row["id"]))
        state["excluded"][key] = row
        state["candidates"].pop(key, None)
        context.get("_recommendation_sections", {}).pop(key[::-1], None)


def search_candidates(context, term, department=None, ge_category=None, exclude_ids=None):
    """Retrieve the full matching local pool; apply rules before any scores."""
    started = observability.now()
    state = context["_prefilter_state"]
    gate = recommendation_gate(context)
    if not gate["ready"]:
        return {"found": False, "courses": [], "term": term,
                "reason": "recommendation conditions need clarification or verification",
                "recommendation_gate": gate, "prefilter": summary(context)}
    cache_key = (term, department, ge_category, tuple(sorted(exclude_ids or [])))
    if cache_key in state["pools"]:
        return deepcopy(state["pools"][cache_key])
    pool = db.retrieve_course_candidates(term, department, ge_category, exclude_ids)
    if not pool.get("found"):
        return {**pool, "prefilter": {"enabled": True, "coverage": "unknown",
                                      "plan_status": state["plan_status"], "issues": state["issues"]}}
    catalog = db.get_catalog(db._to_term(term))
    candidates = []
    for entry in pool["courses"]:
        # The pool comes from this catalog: resolve its IDs exactly. The
        # free-text parser does not know every registrar department.
        ref = catalog.ref_for_display(entry["course_id"]) or parse_course_mention(entry["course_id"])
        if ref is None:
            continue
        record = catalog.get_course(ref)
        course = ({**db._course_record_to_dict(record), "ge_labels_verified": record.ge_categories_verified}
                  if record else entry)
        sections = [db._section_record_to_dict(section) for section in catalog.get_sections(ref)]
        override = state.get("section_overrides", {}).get((term, ref.display()))
        candidates.append(_metadata_candidate(course, override.get("sections") if override else sections,
                                              context, term, offering=override))
    result = _apply(candidates, context)
    _store(result, context, term)
    light_keys = ("course_id", "title", "units", "level", "ge_categories", "term", "sections", "prefilter",
                  "prerequisite_status", "offering_status", "authoritative", "requires_secondary",
                  "required_secondary_types", "pairing", "excluded_section_evidence", "comment_contexts", "course_notices")
    response = {**pool, "courses": [{key: row[key] for key in light_keys if key in row} for row in result["kept"]],
                "total_found": len(result["kept"]), "truncated": False,
                "prefilter": {"enabled": True, "applied_plan": result["applied_plan"],
                              "plan_status": state["plan_status"], "issues": state["issues"],
                              "counts": result["counts"], "excluded": result["excluded"],
                              "needs_verification": result["needs_verification"]}}
    state["pools"][cache_key] = deepcopy(response)
    elapsed = observability.elapsed_ms(started)
    observability.observe_ms("prefilter.filter_ms", elapsed)
    for name, count in result["counts"].items():
        observability.increment(f"prefilter.candidates_{name}", count)
    for row in result["excluded"]:
        for reason in row["reasons"]:
            observability.increment("prefilter.exclusions", reason=reason)
    observability.log_event(logger, logging.INFO, "dynamic_prefilter", **result["counts"], elapsed_ms=elapsed,
                            plan_status=state["plan_status"])
    return response


def ensure_candidate(context, course_id, term=None):
    """Recheck model-supplied IDs outside the retrieved catalog conservatively."""
    term = term or context.get("term")
    cid = course_key(course_id)
    state = context["_prefilter_state"]
    key = (term, cid)
    if key in state["excluded"]:
        return None, state["excluded"][key]
    if key in state["candidates"]:
        return _enrich_candidate(context, state["candidates"][key], term)
    if not cid or not term:
        return None, {"id": cid or str(course_id), "reasons": ["candidate_identity_unknown"]}
    # These are lightweight eligibility inputs, never grades/RMP/reviews.
    info = db.get_course_info(cid)
    sections = db.get_sections(cid, term)
    course = info.get("course") or {"course_id": cid, "ge_categories": []}
    candidate = _metadata_candidate(course, sections.get("sections") or [], context, term, offering=sections)
    result = _apply([candidate], context)
    _store(result, context, term)
    if result["excluded"]:
        return None, result["excluded"][0]
    return _enrich_candidate(context, result["kept"][0], term)


def _enrich_candidate(context, row, term):
    """Resolve selected candidates only; never crawl/truncate the full pool."""
    state = context["_prefilter_state"]
    key = (term, course_key(row["course_id"]))
    from app.recommendations.pairing import discussion_policy_current
    if key in state.setdefault("evidence_checked", {}) and discussion_policy_current(row.get("pairing")):
        return row, None
    state["evidence_checked"][key] = True
    from app.data.ge_evidence import enrich_course_ge_evidence
    course = {**row, **enrich_course_ge_evidence(row, term)}
    response = db.enrich_sections_pairing(row["course_id"], term, {
        "found": bool(row.get("sections")), "sections": row.get("sections") or [], "term": term,
        "source": row.get("section_source") or "prefilter_metadata", "offering_status": row.get("offering_status"),
        **{name: deepcopy(row[name]) for name in ("source_url", "retrieved_at", "pairing_source_proof",
                                                 "comment_contexts", "course_notices") if name in row},
        "pairing": row.get("pairing") if discussion_policy_current(row.get("pairing")) else None,
    })
    candidate = _metadata_candidate(course, response.get("sections") or [], context, term, offering=response)
    candidate.update({name: deepcopy(response[name]) for name in (
        "source_url", "retrieved_at", "pairing_source_proof", "comment_contexts", "course_notices") if name in response})
    result = _apply([candidate], context)
    old_digest = ((row.get("pairing") or {}).get("evidence") or {}).get("html_sha256")
    new_digest = ((candidate.get("pairing") or {}).get("evidence") or {}).get("html_sha256")
    if old_digest and old_digest == new_digest:
        # Rechecking an already filtered snapshot must not erase why sections
        # disappeared. Explicit refresh builds a new audit from full new rows.
        for kept in result["kept"]:
            previous = (row.get("prefilter") or {}).get("excluded_sections") or []
            current = kept["prefilter"].get("excluded_sections") or []
            kept["prefilter"]["excluded_sections"] = list({json.dumps(item, sort_keys=True): item
                for item in [*previous, *current]}.values())
    _store(result, context, term)
    state["pools"].clear()
    if result["excluded"]:
        return None, result["excluded"][0]
    return result["kept"][0], None


def filter_items(context, items, term=None):
    if not active(context):
        return items, []
    if not isinstance(items, list):
        return [], [{"id": "", "reasons": ["invalid_candidate_items"]}]
    kept, excluded = [], []
    for item in items or []:
        if not isinstance(item, dict):
            excluded.append({"id": "", "reasons": ["invalid_candidate_item"]})
            continue
        row, rejection = ensure_candidate(context, item.get("course_id"), term)
        (excluded if rejection else kept).append(rejection if rejection else item)
    return kept, excluded


def _person_key(value):
    return re.sub(r"[.\s]+", "", normalize_instructor(value or ""))


def _retained_person(person, rows):
    allowed = {_person_key(instructor) for row in rows for section in row.get("sections") or []
               for instructor in section.get("instructors") or [] if instructor}
    if _person_key(person) in allowed:
        return True
    # Verify full-name aliases only against the local identity snapshot, after
    # filtering. No surname/initial approximation or network lookup is allowed.
    from app.data import professors
    for row in rows:
        department = (parse_course_mention(row.get("course_id") or "").department
                      if parse_course_mention(row.get("course_id") or "") else None)
        requested = professors.lookup_professor(person, department=department)
        if not requested or not professors.verify_rating_identity(person, {
                "found": True, "instructor": professors.build_profile(requested)}).get("found"):
            continue
        for section in row.get("sections") or []:
            for instructor in section.get("instructors") or []:
                actual = professors.lookup_professor(instructor, department=department)
                if actual and actual.get("legacyId") is not None and actual.get("legacyId") == requested.get("legacyId"):
                    return True
    return False


def _tool_course(args, context):
    raw = args.get("course_id") or args.get("course")
    if not raw:
        return None
    if course_key(raw):
        return course_key(raw)
    if args.get("department"):
        from app.catalog.departments import resolve_department
        department = resolve_department(args["department"])
        if department and course_key(f"{department} {raw}"):
            return course_key(f"{department} {raw}")
    state = context["_prefilter_state"]
    known = {cid for (_, cid) in (*state["candidates"].keys(), *state["excluded"].keys()) if cid}
    matches = [cid for cid in known if parse_course_mention(cid).course_number == str(raw).strip().upper()]
    return matches[0] if len(matches) == 1 else None


def guard_call(name, args, context):
    """Enforce the same rules even when a model bypasses search_courses."""
    if not active(context):
        return args, None
    gate = recommendation_gate(context)
    if not gate["ready"] and name in DETAIL_TOOLS | {"propose_recommendation", "search_courses"}:
        return args, {"ok": False, "found": False, "error": "recommendation conditions remain unresolved",
                      "recommendation_gate": gate, "prefilter": summary(context)}
    term = args.get("term") or context.get("term")
    from app.agent import graduation
    if name == "search_courses" and graduation.tray(context):
        # The tray is the candidate set: a department listing costs tens of
        # thousands of tokens and offers courses the degree path already weighed.
        return args, {"ok": False, "found": False, "error_code": "tray_is_the_candidate_set",
                      "error": "A degree-path tray was built for this request; search_courses is disabled this "
                               "turn. Pick from the tray and call propose_recommendation."}
    if name == "propose_recommendation":
        args, refused = graduation.review_proposal(context, args, term)
        if refused:
            return args, refused
        kept, excluded = filter_items(context, args.get("items"), term)
        hard = context["_prefilter_state"]["plan"].get("hard_filters") or {}
        if hard.get("excluded_weekdays") or hard.get("earliest_start_is_hard"):
            verified = []
            for item in kept:
                cid = course_key(item.get("course_id"))
                # Timetable refresh is lightweight evidence, before any grade
                # query. Unknown rows stay in the pool, outside formal cards.
                response = section_response(context, cid, term)
                row = context["_prefilter_state"]["candidates"].get((term, cid))
                sections = _verified_time_sections(row, hard) if row and response.get("found") else []
                if sections:
                    context.setdefault("_recommendation_sections", {})[(cid, term)] = {
                        **response, "found": True, "sections": sections,
                    }
                    verified.append(item)
                else:
                    pending = [reason for reason in (row.get("prefilter") or {}).get("reasons", [])
                               if reason in {"time_unknown", "secondary_pairing_unknown", "section_schedule_unknown"}] if row else []
                    excluded.append({"id": cid, "reasons": ["hard_time_not_verified", *pending]})
            kept = verified
        if not kept:
            return args, {"ok": False, "error": "no proposed course passed prefilter", "prefilter_excluded": excluded}
        return {**args, "items": kept}, None
    cid = _tool_course(args, context)
    if (args.get("course_id") or args.get("course")) and not cid and name in DETAIL_TOOLS:
        return args, {"found": False, "error": "course context is not verified for this detail lookup",
                      "prefilter_reason": "course_context_unverified"}
    if cid and name in DETAIL_TOOLS | {"get_sections", "get_live_sections", "check_prerequisites_met"}:
        _, rejection = ensure_candidate(context, cid, term)
        if rejection:
            return args, {"found": False, "error": "course excluded by prefilter", "prefilter_excluded": [rejection]}
    person = args.get("instructor_name")
    if person and name in PROFESSOR_TOOLS | {"get_zotistics_grades"}:
        rows = [row for (row_term, _), row in context["_prefilter_state"]["candidates"].items()
                if row_term == term and (not cid or course_key(row["course_id"]) == course_key(cid))]
        if not _retained_person(person, rows):
            return args, {"found": False, "error": "instructor is not verified in the retained candidate sections",
                          "prefilter_reason": "instructor_outside_retained_sections"}
    if name in DETAIL_TOOLS:
        note_details(context, name)
    return args, None


def _verified_time_sections(row, hard):
    """Restrict formal cards to known compatible times; retain unknown pool rows."""
    from app.recommendations.prefilter import _filter_sections, _kind, _section_decision
    if "pairing" in row:
        from app.recommendations.pairing import normalize_pairing_evidence, available_pairing_sections
        pairing = normalize_pairing_evidence(row, term=row.get("term"))
        if pairing["version"] == 1 and pairing["requirement"] == "unknown":
            return []
        known = [section for section in row.get("sections") or [] if not any(_section_decision(section, row, hard))]
        retained, status = available_pairing_sections(pairing, known, verified_only=True)
        return retained if status == "satisfied" else []
    if "secondary_pairing_unknown" in (row.get("prefilter") or {}).get("reasons", []):
        # This warning was derived from the original pre-filter timetable.
        # A discussion disappearing on an excluded day does not prove that
        # the remaining lecture can be registered without that discussion.
        return []
    raw = row.get("sections") or []
    known = [section for section in raw if not any(_section_decision(section, row, hard))]
    if not any(_kind(section) in {"lec", "sem"} for section in known):
        return []
    candidate = {**row, "sections": known}
    # Cards already require observed secondary enrollment components. Apply
    # that same conservative pairing contract before presenting time fitness.
    secondary_types = {_kind(section) for section in raw
                       if _kind(section) in {"dis", "lab", "stu", "act", "tut", "fld"}}
    if secondary_types and not candidate.get("required_secondary_types") and len(secondary_types) > 1:
        # Unknown optional/required status cannot disappear when a TBA
        # component was removed from the displayed subset.
        return []
    if candidate.get("requires_secondary") is None and secondary_types:
        candidate["requires_secondary"] = True
    sections, _, warnings, no_bundle = _filter_sections(candidate, hard)
    if no_bundle or warnings:
        return []
    return sections


def note_details(context, kind, count=1):
    if active(context):
        context["_prefilter_state"]["detail_query_attempts"] += count
        observability.increment("prefilter.detail_query_attempts", count, kind=kind)


def section_response(context, course_id, term):
    row, rejection = ensure_candidate(context, course_id, term)
    if rejection:
        return {"found": False, "prefilter_excluded": [rejection], "sections": []}
    if (not row.get("sections") and row.get("offering_status") != "not_offered"
            and (term, course_key(course_id)) not in context["_prefilter_state"].get("section_overrides", {})):
        return apply_section_response(context, course_id, term, db.get_sections(course_id, term))
    return {"found": bool(row.get("sections")), "source": row.get("section_source") or "prefilter_metadata",
            "sections": deepcopy(row.get("sections") or []), "term": term,
            "pairing": deepcopy(row.get("pairing")),
            **({"excluded_section_evidence": deepcopy(row["excluded_section_evidence"])}
               if row.get("excluded_section_evidence") else {}),
            **{name: deepcopy(row[name]) for name in ("source_url", "retrieved_at", "pairing_source_proof",
                                                     "comment_contexts", "course_notices") if name in row},
            "offering_status": row.get("offering_status"), "authoritative": row.get("authoritative", False)}


def apply_section_response(context, course_id, term, response):
    """Reapply hard constraints to an explicit refresh/current-section response."""
    state = context["_prefilter_state"]
    cid = course_key(course_id)
    response = db.enrich_sections_pairing(course_id, term, response,
                                         force_refresh=response.get("is_live") is True)
    old = state["candidates"].get((term, cid)) or {"course_id": cid, "ge_categories": []}
    state.setdefault("section_overrides", {})[(term, cid)] = deepcopy(response)
    candidate = _metadata_candidate(old, response.get("sections") or [], context, term, offering=response)
    candidate.pop("excluded_section_evidence", None)  # Explicit refresh replaces the old source snapshot.
    candidate.update({name: deepcopy(response[name]) for name in (
        "source_url", "retrieved_at", "pairing_source_proof", "comment_contexts", "course_notices") if name in response})
    result = _apply([candidate], context)
    _store(result, context, term)
    state["pools"].clear()
    if not result["kept"]:
        return {**response, "found": False, "sections": [], "prefilter_excluded": result["excluded"],
                "excluded_section_evidence": _excluded_section_evidence(response.get("sections") or [], set())}
    return {**response, "sections": result["kept"][0].get("sections") or [],
            **({"excluded_section_evidence": deepcopy(result["kept"][0]["excluded_section_evidence"])}
               if result["kept"][0].get("excluded_section_evidence") else {}),
            "prefilter": card_metadata(context, cid, term)}


def card_metadata(context, course_id, term):
    row = context["_prefilter_state"]["candidates"].get((term, course_key(course_id))) or {}
    reasons = list((row.get("prefilter") or {}).get("reasons") or [])
    # Engine status/warnings are structured; never treat a kept course as a
    # certificate of registration or fabricate a degree contribution.
    for response in context["_prefilter_state"]["pools"].values():
        reasons += [reason for issue in response.get("prefilter", {}).get("needs_verification", [])
                    if response.get("term") == term and issue.get("term", term) == term
                    and course_key(issue["id"]) == course_key(course_id) for reason in issue["reasons"]]
    reasons += context["_prefilter_state"].get("issues") or []
    return {"enabled": True, "status": "needs_verification" if reasons else "keep",
            "warnings": [{"code": reason, "message": _warning_text(reason, context.get("response_language"))}
                         for reason in dict.fromkeys(reasons)]}


def _warning_text(reason, language):
    messages = {
        "prerequisite_status_unknown": ("先修资格尚未确认。", "Prerequisite eligibility is not yet verified."),
        "prerequisite_evidence_unverified": ("现有学业记录不足以确认先修资格。", "The available academic record does not fully verify prerequisites."),
        "repeat_eligibility_unknown": ("你有同门课的修读记录；重复修读资格及学分、GPA 影响需核实。", "You have a record for this course; repeat eligibility and credit/GPA effects need verification."),
        "ge_audit_unverified": ("缺少已核实的 GE 完成记录，暂未按 GE 缺项排除课程。", "Verified GE progress is unavailable; courses have not been excluded on assumed GE completion."),
        "ge_labels_unknown": ("该课程在目标学年的 GE 类别尚未确认。", "This course's GE categories for the target academic year are not yet verified."),
        "time_unknown": ("部分班次时间待定，尚不能确认符合你的时间要求。", "Some section times are unknown; your time constraints are not fully verified."),
        "secondary_pairing_unknown": ("讲座与讨论课或实验课的配对条件需核实。", "Lecture and discussion/lab pairing needs verification."),
        "offering_status_unknown": ("目标学期开课信息尚未确认。", "Offering information for the target term is not yet verified."),
        "eligibility_status_unknown": ("课程修读限制尚未确认。", "Enrollment restrictions are not yet verified."),
        "section_schedule_unknown": ("目标学期的班次安排尚未确认。", "Sections for the target term are not yet verified."),
        "section_status_unknown": ("班次当前状态尚未确认。", "Current section status is not yet verified."),
        "section_type_unknown": ("班次类型尚未确认。", "The section type is not yet verified."),
        "earliest_start_scope_unsupported": ("按特定星期或课程限定的时间要求尚需核实，本次未扩大为所有课的限制。",
                                             "Time limits scoped to particular days or courses need verification; they were not applied to all classes."),
    }
    pair = messages.get(reason, ("部分选课条件仍待核实；暂时保留此课程供比较。",
                                 "Some selection conditions need verification; this course is retained for comparison."))
    return pair[0] if language == "zh" else pair[1]


def summary(context):
    if not active(context):
        return None
    state = context["_prefilter_state"]
    return {"enabled": True, "applied_plan": state["plan"], "plan_status": state["plan_status"],
            "requested_plan": state.get("requested_plan", state["plan"]),
            "unresolved_constraints": state.get("unresolved_constraints", []),
            "recommendation_gate": recommendation_gate(context),
            "issues": state["issues"], "kept_count": len(state["candidates"]),
            "excluded_count": len(state["excluded"]),
            "excluded": list(state["excluded"].values()),
            "needs_verification": [{"id": row["course_id"], "term": term,
                                    "reasons": (row.get("prefilter") or {}).get("reasons") or []}
                                   for (term, _), row in state["candidates"].items()
                                   if (row.get("prefilter") or {}).get("status") == "needs_verification"],
            "detail_query_attempts": state["detail_query_attempts"], "parse_metrics": state["parse_metrics"]}


MODEL_LISTED_EXCLUSIONS = 40
_EXCLUSION_FIELDS_FOR_MODEL = ("id", "term", "reasons", "missing_prerequisites", "ge_contribution")


def _exclusions_for_model(rows):
    """Excluded courses as the model needs them: which, and why.

    Each stored record also carries every section of the course as audit
    evidence. With a few hundred prerequisite exclusions that copy alone was
    millions of characters in one tool result and overflowed the model's
    context, so the evidence stays server-side.
    """
    rows = list(rows or [])
    listed = [{key: row[key] for key in _EXCLUSION_FIELDS_FOR_MODEL if row.get(key)}
              for row in rows[:MODEL_LISTED_EXCLUSIONS]]
    return {"excluded_count": len(rows),
            "exclusion_reason_counts": dict(Counter(reason for row in rows for reason in row.get("reasons") or [])),
            "excluded": listed,
            **({"excluded_not_listed": len(rows) - len(listed)} if len(rows) > len(listed) else {})}


def model_summary(context):
    """`summary` for a tool result: counts and reasons, not the full audit lists."""
    full = summary(context)
    if full is None:
        return None
    compact = {key: value for key, value in full.items() if key not in {"excluded", "needs_verification"}}
    return {**compact, "needs_verification_count": len(full["needs_verification"]),
            **_exclusions_for_model(full["excluded"])}


def model_pool_view(pool):
    """A search result for the model, with exclusions reduced the same way."""
    report = pool.get("prefilter") if isinstance(pool, dict) else None
    if not isinstance(report, dict) or not isinstance(report.get("excluded"), list):
        return pool
    verification = report.get("needs_verification") or []
    slim = {key: value for key, value in report.items() if key not in {"excluded", "needs_verification"}}
    return {**pool, "prefilter": {
        **slim, **_exclusions_for_model(report["excluded"]),
        "needs_verification_count": len(verification),
        "needs_verification": verification[:MODEL_LISTED_EXCLUSIONS]}}


def instruction_messages(context):
    if not active(context):
        return []
    from app.llm.safety import untrusted_context_message
    state = context["_prefilter_state"]
    full_summary = summary(context)
    # The complete pool and all exclusions remain server-side and in the
    # persisted report. A narrow search can rank the full matching pool; this
    # is not a first-N course cutoff before filtering/scoring.
    compact = {key: value for key, value in full_summary.items()
               if key not in {"needs_verification", "excluded"}}
    compact["needs_verification_count"] = len(full_summary["needs_verification"])
    data = {"summary": compact,
            "candidate_departments": dict(Counter(
                parse_course_mention(row.get("course_id")).department
                for row in state["candidates"].values() if parse_course_mention(row.get("course_id")))),
            "exclusion_reason_counts": dict(Counter(reason for row in state["excluded"].values()
                                                     for reason in row.get("reasons") or []))}
    return [{"role": "system", "content": (
        "Dynamic prefilter is enabled; read plan_status and recommendation_gate before claiming it succeeded. "
        "When recommendation_gate.ready=false, do not recommend a slate, call grade/RMP/review tools, or "
        "infer missing requirements. Explain the unresolved condition and ask one useful clarification or retry. "
        "Missing trusted GE audit cannot be repaired with web search or inferred from a short course list. "
        "An applied false/default filter does not cancel an unresolved user requirement. "
        "Backend rules run before grade/RMP/review "
        "lookups. Use retained candidates and their retained sections. Needs-verification candidates are "
        "not confirmed eligible; state their uncertainties. A completed GE category alone is not an "
        "exclusion. Do not invent GE audit results or override exclusions. Use existing fixed Python "
        "ranking when enabled; no dynamic weights. Use search_courses for relevant narrower retrieval; "
        "do not invent a GE category, department or completed-course exclusion as a user requirement. "
        "TBA/unknown times cannot certify a hard time condition. Only present verified retained section "
        "combinations as satisfying that condition. secondary_pairing_unknown cannot certify a complete "
        "combination and must never be explained as pure lecture/no discussion. A component hidden by "
        "filtering may still be required. Soft preferences do not eliminate candidates."
    )}, untrusted_context_message("dynamic prefilter results", json.dumps(data, ensure_ascii=False)),
        *_graduation_messages(context)]


def _graduation_messages(context):
    from app.agent import graduation
    return graduation.instruction_messages(context)
