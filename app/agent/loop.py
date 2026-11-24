"""
Agent loop — drives a tool-using LLM conversation until it produces
a final answer (or a safety bound trips).

Each iteration:
    1. stream one LLM call with tools=TOOL_SCHEMAS, accumulating
       both content deltas and tool_call deltas
    2. if the model emitted tool calls, dispatch each one through
       app.agent.tools.dispatch, append the tool results to messages,
       and loop again
    3. otherwise the streamed content IS the final answer — yield a
       "final" event and stop

Event protocol (yielded to the caller, then forwarded to SSE):
    {"type": "token",            "text": "<delta>"}
        Final-answer text deltas. Also emitted for intermediate text
        the model produces BEFORE tool calls in the same iteration
        (Claude-style narration). The caller treats it as live text.

    {"type": "tool_call_start",  "name":..., "args":..., "label":...}
    {"type": "tool_call_done",   "name":..., "ok":bool, "label":...}
        Surround each tool dispatch. The frontend renders these as
        status chips ("查询 CS122A sections...").

    {"type": "cards_proposed",   "cards": [...]}
        Emitted right after a propose_recommendation tool dispatch
        succeeds. Carries the enriched course-card payload the
        frontend renders as click-to-add tiles. Caller is expected
        to accumulate the latest batch into the final SSE meta event
        (last call wins).

    {"type": "final",            "text":..., "iterations":N, "tool_calls":M,
                                 "truncated": bool (optional)}
        Clean termination. text is the full accumulated assistant
        content from the final iteration (already streamed via
        "token" events; provided again for persistence).
        truncated=True means this answer came from the no-tools
        fallback after a budget limit was hit (see limit_reached).

    {"type": "limit_reached",    "reason": "max_iterations" | "max_tool_calls",
                                 "iterations": N, "tool_calls": M,
                                 "continuation_id": "<token>"}
        Tool-call budget exhausted. Emitted BEFORE the fallback final
        so the frontend can render a "Continue" button. The
        continuation_id can be passed to resume_agent() within
        CONTINUATION_TTL_S to resume the loop with the stashed
        message history and a fresh budget.

    {"type": "error",            "message":"..."}
        Fatal LLM error. Caller is expected to fall back to the
        legacy handler path. Budget exhaustion no longer surfaces
        as error — see limit_reached above.

Safety / fallback:
    MAX_ITERATIONS caps how many tool-call rounds the model gets.
    MAX_TOTAL_TOOLS caps cumulative tool dispatches across rounds.
    On limit, we stash the conversation, emit limit_reached, then
    do ONE more evidence-bounded LLM call. A ready recommendation
    may stage cards; unresolved hard conditions require clarification.

Cancellation:
    asyncio.CancelledError propagates out unchanged — the user hit
    Stop, the SSE consumer cancelled the producer, we cancel the
    LLM call. Same model as stream_answer_llm.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import logging
import re
import secrets
import time
from typing import AsyncIterator, Optional

from app import config, observability
from app.agent import tools as agent_tools
from app.agent import prefilter
from app.agent import restriction_payload
from app.agent.deep_search_history import (
    build_history_instruction_message,
    history_refresh_missing,
    load_history_hint,
    record_run_trace,
    verification_required_text,
)
from app.agent.deep_search_state import DeepSearchRunState
from app.agent.workflow_router import (
    build_primary_workflow_plan,
    build_route_hint_message,
    route_solution,
)
from app.llm.safety import untrusted_context_message

logger = logging.getLogger(__name__)

_GRADE_CONTEXT_KEYS = ("_zotistics_results", "_grade_charts", "_recommendation_sections",
                       "_ranking_profile", "_recommendation_proposed", "_recommendation_card_facts",
                       "query_course_ids", "_prefilter_state", "_controlled_recommendation",
                       "_controlled_recommendation_cards")


def _grade_snapshot(context):
    return {key: deepcopy(context[key]) for key in _GRADE_CONTEXT_KEYS if key in (context or {})}


def uses_controlled_recommendation_output(query, query_term_source="default", *, snapshot=None):
    """Protect opted-in recommendation turns, including their continuations."""
    if (snapshot or {}).get("_controlled_recommendation") is True:
        return True
    return ((config.dynamic_prefilter_enabled() or config.recommendation_ranking_enabled())
            and prefilter.is_recommendation_request(query, query_term_source))


def recommendation_cards_for_display(cards, language="zh"):
    """Normalize only the new presentation path; never publish model diagnostics."""
    from app.recommendations.presentation import normalize_recommendation_card
    return [normalize_recommendation_card(card, language) for card in cards or []
            if isinstance(card, dict)]


def merge_recommendation_cards(existing, incoming):
    """Keep earlier positions while applying explicit newer same-course choices."""
    from app.terms import parse_term_key

    def key(card):
        parsed = parse_term_key(str(card.get("term") or ""))
        term = parsed.terms[0].canonical_name if parsed.kind == "single" else str(card.get("term") or "")
        return prefilter.course_key(card.get("course_id")) or card.get("course_id"), term

    combined = list(existing or [])
    positions = {key(card): index for index, card in enumerate(combined)}
    for card in incoming or []:
        identity = key(card)
        if identity in positions:
            combined[positions[identity]] = card
        else:
            positions[identity] = len(combined)
            combined.append(card)
    return combined


def controlled_tool_arguments(args):
    """Status events expose identities, never an unvalidated model-authored reason."""
    result = {}
    if args.get("term"):
        from app.terms import parse_term_key
        parsed = parse_term_key(str(args["term"]))
        if parsed.kind == "single":
            result["term"] = parsed.terms[0].canonical_name
    if args.get("course_id"):
        cid = prefilter.course_key(args["course_id"])
        if cid:
            result["course_id"] = cid
    if isinstance(args.get("items"), list):
        result["items"] = [{"course_id": cid} for item in args["items"] if isinstance(item, dict)
                           and (cid := prefilter.course_key(item.get("course_id")))]
    return result


def _controlled_card_event(context, staged, term, *, fallback=False):
    cards = recommendation_cards_for_display(staged, context.get("response_language") or "zh")
    if fallback:
        cards = merge_recommendation_cards(context.get("_controlled_recommendation_cards"), cards)
    context["_controlled_recommendation_cards"] = cards
    _record_recommendation_card_facts(context, cards, term)
    return {"type": "cards_proposed", "cards": deepcopy(cards), "replace": True,
            "controlled_recommendation": True, **({"from_fallback": True} if fallback else {})}


def controlled_recommendation_result(context, *, incomplete=False, failure_reason=None):
    """Render only validated backend facts, never a provider's prose."""
    from app.recommendations.presentation import render_recommendation_answer
    cards = recommendation_cards_for_display(context.get("_controlled_recommendation_cards"),
                                             context.get("response_language") or "zh")
    context["_controlled_recommendation_cards"] = cards
    gate = prefilter.recommendation_gate(context)
    state = context.get("_prefilter_state") or {}
    plan = state.get("plan") or {}
    requested_plan = state.get("requested_plan") or plan
    ge_intent = any(candidate.get("course_scope") == "ge"
                    or (candidate.get("hard_filters") or {}).get("only_unmet_ge") is True
                    for candidate in (plan, requested_plan))
    incomplete = bool(incomplete or not cards or (gate.get("enabled") and not gate.get("ready"))
                      or (ge_intent
                          and any((card.get("ge_evidence") or {}).get("is_ge") is not True for card in cards)))
    from app.agent import profile_followup
    # The recommendation comes first; a question about the record follows only a complete one.
    complete = bool(cards) and not incomplete and not failure_reason
    follow_up = profile_followup.closing_note(context, ask=complete)
    if complete and context.get("_plan_review"):
        # A request for "next term's courses" was read as a term plan; say how to ask for the other kind.
        zh = str(context.get("response_language") or "zh").startswith("zh")
        hint = ("这是按毕业进度排的学期计划。如果你想要的是按给分或教授评分挑出来的一批课，直接告诉我。" if zh else
                "This is a term plan built from your degree progress. If you wanted a list picked by grades "
                "or professor ratings instead, just say so.")
        follow_up = "\n\n".join(part for part in (hint, follow_up) if part)
    from app.recommendations.presentation import plan_review_text
    lead = plan_review_text(context.get("_plan_review"), context.get("response_language") or "zh")
    text = render_recommendation_answer(cards, context.get("response_language") or "zh", follow_up=follow_up,
                                        lead=lead,
                                        gate=gate, incomplete=incomplete, failure_reason=failure_reason,
                                        plan=plan, requested_count=state.get("requested_count"),
                                        requested_plan=state.get("requested_plan"))
    def intent(candidate):
        return {"course_scope": candidate.get("course_scope", "any"),
                "hard_filters": {"only_unmet_ge": (candidate.get("hard_filters") or {}).get("only_unmet_ge") is True}}
    return {"text": text, "cards": deepcopy(cards), "controlled_recommendation": True,
            "recommendation_gate": gate, "incomplete": bool(incomplete),
            "presentation_plan": intent(plan), "requested_plan": intent(requested_plan),
            **({"requested_count": state["requested_count"]} if state.get("requested_count") is not None else {}),
            **({"follow_up": follow_up} if follow_up else {}),
            **({"lead": lead} if lead else {}),
            **({"failure_reason": failure_reason} if failure_reason else {})}


def _controlled_failure_events(context, reason):
    result = controlled_recommendation_result(context, incomplete=True, failure_reason=reason)
    return [{"type": "token", "text": result["text"], "controlled_recommendation": True},
            {"type": "final", **result}]

MAX_ITERATIONS = 12
# Allow course searches, eligibility checks, and professor lookups enough
# room to reach recommendation cards and the final explanation.
MAX_TOTAL_TOOLS = 50
IDEMPOTENT_TERM_READ_TOOLS = frozenset(
    agent_tools.TERM_SCOPED_TOOLS - {"propose_recommendation"}
)
_OFFERING_SEARCH_MARKERS = (
    "schedule of classes",
    "class schedule",
    "websoc",
    "offered",
    "offering",
    "sections",
    "开课",
    "排课",
    "课表",
)
_REDUNDANT_OFFERING_PAGE_MARKERS = (
    "reg.uci.edu/perl/websoc",
    "websoc.reg.uci.edu",
    "api.peterportal.org",
    "peterportal.org",
    "coursicle.com",
    "web.archive.org",
)

# When a budget limit is hit we stash the in-progress conversation so
# the user can click "Continue" to resume with a fresh budget. State
# lives in-process (single-worker FastAPI dev setup); restart wipes
# everything, which is fine — a stale continuation_id just yields a
# clean error from resume_agent().
CONTINUATION_TTL_S = 600  # 10 min — long enough to read + decide
_continuation_store: dict[str, dict] = {}
_LOG_VALUE_MAX_CHARS = 160


def _gc_continuations() -> None:
    now = time.time()
    stale = [k for k, v in _continuation_store.items()
             if now - v["created_at"] > CONTINUATION_TTL_S]
    for k in stale:
        _continuation_store.pop(k, None)


def _log_value(value) -> object:
    if isinstance(value, str):
        clean = value.replace("\n", " ")
        return clean if len(clean) <= _LOG_VALUE_MAX_CHARS else clean[:157] + "..."
    if isinstance(value, list):
        if len(value) <= 5:
            return [_log_value(item) for item in value]
        return {
            "count": len(value),
            "sample": [_log_value(item) for item in value[:5]],
        }
    if isinstance(value, dict):
        return {
            str(k): _log_value(v)
            for k, v in list(value.items())[:8]
        }
    return value


def _summarize_tool_args(args: dict) -> dict:
    out = {}
    for key, value in (args or {}).items():
        if key == "items" and isinstance(value, list):
            out[key] = {"count": len(value)}
        else:
            out[key] = _log_value(value)
    return out


def _idempotent_tool_key(name: str, args: dict) -> Optional[str]:
    if (name == "get_live_sections" and args.get("force_refresh") is True
            and (config.dynamic_prefilter_enabled() or config.recommendation_ranking_enabled())):
        return None
    if name not in IDEMPOTENT_TERM_READ_TOOLS:
        return None
    return f"{name}:{json.dumps(args or {}, sort_keys=True, default=str, separators=(',', ':'))}"


_FAILURE_KEY_IGNORED_ARGS = frozenset({"restriction_type", "follow_links", "reason", "max_results"})


# Failures that the same arguments will reproduce within one turn. Other
# errors (a prefilter asking for a step first, a timeout) may pass on retry.
_FAILURES_NOT_WORTH_REPEATING = frozenset({
    "department_not_in_term",
    "websoc_comments_missing",
    "websoc_department_unavailable",
    "provider_network_error",
})


def _failed_call_key(name: str, args: dict) -> str:
    """Identity of a lookup for "this already failed in this turn".

    Arguments that only change how a result is read (the restriction kind,
    the stated reason for a search) are left out: the same department that
    had no notices a moment ago has none under another restriction_type.
    """
    kept = {k: v for k, v in (args or {}).items()
            if k not in _FAILURE_KEY_IGNORED_ARGS and v not in (None, "", [])}
    return f"{name}:{json.dumps(kept, sort_keys=True, default=str, separators=(',', ':'))}"


def _repeated_failure_result(previous: dict) -> dict:
    return {
        "ok": False,
        "error_code": "repeated_failed_call",
        "message": (
            "This exact lookup already failed in this turn and was not run again. "
            "Change the arguments (for a department, use a code from term_departments) "
            "or answer with what is verified."
        ),
        "previous_error": previous,
    }


def _failure_summary(result: dict) -> dict:
    return {key: result[key] for key in ("error_code", "error", "message") if result.get(key)}


def _record_definitive_course_offering(
    context: dict,
    name: str,
    args: dict,
    result: dict,
) -> None:
    if name == "get_course_offerings":
        rows = result.get("offerings") or []
        # Only stop supplemental research when every requested row is resolved.
        # One definitive term must not suppress research for another term whose
        # lookup failed. Historical references do not resolve the future target.
        if rows and all(
            row.get("authoritative") is True
            and row.get("source") == "registrar_websoc"
            and row.get("offering_status") in {"offered", "not_offered"}
            for row in rows
        ):
            for row in rows:
                _record_definitive_course_offering(
                    context, "get_sections", args,
                    {**row, "course_id": result.get("course_id") or args.get("course_id")},
                )
        return
    if (
        name != "get_sections"
        or result.get("source") != "registrar_websoc"
        or result.get("authoritative") is not True
        or result.get("offering_status") not in {"offered", "not_offered"}
    ):
        return
    record = {
        "course_id": str(result.get("course_id") or args.get("course_id") or ""),
        "term": str(result.get("term") or args.get("term") or ""),
        "offering_status": result.get("offering_status"),
        "source_url": result.get("source_url"),
    }
    records = context.setdefault("_definitive_course_offerings", [])
    identity = (record["course_id"], record["term"])
    if not any((item.get("course_id"), item.get("term")) == identity for item in records):
        records.append(record)


def _definitive_offering_stop_reason(
    name: str,
    args: dict,
    *,
    context: dict,
) -> Optional[str]:
    records = context.get("_definitive_course_offerings") or []
    if not records:
        return None

    if name == "fetch_page":
        url = str(args.get("url") or "").lower()
        if any(marker in url for marker in _REDUNDANT_OFFERING_PAGE_MARKERS):
            return (
                "official Registrar WebSoc already returned a definitive course-offering "
                "result; redundant offering-page fetch is disabled"
            )

    if name == "web_search":
        query = str(args.get("query") or "")
        query_lower = query.lower()
        query_compact = re.sub(r"[^a-z0-9]", "", query_lower)
        for record in records:
            course_compact = re.sub(
                r"[^a-z0-9]",
                "",
                str(record.get("course_id") or "").lower(),
            )
            if (
                course_compact
                and course_compact in query_compact
                and any(marker in query_lower for marker in _OFFERING_SEARCH_MARKERS)
            ):
                return (
                    "official Registrar WebSoc already returned a definitive result for "
                    f"{record['course_id']} in {record['term']}; redundant offering search "
                    "is disabled"
                )

    if name == "get_live_sections":
        requested_course = re.sub(
            r"[^a-z0-9]", "", str(args.get("course_id") or "").lower()
        )
        requested_term = str(args.get("term") or "")
        for record in records:
            record_course = re.sub(
                r"[^a-z0-9]", "", str(record.get("course_id") or "").lower()
            )
            if (requested_course == record_course and requested_term == record.get("term")
                    and record.get("offering_status") == "not_offered"):
                return (
                    "official Registrar WebSoc already resolved this historical offering; "
                    "a secondary live-availability lookup is not applicable"
                )
    return None


def _add_offering_event_fields(event: dict, name: str, result: dict) -> None:
    if name != "get_sections":
        return
    offering_status = result.get("offering_status")
    if not offering_status:
        offering_status = "offered" if result.get("sections") else "unavailable"
    event.update(
        {
            "source": result.get("source"),
            "offering_status": offering_status,
            "authoritative": result.get("authoritative") is True,
            "section_count": len(result.get("sections") or []),
            "fetch_summary": result.get("fetches") or [],
        }
    )


def _summarize_tool_result(result: dict) -> dict:
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    summary = {
        "ok": result.get("ok"),
        "found": result.get("found"),
        "error": result.get("error"),
        "error_code": result.get("error_code"),
        "provider": result.get("provider"),
        "source": result.get("source"),
        "workflow_id": result.get("workflow_id"),
        "source_url": result.get("source_url"),
        "final_url": result.get("final_url"),
        "status_code": result.get("status_code"),
        "extraction_status": result.get("extraction_status"),
        "offering_status": result.get("offering_status"),
        "lookup_attempts": result.get("lookup_attempts"),
        "search_criteria": result.get("search_criteria"),
        "registration_ends": result.get("registration_ends"),
    }
    if "results" in result and isinstance(result["results"], list):
        summary["result_count"] = len(result["results"])
        summary["result_domains"] = [
            item.get("domain") for item in result["results"][:5]
            if isinstance(item, dict)
        ]
        summary["result_urls"] = [
            item.get("url") for item in result["results"][:5]
            if isinstance(item, dict)
        ]
    if "links" in result and isinstance(result["links"], list):
        summary["link_count"] = len(result["links"])
        summary["link_urls"] = [
            item.get("url") for item in result["links"][:10]
            if isinstance(item, dict)
        ]
    if "key_passages" in result and isinstance(result["key_passages"], list):
        summary["key_passage_count"] = len(result["key_passages"])
    linked_pages = result.get("linked_pages")
    if isinstance(linked_pages, dict):
        pages = linked_pages.get("pages") or []
        errors = linked_pages.get("errors") or []
        summary["linked_page_count"] = len(pages)
        summary["linked_page_urls"] = [
            page.get("url") for page in pages[:10]
            if isinstance(page, dict)
        ]
        summary["linked_page_error_count"] = len(errors)
    if "sections" in result and isinstance(result["sections"], list):
        summary["section_count"] = len(result["sections"])
    if "courses" in result and isinstance(result["courses"], list):
        summary["course_count"] = len(result["courses"])
    if "staged_count" in result:
        summary["staged_count"] = result.get("staged_count")
        summary["skipped_count"] = result.get("skipped_count")
    return {k: v for k, v in summary.items() if v is not None}


def _agent_web_research_summary(tool_name: str, result: dict) -> Optional[dict]:
    """Summarize only URLs that the tool actually fetched, not search hits.

    Sources read from the local snapshot or reused from the 24-hour page cache
    were not requested this time; they are counted separately as ``reused``.
    """
    if not isinstance(result, dict):
        return None
    fetched_urls: list[str] = []
    failure_count = 0
    reused_count = 0
    if tool_name == "get_department_restrictions":
        sources = [
            item
            for item in (result.get("fetch_summary") or [])
            if isinstance(item, dict)
        ]
        fetches = [item for item in sources if not item.get("local") and not item.get("cached")]
        reused_count = len(sources) - len(fetches)
        fetched_urls.extend(
            item.get("final_url") or item.get("url")
            for item in fetches
            if item.get("final_url") or item.get("url")
        )
        failure_count = sum(item.get("ok") is False for item in fetches)
        success_count = sum(item.get("ok") is not False for item in fetches)
    elif tool_name == "fetch_page":
        fetched = result.get("final_url") or result.get("source_url")
        if fetched:
            fetched_urls.append(fetched)
        failure_count = 0 if result.get("ok") else 1
        success_count = len(fetched_urls) if result.get("ok") else 0
    else:
        return None

    fetched_urls = list(dict.fromkeys(fetched_urls))
    return {
        "fetched_urls": fetched_urls,
        "success_count": success_count,
        "failure_count": failure_count,
        "reused_count": reused_count,
    }


def _result_has_term_data(result: dict) -> bool:
    """Whether a term-scoped tool found at least one course with a section."""
    if not isinstance(result, dict) or result.get("found") is False:
        return False
    if isinstance(result.get("offerings"), list):
        return any(row.get("offering_status") == "offered" for row in result["offerings"])
    sections = result.get("sections")
    if isinstance(sections, list) and sections:
        return True
    courses = result.get("courses")
    if isinstance(courses, list):
        return any(
            isinstance(course, dict) and bool(course.get("sections"))
            for course in courses
        )
    return int(result.get("staged_count") or 0) > 0


def _latest_user_content(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _messages_with_run_hints(
    messages: list[dict],
    hint_messages: list[dict[str, str]],
) -> list[dict]:
    if not hint_messages:
        return messages
    last_user_idx = None
    for idx, message in enumerate(messages):
        if message.get("role") == "user":
            last_user_idx = idx
    if last_user_idx is None:
        return [*hint_messages, *messages]
    return [
        *messages[:last_user_idx],
        *hint_messages,
        *messages[last_user_idx:],
    ]


def _workflow_result_message(
    route: dict,
    results: list[dict],
) -> Optional[dict[str, str]]:
    if not results:
        return None
    llm_results = [_compact_workflow_record(record) for record in results]
    return untrusted_context_message(
        "Primary workflow results",
        json.dumps(
            {"route": route, "results": llm_results},
            ensure_ascii=False,
            default=str,
        ),
    )


def _workflow_instruction_message(results: list[dict], user_query: str = "") -> Optional[dict[str, str]]:
    """Keep trusted execution rules independent from tool/source evidence."""
    if not results:
        return None
    other_task = prefilter.is_recommendation_request(user_query) or bool(re.search(
        r"评价|好不好|难度|给分|课表|上课时间|先修|GPA|RMP|"
        r"\b(?:ratings?|reviews?|professors?|prerequisites?|schedules?)\b",
        user_query, re.I,
    ))
    restriction_only = bool(user_query) and not other_task and all(
        record.get("tool") == "get_department_restrictions"
        for record in results
    )
    restriction_instruction = ""
    if restriction_only:
        restriction_instruction = (
            " The deterministic verified_facts block is already visible to the "
            "user. Add at most two short sentences of explanation or a next-step "
            "question. Do not repeat dates, sources, exceptions, tables, or the "
            "fact block. Do not infer access before an event's effective_at, and "
            "do not restate school affiliation as an enrollment rule."
        )
    return {
        "role": "system",
        "content": (
            "The server already executed the developer-owned primary workflow before "
            "this model call. Do not call these primary tools again for the same completed "
            "course, term and arguments. An unresolved co-class reference may require a related "
            "course's target-term timetable; that is a new lookup scope, not a repeated primary call. Use the results "
            "in the separate context data as the primary evidence, report structured failures honestly, and "
            "only use web_search/fetch_page for optional supplemental evidence."
            f"{restriction_instruction}"
        ),
    }


def _runtime_hint_messages(context, *, workflow_route=None, primary_results=None,
                           history_hint_message=None):
    """Rebuild current execution evidence for normal, fallback and resumed calls."""
    records = primary_results or []
    hints = [hint for hint in (
        build_route_hint_message(workflow_route),
        _workflow_instruction_message(records, context.get("user_query") or ""),
        _workflow_result_message(workflow_route, records),
        build_history_instruction_message() if history_hint_message else None,
        history_hint_message,
    ) if hint is not None]
    from app.recommendations.pairing import contract_enabled
    if contract_enabled():
        hints.append({"role": "system", "content": (
            "Current discussion policy: user_confirmed_product_policy; "
            "policy_id=user_confirmed_discussion_presence, policy_version=1, user_confirmed=true. "
            "On verified complete unfiltered official target-term course timetables, listed Dis means "
            "discussion required under this product policy; no listed Dis means no discussion required "
            "under this policy. This supersedes earlier blanket coexistence/absence prohibitions only "
            "for those complete current facts; it is not a university-wide Registrar registration rule. "
            "Course instructions and co-class references require the related target-term timetable lookup. "
            "Respect needs_lookup and required_course_components; failed or partial lookups stay unresolved. "
            "Do not invent cross-course matching relationships. Before asserting a hard weekday/time "
            "condition is met, verify the lecture plus every required Dis/other component as a complete "
            "combination. TBA is unknown; ON LINE alone does not establish asynchronous teaching or "
            "a free weekday. Explicit asynchronous evidence and scheduled online meetings are different. "
            "This policy also applies during budget finalization and Continue and never relaxes hard conditions."
        )})
    hints.extend(prefilter.instruction_messages(context))
    gate = prefilter.recommendation_gate(context)
    if gate["enabled"]:
        hints.extend([{
            "role": "system",
            "content": (
                "The server's recommendation readiness applies to this answer, budget finalization, "
                "and Continue. A failed or unresolved plan is not an unconstrained request. "
                "Continue or a budget limit never relaxes or cancels the user's hard conditions. "
                "If readiness is false, do not call propose_recommendation or present a formal "
                "recommendation as meeting those conditions. State what is unresolved and ask for "
                "the missing information or clarification; retained candidates are not confirmed eligible. "
                "Public web searches and course GE tags cannot verify a student's private GE audit "
                "or establish which requirements remain. Do not route missing personal audit evidence "
                "to web_search/fetch_page. Never infer asynchronous meetings from ON LINE/TBA. "
                "Use the existing Python ranking when enabled; do not invent weights, treat adjusted "
                "scores as raw grade rates, or generate chart HTML."
            ),
        }, untrusted_context_message("recommendation readiness", gate)])
    card_facts = context.get("_recommendation_card_facts")
    if card_facts and card_facts.get("cards"):
        hints.extend([{
            "role": "system", "content": (
                "The current recommendation card facts describe the actual UI selection. When your "
                "answer says a lecture is selected or recommended, its code, instructor, days and "
                "time must match selected_lecture and primary_code. A different section must be "
                "explicitly described as an alternative; discussing it does not change the card. "
                "If required_secondary is unknown or the selected pairing has needs_lookup=true, "
                "never claim pure lecture, no Discussion/Lab, or that entering only the lecture code "
                "is sufficient. An empty secondary list after filtering does not prove no secondary "
                "is required, and does not certify a complete combination satisfies a hard time condition. "
                "A verified complete official target-term timetable may establish listed/no-listed Dis "
                "under user_confirmed_product_policy; cite the official listing separately from the "
                "product's rule and do not present it as a university-wide mandate. "
                "If unresolved includes ge_labels_unknown, describe any source-recorded GE labels as "
                "unverified; never say those labels are confirmed, database-confirmed or verified. "
                "Course labels do not prove the student's remaining personal GE requirements. "
                "Preserve each ranking feature's evidence scope. Instructor-wide grades cannot be "
                "compared to course-instructor grades as a difference between two sections of this course. "
                "Read explicit scope fields; never infer a shared scope from numeric differences or similarities. "
                "Scores are not raw A rates. These card facts do not waive the recommendation readiness "
                "gate or confirm unresolved enrollment conditions."
            ),
        }, untrusted_context_message("current recommendation card facts", card_facts)])
    return hints


def _record_recommendation_card_facts(context, cards, term):
    """Keep only the latest same-term card selections, without the full grade payload."""
    from app.terms import parse_term_key

    def canonical(value):
        parsed = parse_term_key(value)
        return parsed.terms[0].canonical_name if parsed.kind == "single" else None

    def section_fact(section):
        if not section:
            return None
        fact = {
            "code": section.get("code") or section.get("section_code"),
            "num": section.get("num") or section.get("section_num"),
            "type": section.get("type") or section.get("section_type"),
            "days": section.get("days"),
            "time": section.get("time") or section.get("time_display") or (
                f"{section['start_time']}–{section['end_time']}"
                if section.get("start_time") and section.get("end_time") else None),
        }
        if section.get("instructors"):
            fact["instructors"] = deepcopy(section["instructors"])
        return fact

    target = canonical(term)
    rows = []
    for card in cards or []:
        if not isinstance(card, dict) or not target or canonical(card.get("term")) != target:
            continue
        primary_code = card.get("primary_code")
        groups = card.get("section_groups") or []
        selected_group = next((group for group in groups
                               if primary_code is not None
                               and str((group.get("primary") or {}).get("code")) == str(primary_code)), {})
        selected = selected_group.get("primary") or next((section for section in card.get("sections") or []
                    if primary_code is not None and str(section.get("section_code")) == str(primary_code)), {})
        warnings = [*(card.get("prefilter") or {}).get("warnings", []),
                    *(selected.get("prefilter") or {}).get("warnings", [])]
        unresolved = list(dict.fromkeys(warning["code"] for warning in warnings
                                       if isinstance(warning, dict) and isinstance(warning.get("code"), str)))
        from app.recommendations.pairing import normalize_pairing_evidence, pairing_for_primary
        contract = normalize_pairing_evidence(card, term=target)
        pairing = pairing_for_primary(contract, primary_code)
        evidence = pairing.get("evidence") or {}
        requirement = pairing.get("requirement", "unknown") if evidence.get("status") == "verified" else "unknown"
        source_proof = card.get("pairing_source_proof") or {}
        required_courses = pairing.get("required_course_components") or evidence.get("required_course_components") or []
        references = evidence.get("co_class_references") or []
        pairing_fact = {
            "requirement": requirement, "facts_source": evidence.get("source"),
            "policy_source": evidence.get("policy_source"), "policy_id": evidence.get("policy_id"),
            "policy_version": evidence.get("policy_version"), "user_confirmed": evidence.get("user_confirmed") is True,
            "needs_lookup": evidence.get("needs_lookup") is True or pairing.get("needs_lookup") is True,
            "relationship": pairing.get("relationship", "unknown"),
            "required_components": deepcopy(pairing.get("required_components") or []),
            "required_course_components": [{key: deepcopy(value[key]) for key in
                ("course_id", "term", "requirement", "required_components", "needs_lookup", "status") if key in value}
                for value in required_courses if isinstance(value, dict)],
            "co_class_references": [{key: value[key] for key in ("course_id", "term", "relation") if key in value}
                                    for value in references if isinstance(value, dict)],
            "official_table_complete": all(source_proof.get(key) is True for key in (
                "full_unfiltered_request", "response_identity_validated", "response_document_complete", "source_rows_fully_parsed")),
        }
        ranking = card.get("ranking") or selected.get("ranking") or {}
        rows.append({
            "course_id": card.get("course_id"), "primary_code": primary_code,
            "selected_lecture": section_fact(selected),
            "alternative_lectures": [section_fact(group["primary"]) for group in groups
                                     if group.get("primary") and group is not selected_group],
            "required_secondary": requirement,
            "pairing": pairing_fact,
            "selection_verification": {key: deepcopy((card.get("selection_verification") or {}).get(key))
                                       for key in ("status", "primary_code", "selected_secondary_codes")},
            "secondary_type": card.get("secondary_type"),
            "secondary_options": [section_fact(section) for section in selected_group.get("secondaries") or []],
            "prefilter_status": (card.get("prefilter") or {}).get("status"), "unresolved": unresolved,
            "ge_category": card.get("ge_category"),
            "ranking": {"final_score": ranking.get("final_score"), "feature_scopes": {
                name: {key: feature[key] for key in ("scope", "instructor_name", "valid") if key in feature}
                for name, feature in (ranking.get("features") or {}).items() if isinstance(feature, dict)
            }},
        })
    context["_recommendation_card_facts"] = {
        "term": target, "request_unresolved": deepcopy((context.get("_prefilter_state") or {}).get("unresolved_constraints") or []),
        "cards": rows,
    }


def _compact_workflow_record(record: dict) -> dict:
    if record.get("tool") != "get_department_restrictions":
        return record
    result = record.get("result") or {}
    bundle = result.get("evidence_bundle") or {}
    keep_ids = {
        bundle.get("primary_event_id"),
        *(bundle.get("related_event_ids") or []),
        *(
            event_id
            for conflict in bundle.get("conflicts") or []
            for event_id in conflict.get("event_ids") or []
        ),
    }
    compact_bundle = {
        key: bundle.get(key)
        for key in (
            "query",
            "primary_event_id",
            "related_event_ids",
            "eligibility",
            "sources",
            "evidence_status",
            "missing_required_fields",
            "conflicts",
        )
    }
    compact_bundle["events"] = [
        event
        for event in bundle.get("events") or []
        if event.get("event_id") in keep_ids
    ]
    compact_result = {
        key: result.get(key)
        for key in (
            "ok",
            "error_code",
            "message",
            "term",
            "department",
            "course_id",
            "restriction_type",
            "restriction_query",
            "source_url",
            "verified_facts",
            "notice_statements",
            "fetch_summary",
        )
        if result.get(key) is not None
    }
    compact_result["evidence_bundle"] = compact_bundle
    return {**record, "result": compact_result}


def _clarification_text(
    user_query: str,
    clarification: dict,
    response_language: Optional[str] = None,
) -> str:
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", user_query or ""))
    language = response_language or ("zh" if has_cjk else "en")
    key = "message_zh" if language == "zh" else "message_en"
    return str(clarification.get(key) or clarification.get("message_en") or "")


def _stash_continuation(
    messages: list,
    *,
    user_id: str,
    term: Optional[str],
    default_term: Optional[str],
    allowed_query_terms: list[str],
    query_term_source: str,
    response_language: str,
    pending_schedule: Optional[list[dict]],
    iterations_used: int,
    tool_calls_used: int,
    deep_search_state: DeepSearchRunState,
    history_hint_message: Optional[dict[str, str]],
    grade_context: Optional[dict] = None,
) -> str:
    _gc_continuations()
    cid = secrets.token_urlsafe(16)
    _continuation_store[cid] = {
        "messages": list(messages),  # shallow copy — entries are dicts we won't mutate
        "user_id":  user_id,
        "term":     term,
        "default_term": default_term,
        "allowed_query_terms": list(allowed_query_terms),
        "query_term_source": query_term_source,
        "response_language": response_language,
        "pending_schedule": list(pending_schedule or []),
        "iterations_used":  iterations_used,
        "tool_calls_used":  tool_calls_used,
        "deep_search_state": deep_search_state,
        "history_hint_message": history_hint_message,
        "grade_context": _grade_snapshot(grade_context),
        "created_at": time.time(),
    }
    logger.info("[agent] stashed continuation %s (%d msgs, %d iters, %d tools)",
                cid, len(messages), iterations_used, tool_calls_used)
    return cid


def pop_continuation(continuation_id: str) -> Optional[dict]:
    _gc_continuations()
    return _continuation_store.pop(continuation_id, None)


async def run_agent(
    messages: list[dict],
    *,
    client,
    model: str,
    user_id: str,
    term: Optional[str] = None,
    default_term: Optional[str] = None,
    allowed_query_terms: Optional[list[str]] = None,
    query_term_source: str = "default",
    response_language: str = "en",
    pending_schedule: Optional[list[dict]] = None,
    offering_course_ids: Optional[list[str]] = None,
) -> AsyncIterator[dict]:
    """
    Run the agent loop on a prebuilt messages list. `messages` is
    mutated in place (assistant + tool messages are appended each
    round) so the caller can inspect the full trace.

    ``allowed_query_terms`` is the backend-resolved immutable allowlist.
    ``term`` remains only as a representative compatibility value used by
    deterministic workflow routing.
    """
    user_query = _latest_user_content(messages)
    deep_search_state = DeepSearchRunState(query=user_query)
    resolved_allowed_terms = list(
        allowed_query_terms
        if allowed_query_terms is not None
        else ([term] if term else [])
    )
    matches, history_hint_message = load_history_hint(user_query)
    deep_search_state.history_matches = matches
    async for event in _run_loop(
        messages, client=client, model=model,
        user_id=user_id, term=term,
        default_term=default_term or term,
        allowed_query_terms=resolved_allowed_terms,
        query_term_source=query_term_source,
        response_language=response_language,
        pending_schedule=pending_schedule,
        offering_course_ids=offering_course_ids,
        start_iteration=0, start_tool_count=0,
        user_query=user_query,
        deep_search_state=deep_search_state,
        history_hint_message=history_hint_message,
    ):
        yield event


async def resume_agent(
    continuation_id: str,
    *,
    client,
    model: str,
) -> AsyncIterator[dict]:
    """
    Resume a previously stashed agent loop. Called when the user
    clicks "Continue" after a limit_reached event. Pops the snapshot
    so it can't be replayed twice. The general agent budget resets, but
    deep-search visited memory and its 8-page/depth budget remain attached
    to the same answer.

    Yields the same event protocol as run_agent.
    """
    snap = pop_continuation(continuation_id)
    if not snap:
        yield {"type": "error",
               "message": "continuation_id not found or expired"}
        return

    # Append a nudge so the model knows the user wants it to finish.
    # Without this the model often just re-asks for clarification
    # since it doesn't otherwise know why it was re-invoked.
    messages = list(snap["messages"]) + [{
        "role": "user",
        "content": (
            "Please continue from where you left off. Use the "
            "information you've already gathered (visible in the "
            "tool results above) to finalize your answer. Only call "
            "more tools if there's a specific gap you still need to "
            "fill. Continue does not relax or cancel hard conditions. If a required "
            "condition is unresolved, resolve the evidence gap or ask for clarification "
            "instead of presenting it as satisfied. "
            f"Respond in {'Chinese' if snap.get('response_language') == 'zh' else 'English'}."
        ),
    }]
    logger.info("[agent] resuming continuation %s (was %d iters / %d tools)",
                continuation_id, snap["iterations_used"], snap["tool_calls_used"])

    async for event in _run_loop(
        messages, client=client, model=model,
        user_id=snap["user_id"], term=snap["term"],
        default_term=snap.get("default_term") or snap.get("term"),
        allowed_query_terms=snap.get("allowed_query_terms") or [],
        query_term_source=snap.get("query_term_source") or "default",
        response_language=snap.get("response_language") or "en",
        pending_schedule=snap.get("pending_schedule") or [],
        start_iteration=0, start_tool_count=0,
        user_query=snap["deep_search_state"].query,
        deep_search_state=snap["deep_search_state"],
        history_hint_message=snap.get("history_hint_message"),
        grade_context=snap.get("grade_context"),
    ):
        yield event


async def _run_loop(
    messages: list[dict],
    *,
    client,
    model: str,
    user_id: str,
    term: Optional[str],
    default_term: Optional[str],
    allowed_query_terms: list[str],
    query_term_source: str,
    response_language: str,
    pending_schedule: Optional[list[dict]],
    start_iteration: int,
    start_tool_count: int,
    user_query: str,
    deep_search_state: DeepSearchRunState,
    history_hint_message: Optional[dict[str, str]],
    offering_course_ids: Optional[list[str]] = None,
    grade_context: Optional[dict] = None,
) -> AsyncIterator[dict]:
    """The actual iteration body, shared by run_agent and resume_agent."""
    tool_context = {
        "user_id": user_id,
        "term": term,
        "default_term": default_term,
        "allowed_query_terms": list(allowed_query_terms),
        "query_term_source": query_term_source,
        "response_language": response_language,
        "pending_schedule": list(pending_schedule or []),
        "user_query": user_query,
        "deep_search_state": deep_search_state,
    }
    from app.catalog.normalization import iter_course_mentions
    tool_context["query_course_ids"] = list(offering_course_ids or []) or list(dict.fromkeys(
        ref.display() for ref, _, _ in iter_course_mentions(user_query)
    ))
    tool_context.update(_grade_snapshot(grade_context))
    controlled_output = uses_controlled_recommendation_output(
        user_query, query_term_source, snapshot=tool_context)
    if controlled_output:
        tool_context["_controlled_recommendation"] = True
        tool_context.setdefault("_controlled_recommendation_cards", [])
        yield {"type": "response_mode", "controlled_recommendation": True,
               "response_language": response_language}
    await prefilter.initialize(tool_context, messages)
    workflow_route = route_solution(user_query, term=term)
    primary_plan = build_primary_workflow_plan(workflow_route)
    if (offering_course_ids and allowed_query_terms and not primary_plan.get("calls")
            and not primary_plan.get("clarification")
            and (query_term_source in {"comparison", "history"}
                 or re.search(r"开课|开设|有开|开过|谁教|教授|offered|teaches|taught|instructors", user_query, re.I))):
        primary_plan["calls"] = [
            {"tool": "get_course_offerings", "workflow_id": "course_offerings",
             "args": {"course_id": course, "terms": list(allowed_query_terms)}}
            for course in list(dict.fromkeys(offering_course_ids))[:8]
        ]
    if primary_plan.get("deferred"):
        observability.log_event(
            logger, logging.INFO, "workflow_primary_deferred",
            workflow_ids=workflow_route.get("workflow_ids"),
            reason=primary_plan["deferred"].get("reason"), selected_term=term,
        )
    clarification = primary_plan.get("clarification")
    if clarification:
        clarification_text = _clarification_text(
            user_query,
            clarification,
            response_language,
        )
        observability.log_event(
            logger,
            logging.INFO,
            "workflow_primary_blocked",
            workflow_ids=workflow_route.get("workflow_ids"),
            reason=clarification.get("reason"),
            selected_term=term,
            explicit_terms=workflow_route.get("explicit_terms"),
            departments=workflow_route.get("departments"),
            courses=workflow_route.get("course_ids"),
        )
        yield {"type": "token", "text": clarification_text}
        yield {
            "type": "final",
            "text": clarification_text,
            "iterations": 0,
            "tool_calls": start_tool_count,
            "clarification_required": True,
        }
        return

    total_tool_calls = start_tool_count
    forced_results: dict[str, dict] = {}
    idempotent_results: dict[str, dict] = {}
    restriction_dedupe = restriction_payload.RestrictionPayloadDeduper()
    failed_calls: dict[str, dict] = {}
    primary_result_records: list[dict] = []
    for primary_call in primary_plan.get("calls") or []:
        tool_name = primary_call["tool"]
        args, term_error = agent_tools.resolve_tool_arguments(
            tool_name,
            primary_call["args"],
            context=tool_context,
        )
        label = agent_tools.humanize_tool_call(tool_name, args, response_language)
        total_tool_calls += 1
        observability.log_event(
            logger,
            logging.INFO,
            "workflow_primary_tool_start",
            workflow_id=primary_call.get("workflow_id"),
            tool=tool_name,
            label=label,
            args=_summarize_tool_args(args),
            server_forced=True,
        )
        yield {
            "type": "tool_call_start",
            "name": tool_name,
            "args": args,
            "label": label,
            "server_forced": True,
            "response_language": response_language,
        }
        result = (
            {"error": f"invalid term for {tool_name}: {term_error}"}
            if term_error
            else agent_tools.dispatch(tool_name, args, context=tool_context)
        )
        if asyncio.iscoroutine(result):
            result = await result
        _record_definitive_course_offering(tool_context, tool_name, args, result)
        cache_key = _idempotent_tool_key(tool_name, args) if not term_error else None
        if cache_key is not None:
            idempotent_results[cache_key] = result
        tool_ok = "error" not in result and result.get("ok", True) is not False
        forced_results[tool_name] = result
        primary_result_records.append(
            {
                "workflow_id": primary_call.get("workflow_id"),
                "tool": tool_name,
                "args": args,
                "result": result,
            }
        )
        observability.log_event(
            logger,
            logging.INFO if tool_ok else logging.WARNING,
            "workflow_primary_tool_done",
            workflow_id=primary_call.get("workflow_id"),
            tool=tool_name,
            label=label,
            ok=tool_ok,
            result=_summarize_tool_result(result),
            server_forced=True,
        )
        web_audit = _agent_web_research_summary(tool_name, result)
        if web_audit is not None:
            observability.log_event(
                logger,
                logging.INFO if tool_ok else logging.WARNING,
                "agent_web_research_summary",
                workflow_id=primary_call.get("workflow_id"),
                tool=tool_name,
                **web_audit,
            )
        tool_done_event = {
            "type": "tool_call_done",
            "name": tool_name,
            "ok": tool_ok,
            "label": label,
            "args": args,
            "term_data_available": _result_has_term_data(result),
            "server_forced": True,
        }
        _add_offering_event_fields(tool_done_event, tool_name, result)
        if tool_name == "get_department_restrictions":
            tool_done_event["restriction_evidence"] = result.get("evidence_bundle")
            tool_done_event["verified_facts"] = result.get("verified_facts")
            tool_done_event["fetch_summary"] = result.get("fetch_summary") or []
        yield tool_done_event

    restriction_result = next(
        (
            record["result"]
            for record in primary_result_records
            if record.get("tool") == "get_department_restrictions"
            and isinstance(record.get("result"), dict)
        ),
        None,
    )
    restriction_bundle = (
        restriction_result.get("evidence_bundle")
        if restriction_result
        else None
    )
    restriction_status = (
        restriction_bundle.get("evidence_status")
        if isinstance(restriction_bundle, dict)
        else None
    )
    if restriction_status in {"partial", "unavailable"}:
        verified_facts = restriction_result.get("verified_facts") or {}
        deterministic_text = verified_facts.get("summary_markdown") or (
            "已抓取官方来源，但没有足够证据验证该限制的具体时间。"
        )
        yield {"type": "token", "text": deterministic_text}
        yield {
            "type": "final",
            "text": deterministic_text,
            "iterations": 0,
            "tool_calls": total_tool_calls,
            "restriction_evidence": restriction_bundle,
            "verified_facts": verified_facts,
            "deterministic_restriction_answer": True,
        }
        return

    restriction_prefix = ""
    if restriction_status in {"verified", "conflicting"}:
        verified_facts = restriction_result.get("verified_facts") or {}
        restriction_prefix = verified_facts.get("summary_markdown") or ""
        if restriction_prefix:
            restriction_prefix += "\n\n"
            if not controlled_output:
                yield {"type": "token", "text": restriction_prefix}

    tool_context["_forced_workflow_results"] = forced_results
    observability.increment("solution_router.routes", route=workflow_route["route_type"])
    if workflow_route["route_type"] == "workflow":
        for intent in workflow_route.get("intents", []):
            observability.increment("workflow_router.matches", intent=intent)
    for iteration in range(start_iteration, MAX_ITERATIONS):
        hint_messages = _runtime_hint_messages(
            tool_context, workflow_route=workflow_route, primary_results=primary_result_records,
            history_hint_message=history_hint_message,
        )
        request_messages = _messages_with_run_hints(messages, hint_messages)
        prompt_layers = observability.estimate_prompt_layers(
            request_messages,
            tool_schemas=agent_tools.TOOL_SCHEMAS,
        )
        observability.increment(
            "llm.input_tokens",
            prompt_layers["total_input"],
            model=model,
            source="layered_estimate",
        )
        observability.log_event(
            logger,
            logging.INFO,
            "llm_prompt_layers",
            model=model,
            iteration=iteration,
            **prompt_layers,
        )
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=request_messages,
                tools=agent_tools.TOOL_SCHEMAS,
                tool_choice="auto",
                stream=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[agent] LLM call failed (iter %d): %s: %s",
                         iteration, type(e).__name__, e)
            if controlled_output:
                for event in _controlled_failure_events(tool_context, "model_unavailable"):
                    yield event
                return
            yield {"type": "error", "message": f"LLM call failed: {e}"}
            return

        accumulated_content = ""
        # DeepSeek's reasoning-mode models emit `reasoning_content`
        # deltas (the model's internal chain of thought). We don't
        # forward these to the user, but we MUST capture them and
        # echo them back on the assistant message — DeepSeek rejects
        # follow-up calls otherwise with "reasoning_content in the
        # thinking mode must be passed back to the API".
        accumulated_reasoning = ""
        # Tool-call accumulator keyed by `index` (the model can emit
        # multiple concurrent tool_calls; deltas carry their index).
        tool_calls_acc: dict[int, dict] = {}

        try:
            async for chunk in response:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                if delta.content:
                    accumulated_content += delta.content
                    if not controlled_output:
                        yield {"type": "token", "text": delta.content}

                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    accumulated_reasoning += reasoning

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        slot = tool_calls_acc.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""}
                        )
                        if tc_delta.id:
                            slot["id"] = tc_delta.id
                        fn = tc_delta.function
                        if fn:
                            if fn.name:
                                slot["name"] = fn.name
                            if fn.arguments:
                                slot["arguments"] += fn.arguments
        except asyncio.CancelledError:
            logger.info("[agent] cancelled at iter %d", iteration)
            raise
        except Exception:
            if controlled_output:
                for event in _controlled_failure_events(tool_context, "model_stream_interrupted"):
                    yield event
                return
            raise

        # ── If no tool calls came back, this iteration's content IS
        #    the final answer. We've already streamed the tokens; emit
        #    the "final" event with the full text for persistence.
        if not tool_calls_acc:
            final_text = restriction_prefix + accumulated_content
            verification_missing = history_refresh_missing(deep_search_state)
            if verification_missing:
                final_text = verification_required_text(user_query)
            controlled_result = None
            if controlled_output:
                controlled_result = controlled_recommendation_result(
                    tool_context, incomplete=bool(verification_missing),
                    failure_reason="evidence_unverified" if verification_missing else None)
                final_text = controlled_result["text"]
                yield {"type": "token", "text": final_text, "controlled_recommendation": True}
            trace_result = record_run_trace(deep_search_state, final_text)
            final_event = {
                "type": "final",
                "text": final_text,
                "iterations": iteration + 1,
                "tool_calls": total_tool_calls,
            }
            if controlled_result is not None:
                final_event.update(controlled_result)
            if tool_context.get("_grade_charts") and not tool_context.get("_recommendation_proposed"):
                final_event["grade_charts"] = tool_context["_grade_charts"]
            if prefilter.active(tool_context):
                final_event["prefilter"] = prefilter.summary(tool_context)
            if verification_missing:
                final_event["verification_required"] = True
            if restriction_bundle:
                final_event["restriction_evidence"] = restriction_bundle
                final_event["verified_facts"] = (
                    restriction_result.get("verified_facts") or {}
                )
            if trace_result.get("stored"):
                final_event["deep_search_trace_id"] = trace_result.get("trace_id")
            yield final_event
            return

        # ── Tool-call iteration. Append the assistant message that
        #    requested the calls (OpenAI tool-use protocol requires
        #    this so the model can see its own tool_calls when we
        #    feed back the results).
        ordered = [tool_calls_acc[k] for k in sorted(tool_calls_acc.keys())]
        assistant_msg: dict = {
            "role": "assistant",
            "content": accumulated_content or None,
            "tool_calls": [
                {
                    "id":   tc["id"],
                    "type": "function",
                    "function": {
                        "name":      tc["name"],
                        "arguments": tc["arguments"] or "{}",
                    },
                }
                for tc in ordered
            ],
        }
        if accumulated_reasoning:
            # DeepSeek thinking-mode requirement (see comment above).
            assistant_msg["reasoning_content"] = accumulated_reasoning
        messages.append(assistant_msg)

        for tc in ordered:
            if total_tool_calls >= MAX_TOTAL_TOOLS:
                # Mid-iteration budget hit. The assistant message
                # already in `messages` declared all `ordered` tool
                # calls; some have tool responses appended, but the
                # remaining ones don't. OpenAI/DeepSeek reject any
                # follow-up call (including our no-tools fallback)
                # unless EVERY tool_call_id has a matching tool
                # response. Synthesize "budget exhausted" stubs for
                # the unfilled ids so the message list is valid for
                # both the fallback call and a future resume.
                already_responded: set[str] = set()
                for m in reversed(messages):
                    if m.get("role") == "tool":
                        already_responded.add(m.get("tool_call_id"))
                    elif m.get("role") == "assistant":
                        break
                for ptc in ordered:
                    if ptc["id"] and ptc["id"] not in already_responded:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": ptc["id"],
                            "content": json.dumps({
                                "error": "tool-call budget exhausted before this tool ran",
                            }),
                        })
                async for ev in _emit_limit_reached_and_fallback(
                    messages,
                    reason="max_tool_calls",
                    iterations_used=iteration + 1,
                    tool_calls_used=total_tool_calls,
                    user_id=user_id, term=term,
                    default_term=default_term,
                    allowed_query_terms=allowed_query_terms,
                    query_term_source=query_term_source,
                    response_language=response_language,
                    pending_schedule=pending_schedule,
                    deep_search_state=deep_search_state,
                    history_hint_message=history_hint_message,
                    client=client, model=model,
                    grade_context=tool_context,
                    workflow_route=workflow_route,
                    primary_results=primary_result_records,
                ):
                    yield ev
                return
            total_tool_calls += 1

            # Parse args defensively — DeepSeek occasionally emits
            # malformed JSON on the first token of a delta; treat as
            # empty rather than crashing the whole turn.
            try:
                args = json.loads(tc["arguments"] or "{}")
            except json.JSONDecodeError as e:
                logger.warning("[agent] bad tool args from model (%s): %r → %s",
                               tc["name"], tc["arguments"], e)
                args = {}

            args, term_error = agent_tools.resolve_tool_arguments(
                tc["name"],
                args,
                context=tool_context,
            )

            visible_args = controlled_tool_arguments(args) if controlled_output else args
            label = agent_tools.humanize_tool_call(tc["name"], visible_args, response_language)
            logger.info("[agent] iter=%d tool[%d/%d] %s args=%s",
                        iteration, total_tool_calls + 1, MAX_TOTAL_TOOLS,
                        tc["name"], {k: args.get(k) for k in list(args)[:4]})
            observability.log_event(
                logger,
                logging.INFO,
                "agent_tool_call_start",
                tool=tc["name"],
                label=label,
                iteration=iteration,
                tool_index=total_tool_calls + 1,
                max_tools=MAX_TOTAL_TOOLS,
                args=_summarize_tool_args(args),
            )
            yield {"type": "tool_call_start",
                   "name": tc["name"], "args": visible_args, "label": label,
                   "response_language": response_language}

            professor_grades = []
            forced_cache = tool_context.get("_forced_workflow_results") or {}
            cache_key = _idempotent_tool_key(tc["name"], args) if not term_error else None
            reused_result = False
            stop_reason = (
                _definitive_offering_stop_reason(
                    tc["name"],
                    args,
                    context=tool_context,
                )
                if not term_error
                else None
            )
            failed_key = _failed_call_key(tc["name"], args)
            if failed_key in failed_calls:
                result = _repeated_failure_result(failed_calls[failed_key])
                observability.log_event(
                    logger, logging.INFO, "repeated_failed_call_blocked",
                    tool=tc["name"], args=_summarize_tool_args(args),
                )
            elif term_error:
                result = {"error": f"invalid term for {tc['name']}: {term_error}"}
            elif stop_reason:
                result = {
                    "ok": False,
                    "error": stop_reason,
                    "error_code": "definitive_offering_already_resolved",
                }
                observability.log_event(
                    logger,
                    logging.INFO,
                    "definitive_offering_tool_blocked",
                    tool=tc["name"],
                    args=_summarize_tool_args(args),
                    reason=stop_reason,
                )
            elif tc["name"] in forced_cache and not (tc["name"] == "get_live_sections" and args.get("force_refresh") is True
                    and (config.dynamic_prefilter_enabled() or config.recommendation_ranking_enabled())):
                result = forced_cache[tc["name"]]
                reused_result = True
                observability.log_event(
                    logger,
                    logging.INFO,
                    "workflow_primary_tool_reused",
                    tool=tc["name"],
                    label=label,
                    server_forced=True,
                )
            elif cache_key is not None and cache_key in idempotent_results:
                result = idempotent_results[cache_key]
                reused_result = True
                observability.log_event(
                    logger,
                    logging.INFO,
                    "agent_tool_result_reused",
                    tool=tc["name"],
                    label=label,
                    args=_summarize_tool_args(args),
                )
            else:
                if tc["name"] == "propose_recommendation":
                    await asyncio.to_thread(
                        agent_tools.prepare_recommendation_grades,
                        args.get("items") or [], args.get("term") or term, tool_context,
                    )
                if tc["name"] in {"get_professor_rating", "summarize_professor_reviews"}:
                    professor_grades = await asyncio.to_thread(
                        agent_tools.prepare_professor_grades, args, tool_context,
                    )
                if (tc["name"] in {"get_sections", "get_live_sections", "get_zotistics_grades", "propose_recommendation", "search_courses", "get_professor_rating"}
                        or prefilter.active(tool_context)):
                    result = await asyncio.to_thread(agent_tools.dispatch, tc["name"], args, context=tool_context)
                else:
                    result = agent_tools.dispatch(tc["name"], args, context=tool_context)
            # Some tool dispatchers (e.g. summarize_professor_reviews,
            # which calls the LLM internally) return a coroutine instead
            # of a dict. Await it here so the tool response is always a
            # plain dict by the time we serialize it.
            if asyncio.iscoroutine(result):
                try:
                    result = await result
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("[agent] async tool %s failed: %s: %s",
                                   tc["name"], type(e).__name__, e)
                    result = {"error": f"{type(e).__name__}: {e}"}
            if professor_grades and isinstance(result, dict):
                result = {**result, "historical_grades": professor_grades}
            if (tc["name"] == "get_live_sections" and args.get("force_refresh") is True
                    and (config.dynamic_prefilter_enabled() or config.recommendation_ranking_enabled())):
                # A new rule/timetable snapshot invalidates earlier read tools,
                # including on Continue. Never replay a pre-refresh proof.
                idempotent_results.clear()
                for name in ("get_sections", "get_live_sections", "search_courses"):
                    (tool_context.get("_forced_workflow_results") or {}).pop(name, None)
            _record_definitive_course_offering(
                tool_context,
                tc["name"],
                args,
                result,
            )
            if result.get("error_code") == "repeated_failed_call":
                pass
            elif cache_key is not None and not term_error:
                idempotent_results[cache_key] = result
            if term_error or result.get("error_code") in _FAILURES_NOT_WORTH_REPEATING:
                failed_calls.setdefault(failed_key, _failure_summary(result))
            model_result = result
            if tc["name"] == restriction_payload.TOOL_NAME:
                # The model gets the asked department's part of the official
                # page, and a later department that links to the same page
                # points back to what the model already has this turn.
                model_result, trimmed = restriction_dedupe.model_view(result)
                if trimmed:
                    observability.log_event(
                        logger, logging.INFO, "restriction_payload_trimmed",
                        tool=tc["name"], department=result.get("department"),
                        result_chars=len(json.dumps(result, default=str)),
                        model_chars=len(json.dumps(model_result, default=str)),
                        **trimmed,
                    )
                if (result.get("evidence_bundle") or {}).get("evidence_status") == "verified":
                    # A verified date is the answer. Without this the model kept
                    # checking neighbouring departments and ran out of rounds.
                    model_result = {
                        **model_result,
                        "next_step": (
                            "The restriction evidence for this department is verified. "
                            "Answer now from evidence_bundle and verified_facts; make no "
                            "further lookups unless the user asked about something else."
                        ),
                    }
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(model_result, default=str),
            })

            tool_ok = "error" not in result and result.get("ok", True) is not False
            observability.log_event(
                logger,
                logging.INFO,
                "agent_tool_call_done",
                tool=tc["name"],
                label=label,
                ok=tool_ok,
                iteration=iteration,
                tool_index=total_tool_calls,
                result=_summarize_tool_result(result),
            )
            web_audit = _agent_web_research_summary(tc["name"], result)
            if web_audit is not None and tc["name"] not in forced_cache:
                observability.log_event(
                    logger,
                    logging.INFO if tool_ok else logging.WARNING,
                    "agent_web_research_summary",
                    tool=tc["name"],
                    **web_audit,
                )
            tool_done_event = {
                "type": "tool_call_done",
                "name": tc["name"],
                "ok": tool_ok,
                "label": label,
                "args": visible_args,
                "term_data_available": _result_has_term_data(result),
                "reused": reused_result,
            }
            _add_offering_event_fields(tool_done_event, tc["name"], result)
            if tc["name"] == "get_department_restrictions":
                tool_done_event["restriction_evidence"] = result.get(
                    "evidence_bundle"
                )
                tool_done_event["verified_facts"] = result.get("verified_facts")
                tool_done_event["fetch_summary"] = result.get("fetch_summary") or []
            yield tool_done_event

            # Side-channel: propose_recommendation stages structured
            # cards on the tool_context dict (the LLM-visible return is
            # a short ack). Pop them here and emit a cards_proposed
            # event so the SSE consumer can ship them in the meta
            # event without round-tripping kilobytes through the
            # model's context window.
            staged = tool_context.pop("_proposed_cards", None)
            if staged is not None:
                if controlled_output:
                    yield _controlled_card_event(tool_context, staged,
                                                 result.get("term") or args.get("term") or term)
                else:
                    _record_recommendation_card_facts(tool_context, staged, result.get("term") or args.get("term") or term)
            if staged and not controlled_output:
                yield {"type": "cards_proposed", "cards": staged}

    # Iteration cap exhausted without a final answer.
    logger.warning("[agent] exceeded MAX_ITERATIONS=%d, %d tool calls used",
                   MAX_ITERATIONS, total_tool_calls)
    async for ev in _emit_limit_reached_and_fallback(
        messages,
        reason="max_iterations",
        iterations_used=MAX_ITERATIONS,
        tool_calls_used=total_tool_calls,
        user_id=user_id, term=term,
        default_term=default_term,
        allowed_query_terms=allowed_query_terms,
        query_term_source=query_term_source,
        response_language=response_language,
        pending_schedule=pending_schedule,
        deep_search_state=deep_search_state,
        history_hint_message=history_hint_message,
        client=client, model=model,
        grade_context=tool_context,
        workflow_route=workflow_route,
        primary_results=primary_result_records,
    ):
        yield ev


async def _emit_limit_reached_and_fallback(
    messages: list[dict],
    *,
    reason: str,
    iterations_used: int,
    tool_calls_used: int,
    user_id: str,
    term: Optional[str],
    default_term: Optional[str],
    allowed_query_terms: list[str],
    query_term_source: str,
    response_language: str,
    pending_schedule: Optional[list[dict]],
    deep_search_state: DeepSearchRunState,
    history_hint_message: Optional[dict[str, str]],
    client,
    model: str,
    grade_context: Optional[dict] = None,
    workflow_route: Optional[dict] = None,
    primary_results: Optional[list[dict]] = None,
) -> AsyncIterator[dict]:
    """
    Common tail behavior when a budget limit trips:
      1. Stash `messages` and emit limit_reached with the continuation_id
      2. Run one evidence-bounded LLM call, allowing cards only when ready
      3. Emit a final event marked truncated=True

    Cancellation (user hits Stop while the fallback is streaming)
    propagates unchanged.
    """
    grade_context = _grade_snapshot(grade_context)
    controlled_output = uses_controlled_recommendation_output(
        deep_search_state.query, query_term_source, snapshot=grade_context)
    if controlled_output:
        grade_context["_controlled_recommendation"] = True
        grade_context.setdefault("_controlled_recommendation_cards", [])
    cid = _stash_continuation(
        messages,
        user_id=user_id, term=term,
        default_term=default_term,
        allowed_query_terms=allowed_query_terms,
        query_term_source=query_term_source,
        response_language=response_language,
        pending_schedule=pending_schedule,
        iterations_used=iterations_used,
        tool_calls_used=tool_calls_used,
        deep_search_state=deep_search_state,
        history_hint_message=history_hint_message,
        grade_context=grade_context,
    )
    yield {
        "type": "limit_reached",
        "reason": reason,
        "iterations": iterations_used,
        "tool_calls": tool_calls_used,
        "continuation_id": cid,
        "response_language": response_language,
    }

    tool_context: dict = {
        "user_id": user_id,
        "term": term,
        "default_term": default_term,
        "allowed_query_terms": list(allowed_query_terms),
        "query_term_source": query_term_source,
        "response_language": response_language,
        "pending_schedule": list(pending_schedule or []),
        "user_query": deep_search_state.query,
    }
    tool_context.update(_grade_snapshot(grade_context))
    if controlled_output:
        yield {"type": "response_mode", "controlled_recommendation": True,
               "response_language": response_language}
    gate = prefilter.recommendation_gate(tool_context)
    proposal_allowed = not gate["enabled"] or gate["ready"]
    # Build a fallback prompt that nudges the model to wrap up with
    # what it has. We DON'T mutate `messages` (it's been stashed) —
    # we build a throwaway list for this one call.
    fallback_messages = list(messages) + [{
        "role": "user",
        "content": (
            "The tool-call budget for this turn has "
            "been reached. Write an evidence-bounded final answer using only "
            "the information already gathered above. "
            f"Respond in {'Chinese' if response_language == 'zh' else 'English'}. "
            "Do not mention tool-call budgets, iteration limits, tokens, or "
            "internal system limits in the user-facing answer. Explain what "
            "the evidence supports and what still needs checking. Never invent "
            "a verified course count, eligibility, or missing information. "
            "If evidence is insufficient, say so plainly. The interface will "
            "offer a button to continue; do not claim checks are still running "
            "or will resume automatically. "
            "ONE exception: only if this is a course-recommendation turn, the server's "
            "readiness gate permits it, the candidates are grounded, and "
            "you have not yet called `propose_recommendation`, you may "
            "call it once now to stage the card list; all other tools are disabled. "
            "If readiness is false or hard conditions remain unresolved, do not stage cards "
            "or claim those conditions are met; explain the gap and ask for clarification. "
            "Budget exhaustion never relaxes a hard condition. Otherwise, write a clean "
            "prose answer. Do NOT emit XML, DSML, `<invoke>`, or any "
            "raw tool-call markup — you have at most ONE legitimate "
            "tool available, use it via the normal tool_calls channel."
        ),
    }]
    fallback_messages = _messages_with_run_hints(fallback_messages, _runtime_hint_messages(
        tool_context, workflow_route=workflow_route, primary_results=primary_results,
        history_hint_message=history_hint_message,
    ))

    # Allow ONE last propose_recommendation call so the LLM has a way
    # to stage cards even on the fallback path. Everything else stays
    # disabled — otherwise the loop could spin forever.
    fallback_tools = [
        s for s in agent_tools.TOOL_SCHEMAS
        if s["function"]["name"] == "propose_recommendation"
    ] if proposal_allowed else []

    fallback_prompt_layers = observability.estimate_prompt_layers(
        fallback_messages,
        tool_schemas=fallback_tools,
    )
    observability.log_event(
        logger,
        logging.INFO,
        "llm_prompt_layers",
        model=model,
        iteration="fallback",
        **fallback_prompt_layers,
    )
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=fallback_messages,
            **({"tools": fallback_tools, "tool_choice": "auto"} if fallback_tools else {}),
            stream=True,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("[agent] fallback LLM call failed: %s: %s",
                     type(e).__name__, e)
        if controlled_output:
            for event in _controlled_failure_events(tool_context, "model_unavailable"):
                yield {**event, "truncated": True}
            return
        yield {"type": "error",
               "message": f"fallback LLM call failed: {e}"}
        return

    accumulated = ""
    fallback_tool_calls: dict[int, dict] = {}
    try:
        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                accumulated += delta.content
                if not controlled_output:
                    yield {"type": "token", "text": delta.content}
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    slot = fallback_tool_calls.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc_delta.id:
                        slot["id"] = tc_delta.id
                    fn = tc_delta.function
                    if fn:
                        if fn.name:
                            slot["name"] = fn.name
                        if fn.arguments:
                            slot["arguments"] += fn.arguments
    except asyncio.CancelledError:
        logger.info("[agent] cancelled during fallback finalize")
        raise
    except Exception:
        if controlled_output:
            for event in _controlled_failure_events(tool_context, "model_stream_interrupted"):
                yield {**event, "truncated": True}
            return
        raise

    # Dispatch any propose_recommendation the LLM staged in the
    # fallback. We don't loop again — this is the last call. No
    # tool_call_start/done chips: by this point the message body has
    # already streamed, and a trailing "staging cards…" chip would
    # render out of order. Cards arrive silently via cards_proposed.
    for tc in fallback_tool_calls.values():
        if tc["name"] != "propose_recommendation" or not proposal_allowed:
            continue
        try:
            args = json.loads(tc["arguments"] or "{}")
        except json.JSONDecodeError as e:
            logger.warning("[agent fallback] bad propose_recommendation args: %s", e)
            continue
        logger.info("[agent fallback] late propose_recommendation: items=%d term=%r",
                    len(args.get("items") or []), args.get("term"))
        result = await asyncio.to_thread(agent_tools.dispatch, tc["name"], args, context=tool_context)
        if "error" in result:
            logger.warning("[agent fallback] propose_recommendation dispatch failed: %s",
                           result.get("error"))
            continue
        staged = tool_context.pop("_proposed_cards", None)
        if staged is not None:
            if controlled_output:
                yield _controlled_card_event(tool_context, staged,
                                             result.get("term") or args.get("term") or term, fallback=True)
            else:
                _record_recommendation_card_facts(tool_context, staged, result.get("term") or args.get("term") or term)
        if staged and not controlled_output:
            logger.info("[agent fallback] staged %d cards", len(staged))
            # from_fallback=True signals to the caller that this batch
            # was synthesized late under truncation pressure; the LLM
            # may only have full data for a subset of the original
            # picks. The caller should MERGE these into any earlier
            # batch rather than overwrite — otherwise we silently
            # shrink the user's recommendation.
            yield {"type": "cards_proposed", "cards": staged, "from_fallback": True}

    if cid in _continuation_store:
        _continuation_store[cid]["grade_context"] = _grade_snapshot(tool_context)

    # Safety net: if the LLM bypassed the tool channel and dumped raw
    # XML / DSML markup as text (a known DeepSeek failure mode when it
    # *thinks* it has tools but the API rejected them), scrub it from
    # the final saved text. Tokens already streamed live; the frontend
    # re-renders from `final.text` so the saved/displayed history is
    # clean even if the user saw a flash of markup during streaming.
    cleaned = _scrub_tool_markup(accumulated)
    if cleaned != accumulated:
        logger.warning("[agent fallback] scrubbed %d chars of leaked tool markup",
                       len(accumulated) - len(cleaned))

    verification_missing = history_refresh_missing(deep_search_state)
    if verification_missing:
        cleaned = verification_required_text(deep_search_state.query)
    controlled_result = None
    if controlled_output:
        controlled_result = controlled_recommendation_result(
            tool_context, incomplete=True,
            failure_reason="evidence_unverified" if verification_missing else None)
        cleaned = controlled_result["text"]
        yield {"type": "token", "text": cleaned, "controlled_recommendation": True}
    trace_result = record_run_trace(deep_search_state, cleaned)
    final_event = {
        "type": "final",
        "text": cleaned,
        "iterations": iterations_used,
        "tool_calls": tool_calls_used,
        "truncated": True,
    }
    if controlled_result is not None:
        final_event.update(controlled_result)
    if tool_context.get("_grade_charts") and not tool_context.get("_recommendation_proposed"):
        final_event["grade_charts"] = tool_context["_grade_charts"]
    if prefilter.active(tool_context):
        final_event["prefilter"] = prefilter.summary(tool_context)
    if verification_missing:
        final_event["verification_required"] = True
    if trace_result.get("stored"):
        final_event["deep_search_trace_id"] = trace_result.get("trace_id")
    yield final_event


# DeepSeek (thinking mode) occasionally emits its internal "DSML"
# tool-call serialization as visible text when tools aren't available
# the way it expects. Strip any of these blocks so the saved final
# answer is clean prose, not pseudo-XML.
#
# The leaked block looks like:
#   < | | DSML | | tool_calls>
#   < | | DSML | | invoke name="propose_recommendation">
#   < | | DSML | | parameter name="items" string="false">[...]</| | DSML | | parameter>
#   < | | DSML | | parameter name="term" string="true">Fall 2026</| | DSML | | parameter>
#   </| | DSML | | invoke>
#   </| | DSML | | tool_calls>
#
# Strategy: kill the whole tool_calls span first (everything between
# opener and closer, inclusive, including parameter contents). Then
# scrub any orphan DSML tags left from mid-stream truncations.
_DSML_BLOCK = re.compile(
    r"<\s*\|?\s*\|?\s*DSML\b[^>]*\btool_calls\b[^>]*>"   # opener
    r"[\s\S]*?"                                          # contents
    r"</\s*\|?\s*\|?\s*DSML\b[^>]*\btool_calls\b[^>]*>", # closer
    re.IGNORECASE,
)
# Fallback: opener with no matching closer (mid-stream truncation) —
# strip from the first opener to end of string.
_DSML_OPEN_NO_CLOSE = re.compile(
    r"<\s*\|?\s*\|?\s*DSML\b[\s\S]*\Z",
    re.IGNORECASE,
)
# Catch-all for orphan tags that survived (e.g. </| | DSML | | parameter>
# alone, no opener) — just delete each tag.
_DSML_INLINE = re.compile(
    r"</?\s*\|?\s*\|?\s*DSML[^>]*>",
    re.IGNORECASE,
)


def _scrub_tool_markup(text: str) -> str:
    if not text:
        return text
    out = _DSML_BLOCK.sub("", text)
    out = _DSML_OPEN_NO_CLOSE.sub("", out)
    out = _DSML_INLINE.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()
