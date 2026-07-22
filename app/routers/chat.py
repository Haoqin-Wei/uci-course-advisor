"""
Chat Router — streams every product chat turn through the Agent loop.

The legacy non-streaming chat endpoint, pre-agent intent classifier,
template answer path, and old recommendation query path have been
removed. This router now handles:

  • deterministic hard-fact capture for explicit user statements
  • Agent-loop SSE streaming and continuation
  • validation of final text/cards before persistence
  • session-backed schedule mutations
"""

import asyncio
import logging
import re
from dataclasses import replace

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from app import config, observability
from app.auth.deps import current_user_optional
from app.auth.rate_limit import RateLimit, check_rate_limit
from app.modules.state import (
    get_or_create_session, update_session,
    load_student_into_session, get_known_fields,
)
from app.memory import get_memory_manager
from app.scheduling import (
    build_pending_schedule_bundle_items,
    calendar_day_names,
    resolve_pending_schedule_sections,
    validate_schedule_bundle,
)

# ── Phase 3.3 / 3.5 — session storage + decision detection ──
from app.data import sessions as sessions_data
from app.modules import decision_detector
# ─────────────────────────────────────────────────────────────

# ── Validation ───────────────────────────────────────────
from app.catalog.term import Term
from app.catalog.cache import get_catalog
from app.catalog.coverage import get_term_coverage
from app.catalog.departments import colloquial_course_id
from app.catalog.normalization import iter_course_mentions, parse_course_mention
from app.validation import (
    ValidationContext, validate, decide_action, apply_report, write_log,
)
from app.terms import parse_term_key
from app.terms.conversation import commit_conversation_resolution
from app.terms.service import QueryTermResolution, get_term_resolution_service
# ─────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

router = APIRouter()

CHAT_STREAM_LIMIT = RateLimit("chat.stream", limit=30, window_seconds=60)
CHAT_CONTINUE_LIMIT = RateLimit("chat.continue", limit=20, window_seconds=60)
SCHEDULE_WRITE_LIMIT = RateLimit("schedule.write", limit=120, window_seconds=60)

class ChatRequest(BaseModel):
    message: str
    session_id: str = ""
    term: Optional[str] = None                          # 新增
    system_prompt: Optional[str] = None                 # 新增：前端自定义 LLM system prompt


# ── Session ID resolution ─────────────────────────────────

def _resolve_session_id(
    req_session_id: str,
    user_id: str,
    term_str: Optional[str] = None,
) -> str:
    """
    Resolve the request's session_id.

    Contract:
      - empty session_id creates a new persistent session
      - existing sess_* continues that session
      - legacy/non-persistent keys such as "demo_session" are rejected
    """
    if not req_session_id:
        new_sid = sessions_data.create_session(
            user_id,
            title="New conversation",
            term_scope=term_str,
        )
        logger.info("[stream] new session %s created (frontend signalled new)", new_sid)
        return new_sid

    if req_session_id.startswith("sess_"):
        try:
            sessions_data.get_session_meta(user_id, req_session_id)
            return req_session_id
        except sessions_data.InvalidId as exc:
            raise HTTPException(
                status_code=400,
                detail="session_id must be empty or a valid persistent sess_* id",
            ) from exc
        except sessions_data.SessionNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail="Session not found",
            ) from exc

    raise HTTPException(
        status_code=400,
        detail=(
            "Legacy session_id values are no longer supported; "
            "send an empty session_id to create a session or an existing sess_* id"
        ),
    )


def _history_from_session_turns(
    user_id: str,
    persistent_session_id: str,
    *,
    limit: Optional[int] = None,
) -> list[dict]:
    """
    Return chat history from sessions/{session_id}/turns.jsonl.

    Chat history is intentionally not mirrored into app.modules.state.
    In-memory session state is reserved for structured planning fields
    until the rest of M2.2 migrates those fields to sessions.py too.
    """
    try:
        turns = sessions_data.read_turns(user_id, persistent_session_id)
    except sessions_data.SessionNotFound:
        return []
    history = [
        {"role": t.get("role"), "content": t.get("content")}
        for t in turns
        if t.get("role") in ("user", "assistant")
    ]
    if limit is not None:
        history = history[-limit:]
    return history


def _persist_turn(
    user_id: str,
    persistent_sid: str,
    user_msg: str,
    assistant_reply: str,
    *,
    cards: Optional[list] = None,
    followups: Optional[list] = None,
    validation: Optional[dict] = None,
) -> tuple[int, bool]:
    """
    Append user + assistant turns to sessions/{sid}/turns.jsonl.

    Returns:
        (turn_index, did_auto_title)
        - turn_index: the assistant reply's index (used for decision pinning)
        - did_auto_title: True iff we JUST replaced a placeholder title with
          a snippet of the user's message. The caller uses this signal to
          schedule an LLM-generated title via _maybe_schedule_auto_title.
          False means the title was already user-meaningful (don't touch it).

    Side effect: if this is the FIRST turn in the session (i.e. session
    meta currently has a placeholder title — either "New conversation"
    from chat.py's _resolve_session_id or "New session" from
    sessions.create_session's default), set a snippet title from the
    first 30 chars of the user message. Round 4's _auto_title_session
    then replaces that snippet with an LLM-generated short title.

    Failures here are logged but don't break the response.
    """
    did_auto_title = False
    try:
        sessions_data.append_turn(user_id, persistent_sid, "user", user_msg)
        idx = sessions_data.append_turn(
            user_id, persistent_sid, "assistant", assistant_reply,
            cards=cards, followups=followups, validation=validation,
        )

        # First-turn snippet title (cheap heuristic; Round 4 LLM auto-title
        # runs as a BackgroundTask and overwrites this shortly after).
        try:
            meta = sessions_data.get_session_meta(user_id, persistent_sid)
            placeholder_titles = {"New conversation", "New session", "", None}
            if meta.get("title") in placeholder_titles:
                snippet = (user_msg or "").strip().replace("\n", " ")
                if snippet:
                    title = snippet[:30] + ("…" if len(snippet) > 30 else "")
                    # update_session_meta takes **kwargs, not a dict
                    sessions_data.update_session_meta(
                        user_id, persistent_sid, title=title,
                    )
                    did_auto_title = True
                    logger.info("[stream] snippet-titled session %s → %r (LLM title pending)",
                                persistent_sid, title)
        except Exception as e:
            # Elevated from debug → warning so future regressions are
            # actually visible in the log.
            logger.warning("snippet-title failed for %s: %s: %s",
                           persistent_sid, type(e).__name__, e)

        return idx, did_auto_title
    except Exception as e:
        logger.warning("[stream] persist_turn failed for %s: %s", persistent_sid, e)
        return 0, False


# ── Round 4: LLM auto-title background task ──────────────

async def _auto_title_session(
    user_id: str,
    persistent_sid: str,
    user_msg: str,
    assistant_reply: str,
) -> None:
    """
    Fire-and-forget: ask the LLM for a short (5–10 char) title and
    overwrite the snippet title we set in _persist_turn.

    Triggered ONLY when _persist_turn returned did_auto_title=True, which
    means:
        - This is the first turn of a brand-new session, AND
        - The title was just set to a snippet placeholder by us.

    On any failure (LLM disabled, network error, parse failure, empty
    return) we log and keep the snippet title. No retry.
    """
    try:
        from app.llm.adapter import generate_session_title_llm

        title = await generate_session_title_llm(user_msg, assistant_reply)
        if not title:
            logger.info("[auto-title] LLM returned nothing for %s — keeping snippet",
                        persistent_sid)
            return

        sessions_data.update_session_meta(user_id, persistent_sid, title=title)
        logger.info("[auto-title] %s → %r", persistent_sid, title)
    except Exception as e:
        logger.warning("[auto-title] failed for %s: %s: %s",
                       persistent_sid, type(e).__name__, e)


def _maybe_schedule_auto_title(
    background_tasks: BackgroundTasks,
    did_auto_title: bool,
    user_id: str,
    persistent_sid: str,
    user_msg: str,
    assistant_reply: str,
) -> None:
    """
    If _persist_turn just put a snippet title on a brand-new session,
    schedule an LLM auto-title BackgroundTask. BackgroundTasks run AFTER
    the SSE stream closes, so the user never waits on this.

    Idempotent design: the task only fires when did_auto_title=True
    (i.e. first turn of a new session). Subsequent turns keep whatever
    title was set previously — we never overwrite an existing title.
    """
    if not did_auto_title:
        return
    background_tasks.add_task(
        _auto_title_session,
        user_id, persistent_sid, user_msg, assistant_reply,
    )


def _detect_and_pin_decisions(
    user_id: str,
    persistent_sid: str,
    user_msg: str,
    turn_index: int,
) -> None:
    """
    Run the heuristic decision detector on the user's latest message.
    Append any new decisions to session.decisions (idempotent against
    existing on lower-case match). Failures are logged but not fatal.
    """
    try:
        detected = decision_detector.detect_decisions(user_msg)
        for d in detected:
            result = sessions_data.append_decision(
                user_id, persistent_sid, d, from_turn=turn_index,
            )
            if not result.get("already_existed"):
                logger.info("[stream] pinned decision %r in session %s (turn %d)",
                            d, persistent_sid, turn_index)
    except Exception as e:
        logger.warning("[stream] decision-pinning failed: %s", e)


# ── Channel A: hard-fact capture (every turn) ────────────

def _merge_into_list(session: dict, field: str, items: list[str]) -> list[str]:
    existing = session.setdefault(field, [])
    existing_upper = {c.upper() for c in existing if c}
    newly_added = []
    for item in items:
        if not item:
            continue
        canonical = item.upper().strip()
        if canonical not in existing_upper:
            existing.append(item)
            existing_upper.add(canonical)
            newly_added.append(item)
    return newly_added


# Identity fields — go to MemoryManager profile (long-term identity)
_IDENTITY_FIELDS_FOR_PROFILE = ("major", "year", "target_gpa", "graduation_term")

# Stated-preference fields — go to MemoryManager facts as event-style entries
_PREFERENCE_FIELDS_AS_FACTS = {
    "difficulty_preference": "Stated difficulty preference: {value}",
    "recommendation_goal":   "Stated goal: {value}",
}


def _capture_hard_facts(session: dict, extracted: dict, user_id: str, mem) -> None:
    """
    Channel A: pull every explicitly-stated fact out of `extracted`,
    route it to session/profile/facts as appropriate, and emit INFO logs
    so the operator can see what was captured.
    """
    # ── Course status → session lists + MemoryManager facts ──
    currently_taking = extracted.pop("currently_taking", None) or []
    completed = extracted.pop("completed", None) or []

    if currently_taking:
        new_courses = _merge_into_list(session, "selected_courses", currently_taking)
        if new_courses:
            for c in new_courses:
                mem.add_fact(user_id, f"Currently taking {c}")
            logger.info("[Channel A] currently_taking captured → %s", new_courses)

    if completed:
        new_courses = _merge_into_list(session, "completed_courses", completed)
        if new_courses:
            for c in new_courses:
                mem.add_fact(user_id, f"Completed {c}")
            logger.info("[Channel A] completed captured → %s", new_courses)

    # ── Identity → MemoryManager profile ──
    profile_updates = {}
    for f in _IDENTITY_FIELDS_FOR_PROFILE:
        v = extracted.get(f) if f in ("major", "year") else extracted.pop(f, None)
        if v not in (None, "", []):
            profile_updates[f] = v
    if profile_updates:
        mem.update_profile(user_id, profile_updates)
        logger.info("[Channel A] profile updated → %s", profile_updates)

    # ── Stated preferences → MemoryManager facts ──
    for field, template in _PREFERENCE_FIELDS_AS_FACTS.items():
        v = extracted.get(field)
        if v in (None, "", []):
            continue
        mem.add_fact(user_id, template.format(value=v))
        logger.info("[Channel A] preference fact → %s=%s", field, v)


# ── Channel B: background reflection (every N turns) ─────

async def _run_reflection_task(user_id: str, history: list[dict]) -> None:
    """Fired as a FastAPI BackgroundTask AFTER the response is sent."""
    from app.llm.adapter import reflect_on_history_llm
    mem = get_memory_manager()
    if not mem.provider:
        logger.info("[Channel B] skipped: no memory provider")
        return
    existing = mem.get_preferences(user_id)
    new_prefs = await reflect_on_history_llm(history, existing)
    logger.info(
        "[Channel B] reflection ran (history=%d turns, existing prefs=%d) → %d new preferences: %s",
        len(history), len(existing), len(new_prefs), new_prefs,
    )
    for pref in new_prefs:
        mem.add_preference(user_id, pref)


def _course_ids_in_order(text: str) -> list[str]:
    seen, result = set(), []
    for ref, _start, _end in iter_course_mentions(text or ""):
        cid = colloquial_course_id(ref.department, ref.course_number)
        if cid not in seen:
            seen.add(cid)
            result.append(cid)
    return result


def _extract_deterministic_hard_facts(message: str) -> dict:
    """Small non-LLM extractor for explicit facts stated this turn."""
    text = message or ""
    lower = text.lower()
    updates: dict = {}

    if re.search(r"\b(i am|i'm|my major is|majoring in)\s+(cs|computer science)\b", lower):
        updates["major"] = "Computer Science"
    if re.search(r"\b(easy|easier|light workload|low workload)\b", lower):
        updates["difficulty_preference"] = "easy"

    course_ids = _course_ids_in_order(text)
    if course_ids:
        if re.search(r"\b(currently taking|taking|enrolled in)\b", lower):
            updates["currently_taking"] = course_ids
        elif re.search(r"\b(completed|finished|passed|took)\b", lower):
            updates["completed"] = course_ids
    return updates


# ── Legacy non-streaming chat route removed ──────────────
#
# `/api/chat/stream` is the only product chat entrypoint. Keep a tiny
# unregistered tombstone for internal callers/tests that import the
# symbol directly; no FastAPI route is attached to this function.

async def chat(*_args, **_kwargs):
    raise HTTPException(status_code=410, detail="Use /api/chat/stream")


def _maybe_schedule_reflection(
    background_tasks: BackgroundTasks,
    mem,
    session_id: str,
    user_id: str,
) -> None:
    if mem.should_reflect(session_id):
        history_snapshot = _history_from_session_turns(user_id, session_id, limit=12)
        logger.info(
            "[Channel B] scheduling reflection for turn %d (history=%d msgs)",
            mem.turn_count(session_id), len(history_snapshot),
        )
        background_tasks.add_task(
            _run_reflection_task,
            user_id=user_id,
            history=history_snapshot,
        )


def _retrieved_from_cards(cards: list[dict]) -> dict:
    primary: list[dict] = []
    flagged: list[dict] = []
    for card in cards or []:
        course_id = card.get("course_id")
        if not course_id:
            continue
        item = {"course": {"course_id": course_id}}
        if card.get("prereq_status") in {"not_met", "unknown"} or card.get("prereq_met") is False:
            flagged.append(item)
        else:
            primary.append(item)
    return {
        "primary": primary,
        "flagged": flagged,
        "total_found": len(primary) + len(flagged),
    }


def _validate_response(
    *,
    answer: str,
    cards: list[dict],
    retrieved: Optional[dict],
    state: dict,
    user_message: str,
    term_str: Optional[str],
    session_id: Optional[str],
    retrieval_performed: bool = False,
    query_terms: Optional[list[str]] = None,
    tool_terms: Optional[list[str]] = None,
) -> tuple[str, list[dict], Optional[dict]]:
    target_term = (
        Term.parse(term_str or "")
        or Term.parse(state.get("term", ""))
    )
    if not target_term:
        logger.info("[validation] skipped (no target term)")
        return answer, cards, None

    catalog = get_catalog(target_term)
    if not catalog:
        logger.info("[validation] skipped (no catalog for %s)", target_term.term_id)
        return answer, cards, None

    ctx = ValidationContext(
        llm_answer=answer,
        retrieved=retrieved or _retrieved_from_cards(cards),
        catalog=catalog,
        session_state=state,
        cards=cards,
        user_message=user_message,
        retrieval_performed=retrieval_performed,
        query_terms=query_terms or [],
        tool_terms=tool_terms or [],
        validation_term=term_str,
    )
    report = validate(ctx)
    action = decide_action(report)
    final_answer, final_cards, changed = apply_report(answer, cards, report, action)
    write_log(ctx, report, action, changed, session_id=session_id)
    validation_dict = report.to_dict()
    validation_dict["applied_action"] = action.value
    validation_dict["query_terms"] = query_terms or []
    validation_dict["tool_terms"] = tool_terms or []
    validation_dict["validation_term"] = term_str
    logger.info(
        "[validation] target_term=%s overall=%s errors=%d warnings=%d action=%s",
        target_term.display(), report.overall,
        len(report.errors), len(report.warnings), action.value,
    )
    return final_answer, final_cards, validation_dict


def _validation_term_from_agent_meta(
    agent_meta: dict,
    fallback_term: Optional[str],
) -> Optional[str]:
    """Prefer the term used by the successful grounding tool.

    The selected UI term can differ from a term explicitly supplied in a
    follow-up message. Validation must check the same term the tool queried.
    """

    terms = _validation_terms_from_agent_meta(agent_meta)
    return terms[-1] if terms else fallback_term


def _validation_terms_from_agent_meta(agent_meta: dict) -> list[str]:
    terms: list[str] = []
    for call in agent_meta.get("successful_tool_calls") or []:
        args = call.get("args") if isinstance(call, dict) else None
        parsed = parse_term_key(str((args or {}).get("term") or ""))
        if parsed.kind == "single":
            canonical = parsed.terms[0].canonical_name
            if canonical not in terms:
                terms.append(canonical)
    return terms


# ══════════════════════════════════════════════════════════
#  Streaming endpoint  /api/chat/stream
# ══════════════════════════════════════════════════════════
#
# cards/followups/validation are sent as one final `meta` event.
#
# Wire format:
#   data: {"type": "token", "text": "<chunk>"}
#   data: {"type": "token", "text": "<chunk>"}
#   ...
#   data: {"type": "meta",  "cards": [...], "followups": [...], ...}
#   data: {"type": "done"}


# ── Agent-loop handler ───────────────────────────────────

def _grounded_agent_fallback_reply(
    user_message: str,
    state: dict,
    *,
    term: Optional[str],
    reason: Optional[str] = None,
) -> str:
    course_reply = _deterministic_single_course_fallback_reply(
        user_message,
        state,
        term=term,
        reason=reason,
    )
    if course_reply:
        return course_reply

    details = f" ({reason})" if reason else ""
    return (
        f"I can’t reach the agent right now{details}, so I won’t invent "
        "course recommendations or section details. Please try again, or ask "
        "about a specific course "
        f"and I’ll verify it against the local catalog when the agent is available."
    )


def _format_course_units_for_fallback(record) -> str:
    if record.units is not None:
        return str(int(record.units)) if float(record.units).is_integer() else str(record.units)
    if record.min_units is None and record.max_units is None:
        return "unknown units"
    if record.min_units == record.max_units:
        value = record.min_units
        return str(int(value)) if value is not None and float(value).is_integer() else str(value)

    def fmt(value):
        if value is None:
            return "?"
        return str(int(value)) if float(value).is_integer() else str(value)

    return f"{fmt(record.min_units)}–{fmt(record.max_units)}"


def _deterministic_single_course_fallback_reply(
    user_message: str,
    state: dict,
    *,
    term: Optional[str],
    reason: Optional[str] = None,
) -> Optional[str]:
    """Local-only fallback for explicit single-course questions."""
    refs = []
    seen = set()
    for course_id in _course_ids_in_order(user_message):
        ref = parse_course_mention(course_id)
        if not ref:
            continue
        key = ref.course_id()
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    if len(refs) != 1:
        return None

    effective_term = term or state.get("term")
    target_term = Term.parse(effective_term or "")
    if not target_term:
        return None

    ref = refs[0]
    details = f" ({reason})" if reason else ""
    prefix = (
        f"I can’t reach the agent right now{details}. "
        "Here is the local catalog fallback for the single course you mentioned."
    )

    catalog = get_catalog(target_term)
    coverage = get_term_coverage(target_term)
    coverage_status = coverage.get("coverage_status") or "unknown"
    source_updated_at = coverage.get("updated_at") or "unknown"
    term_label = target_term.display()
    if coverage_status in {"partial", "stale", "unavailable", "unknown"}:
        observability.increment("catalog.coverage_status", status=coverage_status)
        observability.log_event(
            logger,
            logging.WARNING,
            "catalog_coverage",
            term=term_label,
            status=coverage_status,
            source="grounded_fallback",
            updated_at=source_updated_at,
        )
    if not catalog:
        return (
            f"{prefix}\n\n"
            f"I parsed the course as {ref.display()}, but local catalog data for "
            f"{term_label} is unavailable. I cannot verify title, units, sections, "
            f"or prerequisites without the agent/tools."
        )

    record = catalog.get_course(ref)
    if not record:
        if coverage_status in {"partial", "stale"}:
            status_line = (
                f"Local data for {term_label} is {coverage_status}, so I cannot "
                f"confirm whether {ref.display()} exists or is offered."
            )
        else:
            status_line = f"{ref.display()} was not found in the local catalog for {term_label}."
        return (
            f"{prefix}\n\n"
            f"{status_line} Source coverage: {coverage_status}, updated_at: {source_updated_at}."
        )

    sections = catalog.get_sections(ref)
    units = _format_course_units_for_fallback(record)
    title = record.title or "Untitled course"
    lines = [
        prefix,
        "",
        f"{record.ref.display()} — {title} ({units} units)",
    ]
    if record.description:
        lines.append(record.description)
    if record.prerequisite_text:
        lines.append(f"Prerequisites: {record.prerequisite_text}")
    if record.restriction:
        lines.append(f"Restriction: {record.restriction}")

    if sections:
        examples = []
        for section in sections[:3]:
            when = " ".join(
                part for part in (section.days, section.start_time, section.end_time)
                if part
            )
            examples.append(
                f"{section.section_type or 'Section'} {section.section_num or section.section_code}"
                + (f" ({when})" if when else "")
            )
        lines.append(
            f"Sections in {term_label}: {len(sections)} local section(s) found"
            + (f"; examples: {', '.join(examples)}." if examples else ".")
        )
    elif coverage_status == "complete":
        lines.append(f"Sections in {term_label}: no local sections found.")
    else:
        lines.append(
            f"Sections in {term_label}: local data is {coverage_status}; cannot confirm availability."
        )

    lines.append(f"Source coverage: {coverage_status}, updated_at: {source_updated_at}.")
    return "\n".join(lines)


async def _handle_agent(
    user_message: str,
    state: dict,
    memory_context: Optional[dict],
    *,
    user_id: str,
    term: Optional[str],
    system_prompt: Optional[str],
    queue: asyncio.Queue,
    recent_turns: Optional[list[dict]] = None,
    decisions: Optional[list[dict]] = None,
    summary: Optional[str] = None,
    execution_meta: Optional[dict] = None,
) -> tuple[str, list, list, Optional[dict]]:
    """
    Drive a tool-using LLM turn via app.agent.loop and forward its
    events to the SSE queue.

    Pre-flight fallback: if the agent's FIRST event is an error (LLM
    call failed before any output reached the client), stream a
    deterministic grounded fallback instead of starting another LLM
    recommendation path. Mid-flight errors surface as visible error
    events — by that point the user has already seen partial output.

    Returns (reply_text, cards, followups, validation). When supplied,
    ``execution_meta`` is populated with tool execution facts needed by the
    caller without changing this internal return contract.

    `cards` is populated when the LLM calls the `propose_recommendation`
    tool — the loop emits a `cards_proposed` event that we accumulate
    here (last call wins, so a multi-step turn that re-stages overrides
    earlier picks). followups/validation are still always [], None for
    the agent path; those land in their own follow-up phases.
    """
    from app.llm import adapter

    accumulated = ""
    saw_any_event = False
    proposed_cards: list[dict] = []
    successful_tools: set[str] = set()
    successful_tool_calls: list[dict] = []

    try:
        async for event in adapter.stream_agent_response(
            user_message,
            session_state=state,
            user_id=user_id,
            term=term,
            memory_context=memory_context,
            system_prompt_override=system_prompt,
            recent_turns=recent_turns,
            decisions=decisions,
            summary=summary,
        ):
            t = event.get("type")

            # Pre-flight fallback gate: if the very first event is an
            # error, the agent never streamed anything to the client.
            if not saw_any_event:
                saw_any_event = True
                if t == "error":
                    logger.warning("[agent] pre-flight error, grounded fallback: %s",
                                   event.get("message"))
                    fallback = _grounded_agent_fallback_reply(
                        user_message,
                        state,
                        term=term,
                        reason=event.get("message"),
                    )
                    await queue.put({"type": "token", "text": fallback})
                    return (fallback, [], [], None)

            if t == "token":
                accumulated += event.get("text", "")
                await queue.put({"type": "token", "text": event["text"]})
            elif t == "tool_call_start":
                observability.log_event(
                    logger,
                    logging.INFO,
                    "agent_tool_start",
                    tool=event.get("name"),
                    label=event.get("label"),
                    term=term,
                )
                await queue.put({
                    "type":  "tool_call_start",
                    "name":  event.get("name"),
                    "label": event.get("label"),
                    "args":  event.get("args"),
                })
            elif t == "tool_call_done":
                ok = event.get("ok", True)
                if ok and event.get("name"):
                    successful_tools.add(event["name"])
                    call_record = {
                        "name": event["name"],
                        "args": dict(event.get("args") or {}),
                    }
                    if "term_data_available" in event:
                        call_record["term_data_available"] = bool(
                            event.get("term_data_available")
                        )
                    successful_tool_calls.append(call_record)
                if not ok:
                    observability.increment("agent.tool_failures", tool=event.get("name"))
                observability.log_event(
                    logger,
                    logging.INFO if ok else logging.WARNING,
                    "agent_tool_done",
                    tool=event.get("name"),
                    ok=ok,
                    term=term,
                )
                await queue.put({
                    "type":  "tool_call_done",
                    "name":  event.get("name"),
                    "label": event.get("label"),
                    "ok":    ok,
                })
            elif t == "cards_proposed":
                # propose_recommendation tool fired. Default behavior
                # is REPLACE — if the LLM re-stages mid-turn during the
                # normal loop, the latest batch is authoritative
                # (refinement after a bad pick).
                # EXCEPTION: from_fallback=True means the loop hit a
                # budget limit and the fallback path made a last-ditch
                # call. That call only has partial data (the courses
                # the LLM managed to enrich before the cap) and tends
                # to be conservative — it would silently shrink the
                # earlier proposal. Merge those in instead, deduping
                # by course_id so the prior batch is preserved.
                new_cards = event.get("cards") or []
                if event.get("from_fallback") and proposed_cards:
                    existing_ids = {c.get("course_id") for c in proposed_cards}
                    added = 0
                    for c in new_cards:
                        if c.get("course_id") not in existing_ids:
                            proposed_cards.append(c)
                            existing_ids.add(c.get("course_id"))
                            added += 1
                    logger.info("[agent handler] cards_proposed (fallback merge): "
                                "+%d items (total %d)", added, len(proposed_cards))
                else:
                    proposed_cards = new_cards
                    logger.info("[agent handler] cards_proposed: %d items",
                                len(proposed_cards))
            elif t == "limit_reached":
                # Budget hit. The loop will keep streaming token/final
                # events from its no-tools fallback after this; the
                # continuation_id lets the frontend offer a Continue
                # button that POSTs to /api/chat/continue.
                logger.info("[agent handler] forwarding limit_reached: reason=%s cid=%s",
                            event.get("reason"), (event.get("continuation_id") or "")[:8])
                observability.increment(
                    "agent.limit_reached",
                    reason=event.get("reason"),
                )
                observability.log_event(
                    logger,
                    logging.WARNING,
                    "agent_limit_reached",
                    reason=event.get("reason"),
                    iterations=event.get("iterations"),
                    tool_calls=event.get("tool_calls"),
                    term=term,
                )
                await queue.put({
                    "type":  "limit_reached",
                    "reason": event.get("reason"),
                    "iterations": event.get("iterations"),
                    "tool_calls": event.get("tool_calls"),
                    "continuation_id": event.get("continuation_id"),
                })
            elif t == "final":
                # Tokens were already streamed; nothing extra to forward.
                pass
            elif t == "error":
                # Mid-flight error — surface and stop. No fallback (we
                # already showed partial output to the user).
                if execution_meta is not None:
                    execution_meta["error"] = True
                await queue.put({
                    "type": "error",
                    "message": event.get("message", "agent error"),
                })
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("[agent handler] failed: %s", e)
        if execution_meta is not None:
            execution_meta["error"] = True
        if not accumulated:
            fallback = _grounded_agent_fallback_reply(
                user_message,
                state,
                term=term,
                reason=str(e),
            )
            await queue.put({"type": "token", "text": fallback})
            return (fallback, [], [], None)
        await queue.put({"type": "error", "message": str(e)})
        return (accumulated, proposed_cards, [], None)

    if not saw_any_event:
        # Generator yielded zero events — typically LLM_ENABLED=False.
        fallback = _grounded_agent_fallback_reply(
            user_message,
            state,
            term=term,
            reason="agent produced no output",
        )
        await queue.put({"type": "token", "text": fallback})
        return (fallback, [], [], None)

    if execution_meta is not None:
        execution_meta["successful_tools"] = sorted(successful_tools)
        execution_meta["successful_tool_calls"] = successful_tool_calls
    return (accumulated, proposed_cards, [], None)


from fastapi.responses import JSONResponse, StreamingResponse


@router.post("/chat/stream")
async def chat_stream_endpoint(
    req: ChatRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    user: dict = Depends(current_user_optional),
):
    check_rate_limit(request, CHAT_STREAM_LIMIT, user["id"])
    trace_id = observability.get_trace_id()
    return StreamingResponse(
        _stream_chat(req, background_tasks, user["id"], trace_id=trace_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # tell nginx-like proxies not to buffer
        },
    )


async def _stream_chat(
    req: ChatRequest,
    background_tasks: BackgroundTasks,
    user_id: str,
    *,
    trace_id: Optional[str] = None,
):
    """Async generator yielding SSE-formatted events. `user_id` is resolved
    from the session cookie in the entry-point endpoint and passed in
    rather than re-derived here, since dependencies don't compose into
    inner async-generator scopes."""
    import asyncio
    import json as _json

    trace_token = observability.set_trace_id(trace_id or observability.get_trace_id())
    stream_started_at = observability.now()
    first_token_ms: Optional[float] = None
    output_parts: list[str] = []
    tool_failures = 0
    saw_limit_reached = False
    status = "ok"
    final_session_id = req.session_id

    def track_sse_event(event: dict) -> None:
        nonlocal first_token_ms, tool_failures, saw_limit_reached, status, final_session_id
        event_type = event.get("type")
        if event_type == "token":
            text = event.get("text") or ""
            output_parts.append(text)
            if first_token_ms is None:
                first_token_ms = observability.elapsed_ms(stream_started_at)
                observability.observe_ms("sse.first_token_ms", first_token_ms)
        elif event_type == "tool_call_done" and not event.get("ok", True):
            tool_failures += 1
            observability.increment("sse.tool_failures", tool=event.get("name"))
        elif event_type == "limit_reached":
            saw_limit_reached = True
            observability.increment("sse.limit_reached", reason=event.get("reason"))
        elif event_type == "error":
            status = "error"
            observability.increment("sse.errors")
        elif event_type == "meta":
            final_session_id = event.get("session_id") or final_session_id

    def sse(event: dict) -> str:
        track_sse_event(event)
        return f"data: {_json.dumps(event, ensure_ascii=False)}\n\n"

    queue: asyncio.Queue = asyncio.Queue()
    DONE = object()

    async def producer():
        """Run the full chat pipeline, pushing events into the queue."""
        try:
            mem = get_memory_manager()
            # user_id comes from the signed session cookie (or the
            # demo_001 fallback for anonymous demo traffic).

            # ── Phase 3.3: resolve the request's session_id to a persistent one ──
            # ChatRequest.term is retained for wire compatibility only. Ordinary
            # chat authority comes from conversation metadata + the resolver.
            persistent_sid = _resolve_session_id(req.session_id, user_id, None)
            active_session_id = persistent_sid

            session = get_or_create_session(active_session_id, user_id=user_id)
            if not session.get("major"):
                load_student_into_session(active_session_id, user_id, user_id=user_id)
                session = get_or_create_session(active_session_id, user_id=user_id)

            mem.on_turn_start(active_session_id, user_id)
            logger.info("[stream turn %d] user=%s session=%s msg=%r",
                        mem.turn_count(active_session_id), user_id,
                        persistent_sid, req.message[:120])

            # M5: do not run pre-agent LLM classifiers/extractors. The
            # agent sees profile/session/history context directly; hard
            # facts captured here are deterministic regex-only updates.
            intent = "agent"
            extracted = _extract_deterministic_hard_facts(req.message)
            if extracted:
                _capture_hard_facts(session, extracted, user_id, mem)
                session = update_session(active_session_id, session, user_id=user_id)
                session = update_session(active_session_id, extracted, user_id=user_id)

            memory_context = {
                "system_prompt_block": mem.system_prompt_block(user_id),
                "prefetched_context": mem.prefetch(req.message, user_id),
            }

            # ── Phase 3.4: load structured context layers from sessions.py ──
            try:
                session_meta = sessions_data.get_session_meta(user_id, persistent_sid)
                recent_turns = sessions_data.read_turns(user_id, persistent_sid)
                decisions    = session_meta.get("decisions") or []
                summary      = session_meta.get("summary")
            except sessions_data.SessionNotFound:
                session_meta = {}
                recent_turns = []
                decisions    = []
                summary      = None

            term_service = get_term_resolution_service()
            query_resolution = term_service.resolve_message(req.message)
            conversation_term = term_service.effective_for_conversation(session_meta)
            query_terms = [term.canonical_name for term in query_resolution.terms]
            query_term = conversation_term.canonical_name
            term_resolution_reply: Optional[str] = None
            if query_resolution.error is not None:
                term_resolution_reply = (
                    "I couldn't map that term unambiguously: "
                    f"{query_resolution.error.message}. Please specify one canonical "
                    "term such as 2026 Fall."
                )
            elif query_resolution.kind == "single":
                requested_term = query_resolution.terms[0]
                if requested_term.status == "unavailable":
                    term_resolution_reply = (
                        f"Data for {requested_term.canonical_name} has not been published "
                        "with at least one course and section yet, so this conversation "
                        "will stay on its current term."
                    )
                else:
                    query_term = requested_term.canonical_name

            if req.term:
                observability.log_event(
                    logger,
                    logging.INFO,
                    "frontend_term_ignored",
                    request_term=req.term,
                    resolved_term=query_term,
                    session_id=persistent_sid,
                )

            # Off-topic and clarification decisions are handled inside
            # the agent with recent_turns/session context. The old
            # pre-agent short-circuits were removed because they only
            # saw the latest message and produced hardcoded English
            # templates for valid continuations like "继续" / "yes".

            state = get_known_fields(active_session_id, user_id=user_id)
            state["term"] = query_term
            state["effective_term"] = conversation_term.canonical_name
            state["query_terms"] = query_terms
            state["term_mode"] = session_meta.get("term_mode", "auto")
            state["term_source"] = conversation_term.source
            state["pending_schedule"] = session.get("pending_schedule", [])

            # ── Stream the LLM answer through on_token ──
            async def on_token(text: str):
                await queue.put({"type": "token", "text": text})

            validation_dict = None
            # Agent loop is the only chat chain. Pre-flight failures
            # return a deterministic grounded fallback from _handle_agent;
            # no legacy LLM recommendation path is started.
            agent_meta: dict = {}
            if term_resolution_reply is not None:
                reply, cards, followups = term_resolution_reply, [], []
                agent_meta["term_resolution_blocked"] = True
                await queue.put({"type": "token", "text": reply})
            else:
                agent_result = await _handle_agent(
                    req.message, state, memory_context,
                    user_id=user_id,
                    term=query_term,
                    system_prompt=(
                        req.system_prompt
                        if config.allow_custom_system_prompt()
                        else None
                    ),
                    queue=queue,
                    recent_turns=recent_turns,
                    decisions=decisions,
                    summary=summary,
                    execution_meta=agent_meta,
                )
                reply, cards, followups, validation_dict = agent_result
            retrieval_performed = bool(agent_meta.get("successful_tools"))
            tool_terms = _validation_terms_from_agent_meta(agent_meta)
            validation_term = _validation_term_from_agent_meta(
                agent_meta,
                query_term,
            )
            if validation_term and validation_term != query_term:
                observability.log_event(
                    logger,
                    logging.INFO,
                    "validation_term_overridden",
                    selected_term=query_term,
                    validation_term=validation_term,
                    reason="successful_tool_call",
                )

            if validation_dict is None and reply:
                reply, cards, validation_dict = _validate_response(
                    answer=reply,
                    cards=cards,
                    retrieved=None,
                    state=state,
                    user_message=req.message,
                    term_str=validation_term,
                    session_id=active_session_id,
                    retrieval_performed=retrieval_performed,
                    query_terms=query_terms,
                    tool_terms=tool_terms,
                )

            validation_blocked = bool(
                validation_dict
                and validation_dict.get("applied_action") == "block"
            )
            committed_resolution = query_resolution
            if query_resolution.kind == "single" and not query_resolution.all_available:
                requested_name = query_resolution.terms[0].canonical_name
                grounded_available = False
                for call in agent_meta.get("successful_tool_calls") or []:
                    parsed_call_term = parse_term_key(
                        str((call.get("args") or {}).get("term") or "")
                    )
                    if (
                        call.get("term_data_available")
                        and parsed_call_term.kind == "single"
                        and parsed_call_term.terms[0].canonical_name == requested_name
                    ):
                        grounded_available = True
                        break
                if grounded_available:
                    grounded_term = replace(
                        query_resolution.terms[0],
                        data_available=True,
                        status="available",
                    )
                    committed_resolution = QueryTermResolution(
                        query_resolution.automatic,
                        query_resolution.parsed,
                        (grounded_term,),
                    )
            original_term_mode = session_meta.get("term_mode", "auto")
            original_term_scope = session_meta.get("term_scope")
            final_session_meta = commit_conversation_resolution(
                user_id,
                persistent_sid,
                committed_resolution,
                answer_succeeded=bool(reply) and not agent_meta.get("error", False),
                validation_blocked=validation_blocked,
            )
            final_effective_term = term_service.effective_for_conversation(final_session_meta)
            persisted_state = update_session(
                active_session_id,
                {"term": final_effective_term.canonical_name},
                user_id=user_id,
            )
            state.update(persisted_state)
            state["term"] = final_effective_term.canonical_name
            state["effective_term"] = final_effective_term.canonical_name
            state["query_terms"] = query_terms
            state["term_mode"] = final_session_meta.get("term_mode", "auto")
            state["term_source"] = final_effective_term.source
            state["pending_schedule"] = session.get("pending_schedule", [])
            term_update = {
                "changed": (
                    original_term_mode != final_session_meta.get("term_mode")
                    or original_term_scope != final_session_meta.get("term_scope")
                ),
                "mode": final_session_meta.get("term_mode", "auto"),
                "scope": final_session_meta.get("term_scope"),
            }

            observability.log_event(
                logger,
                logging.INFO,
                "term_resolution",
                session_id=persistent_sid,
                resolved_term=final_effective_term.canonical_name,
                mode=final_session_meta.get("term_mode", "auto"),
                source=final_effective_term.source,
                explicit_terms=query_terms,
                tool_terms=tool_terms,
                validation_term=validation_term,
                persisted=term_update["changed"],
            )

            mem.sync_turn(user_id, req.message, reply, active_session_id)

            # ── Phase 3.3 + 3.5 + Round 4: persist turn, schedule auto-title,
            #    then detect decisions ──
            new_turn_index, did_auto_title = _persist_turn(
                user_id, persistent_sid, req.message, reply,
                cards=cards, followups=followups, validation=validation_dict,
            )
            _maybe_schedule_auto_title(
                background_tasks, did_auto_title,
                user_id, persistent_sid, req.message, reply,
            )
            _detect_and_pin_decisions(user_id, persistent_sid, req.message, new_turn_index)

            _maybe_schedule_reflection(background_tasks, mem, active_session_id, user_id)

            await queue.put({
                "type": "meta",
                "session_id": persistent_sid,
                "intent": intent,
                "cards": cards,
                "followups": followups,
                "validation_report": validation_dict,
                "final_answer": reply,
                "session_state": state,
                "pending_schedule": session.get("pending_schedule", []),
                "effective_term": final_effective_term.canonical_name,
                "query_terms": query_terms,
                "term_mode": final_session_meta.get("term_mode", "auto"),
                "term_source": final_effective_term.source,
                "term_status": final_effective_term.status,
                "term_update": term_update,
            })
        except asyncio.CancelledError:
            logger.info("[stream] producer cancelled (client disconnected)")
            raise
        except Exception as e:
            logger.exception("[stream] producer failed: %s", e)
            await queue.put({"type": "error", "message": str(e)})
        finally:
            await queue.put(DONE)

    task = asyncio.create_task(producer())
    try:
        try:
            while True:
                event = await queue.get()
                if event is DONE:
                    yield sse({"type": "done"})
                    break
                yield sse(event)
        except asyncio.CancelledError:
            status = "cancelled"
            # Client disconnected — cancel the producer so the LLM stream
            # closes its upstream connection and DeepSeek stops generating.
            logger.info("[stream] consumer cancelled, cascading to producer")
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            raise
        finally:
            if not task.done():
                task.cancel()
            total_ms = observability.elapsed_ms(stream_started_at)
            observability.observe_ms("sse.total_ms", total_ms)
            observability.increment("sse.streams", status=status)
            usage = observability.record_llm_usage_estimate(
                model="agent",
                input_text=req.message,
                output_text="".join(output_parts),
            )
            observability.log_event(
                logger,
                logging.INFO if status == "ok" else logging.WARNING,
                "sse_stream_end",
                status=status,
                session_id=final_session_id,
                user=user_id,
                term=req.term,
                first_token_ms=first_token_ms,
                total_ms=total_ms,
                tool_failures=tool_failures,
                limit_reached=saw_limit_reached,
                llm_input_tokens=usage["input_tokens"],
                llm_output_tokens=usage["output_tokens"],
                llm_cost_usd=usage["cost_usd"],
                llm_usage_source=usage["source"],
            )
    finally:
        observability.reset_trace_id(trace_token)


# ── Continue endpoint (resume after limit_reached) ───────

class ContinueRequest(BaseModel):
    session_id: str = ""
    continuation_id: str


@router.post("/chat/continue")
async def chat_continue_endpoint(
    req: ContinueRequest,
    request: Request,
    user: dict = Depends(current_user_optional),
):
    """
    Resume an agent loop that hit its budget. The frontend calls this
    when the user clicks the "Continue" button rendered after a
    limit_reached event. Streams the same SSE event protocol as
    /chat/stream.

    The continuation_id is single-use — pop_continuation removes it
    from the in-memory store, so a refresh / double-click can't
    accidentally double-bill the budget.
    """
    check_rate_limit(request, CHAT_CONTINUE_LIMIT, user["id"])
    trace_id = observability.get_trace_id()
    return StreamingResponse(
        _stream_continue(req, user["id"], trace_id=trace_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_continue(
    req: ContinueRequest,
    user_id: str,
    *,
    trace_id: Optional[str] = None,
):
    import json as _json
    from app.agent.loop import resume_agent
    from app.llm.adapter import _get_client, LLM_MODEL, LLM_ENABLED

    trace_token = observability.set_trace_id(trace_id or observability.get_trace_id())
    stream_started_at = observability.now()
    first_token_ms: Optional[float] = None

    def sse(event: dict) -> str:
        nonlocal first_token_ms
        if event.get("type") == "token" and first_token_ms is None:
            first_token_ms = observability.elapsed_ms(stream_started_at)
            observability.observe_ms("sse.continue_first_token_ms", first_token_ms)
        return f"data: {_json.dumps(event, ensure_ascii=False)}\n\n"

    if not LLM_ENABLED:
        try:
            yield sse({"type": "error", "message": "LLM not enabled"})
            yield sse({"type": "done"})
        finally:
            observability.increment("sse.continue_streams", status="error")
            observability.log_event(
                logger,
                logging.WARNING,
                "sse_continue_end",
                status="error",
                session_id=req.session_id,
                user=user_id,
                first_token_ms=first_token_ms,
                total_ms=observability.elapsed_ms(stream_started_at),
                reason="llm_not_enabled",
            )
            observability.reset_trace_id(trace_token)
        return

    # user_id is resolved from the signed session cookie by the caller.
    accumulated = ""
    saw_limit_again = False
    status = "ok"
    try:
        client = _get_client()
        async for event in resume_agent(
            req.continuation_id,
            client=client, model=LLM_MODEL,
        ):
            t = event.get("type")
            if t == "token":
                accumulated += event.get("text", "")
            elif t == "limit_reached":
                saw_limit_again = True
                observability.increment("sse.continue_limit_reached")
            yield sse(event)

        # Persist the resumed reply to the session log so refresh /
        # session reload doesn't lose it. We don't run the full memory
        # pipeline here (no new user message); just append the text.
        if accumulated:
            try:
                sessions_data.append_turn(
                    user_id,
                    req.session_id,
                    "assistant",
                    accumulated,
                )
            except Exception as e:
                logger.warning("[continue] append resumed turn failed: %s", e)

        logger.info("[continue] cid=%s user=%s session=%s chars=%d limit_again=%s",
                    req.continuation_id, user_id, req.session_id,
                    len(accumulated), saw_limit_again)
    except asyncio.CancelledError:
        logger.info("[continue] cancelled by client")
        status = "cancelled"
        raise
    except Exception as e:
        logger.exception("[continue] failed: %s", e)
        status = "error"
        observability.increment("sse.continue_errors")
        yield sse({"type": "error", "message": str(e)})
    finally:
        try:
            yield sse({"type": "done"})
        finally:
            total_ms = observability.elapsed_ms(stream_started_at)
            observability.observe_ms("sse.continue_total_ms", total_ms)
            observability.increment("sse.continue_streams", status=status)
            usage = observability.record_llm_usage_estimate(
                model="agent_continue",
                input_text=req.continuation_id,
                output_text=accumulated,
            )
            observability.log_event(
                logger,
                logging.INFO if status == "ok" else logging.WARNING,
                "sse_continue_end",
                status=status,
                session_id=req.session_id,
                user=user_id,
                first_token_ms=first_token_ms,
                total_ms=total_ms,
                limit_reached=saw_limit_again,
                llm_input_tokens=usage["input_tokens"],
                llm_output_tokens=usage["output_tokens"],
                llm_cost_usd=usage["cost_usd"],
                llm_usage_source=usage["source"],
            )
            observability.reset_trace_id(trace_token)


# ── Schedule endpoints ───────────────────────────────────

class ScheduleRequest(BaseModel):
    session_id: str = ""
    course_id: str
    # section is the section_num ("A", "A1", "B3") — NOT the 5-digit
    # registrar code. For E4-style picks the frontend sends "A" for Lec,
    # "A1" for Dis. Defaults to None (used by /remove to wipe all
    # entries of a course; for /add the frontend always sends one).
    section: Optional[str] = "A"
    # Frontend passes the term selector value; backend uses it to fetch
    # the right sections from db.get_sections (term-strict). None falls
    # back to session-state term, then to the catalog registry default.
    term: Optional[str] = None
    # Hard conflicts / unknown schedule data require an explicit second
    # request so adding a section never silently creates a broken schedule.
    confirm_conflicts: bool = False


def _resolve_section_num(course_id: str, sec: Optional[str],
                         term: Optional[str]) -> Optional[str]:
    """
    Normalise an entry's `section` field to a canonical section_num
    ("A" / "A1" / "B"). The frontend pre-E4 sometimes stored the
    5-digit registrar code ("34190") here; the current picker stores
    section_num directly. To compare entries reliably we look the
    string up against the course's actual sections and return its
    section_num, treating section_num and section_code as
    interchangeable inputs. None on miss — callers fall back to
    raw-string equality.
    """
    if not sec:
        return None
    sec_str = str(sec).strip()
    if not term:
        return sec_str        # best effort; can't resolve without term
    from app.data.db import get_sections
    env = get_sections(course_id, term)
    if not env.get("found"):
        return sec_str
    for s in env.get("sections", []):
        if s.get("section_num") == sec_str or s.get("section_code") == sec_str:
            return s.get("section_num") or s.get("section_code")
    return sec_str


def _entries_match(entry: dict, course_id: str, req_section: Optional[str],
                   term: Optional[str]) -> bool:
    """True if `entry` refers to the same (course, section) as the
    request, surviving the section_num-vs-section_code mismatch
    described in _resolve_section_num."""
    if entry.get("course_id") != course_id:
        return False
    entry_norm = _resolve_section_num(course_id, entry.get("section"), term)
    req_norm   = _resolve_section_num(course_id, req_section, term)
    return entry_norm == req_norm and entry_norm is not None


def _empty_schedule_validation() -> dict:
    return {
        "valid": True,
        "warnings": [],
        "conflicts": [],
        "unknowns": [],
    }


def _validate_pending_schedule(pending_schedule: list[dict], term: Optional[str]) -> dict:
    if not pending_schedule:
        return _empty_schedule_validation()
    if not term:
        return validate_schedule_bundle(
            [
                {
                    "course_id": entry.get("course_id") or "",
                    "selected_sections": [],
                }
                for entry in pending_schedule
                if isinstance(entry, dict)
            ]
        )

    from app.data.db import get_sections

    bundle_items = build_pending_schedule_bundle_items(
        pending_schedule,
        term=term,
        section_lookup=get_sections,
    )
    return validate_schedule_bundle(bundle_items)


@router.post("/schedule/add")
async def add_to_schedule(
    req: ScheduleRequest,
    request: Request,
    user: dict = Depends(current_user_optional),
):
    """Phase E4: each (course_id, section) pair is a separate schedule
    entry. Adding Lec A and Dis A1 of the same course produces TWO
    entries (and downstream, two calendar events) so the user sees
    both blocks on the day grid.

    Stores the normalized section_num so subsequent compares hit the
    fast path instead of going through _resolve_section_num every
    time."""
    check_rate_limit(request, SCHEDULE_WRITE_LIMIT, user["id"])
    user_id = user["id"]
    active_session_id = _resolve_session_id(req.session_id, user_id, req.term)
    session = get_or_create_session(active_session_id, user_id=user_id)
    sec_norm = _resolve_section_num(req.course_id, req.section, req.term)
    sec_canon = sec_norm or req.section
    entry = {"course_id": req.course_id, "section": sec_canon, "status": "pending"}
    # Dedup using section-equivalence (handles legacy entries that stored
    # the 5-digit registrar code where the new picker stores section_num).
    is_dup = any(
        _entries_match(e, req.course_id, sec_canon, req.term)
        for e in session.get("pending_schedule", [])
    )
    schedule_validation = _validate_pending_schedule(
        session.get("pending_schedule", []),
        req.term or session.get("term"),
    )
    if not is_dup:
        from app.data.db import get_sections

        effective_term = req.term or session.get("term")
        candidate_sections = resolve_pending_schedule_sections(
            [entry],
            term=effective_term,
            section_lookup=get_sections,
        )
        pending_sections = resolve_pending_schedule_sections(
            session.get("pending_schedule", []),
            term=effective_term,
            section_lookup=get_sections,
        )
        candidate_item = (
            {"course_id": req.course_id, "selected_sections": candidate_sections}
            if candidate_sections
            else {"course_id": req.course_id}
        )
        schedule_validation = validate_schedule_bundle(
            [candidate_item],
            pending_sections=pending_sections,
        )
        if not schedule_validation["valid"] and not req.confirm_conflicts:
            events = _build_schedule_events(session, req.term)
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "reason": "schedule_validation_failed",
                    "requires_confirmation": True,
                    "pending_schedule": session.get("pending_schedule", []),
                    "events": events,
                    "schedule_validation": schedule_validation,
                },
            )

        session.setdefault("pending_schedule", []).append(entry)
        session = update_session(
            active_session_id,
            {"pending_schedule": session["pending_schedule"]},
            user_id=user_id,
        )
        schedule_validation = _validate_pending_schedule(
            session.get("pending_schedule", []),
            effective_term,
        )
    events = _build_schedule_events(session, req.term)
    return {
        "ok": True,
        "pending_schedule": session["pending_schedule"],
        "events": events,
        "schedule_validation": schedule_validation,
    }


@router.post("/schedule/remove")
async def remove_from_schedule(
    req: ScheduleRequest,
    request: Request,
    user: dict = Depends(current_user_optional),
):
    """If `section` is provided, remove only that specific (course, section).
    Section-equivalence aware so legacy entries (5-digit codes stored
    where section_num is now expected) get matched and removed.
    If omitted, remove every entry for the course."""
    check_rate_limit(request, SCHEDULE_WRITE_LIMIT, user["id"])
    user_id = user["id"]
    active_session_id = _resolve_session_id(req.session_id, user_id, req.term)
    session = get_or_create_session(active_session_id, user_id=user_id)
    if req.section:
        session["pending_schedule"] = [
            e for e in session.get("pending_schedule", [])
            if not _entries_match(e, req.course_id, req.section, req.term)
        ]
    else:
        session["pending_schedule"] = [
            e for e in session.get("pending_schedule", []) if e["course_id"] != req.course_id
        ]
    session = update_session(
        active_session_id,
        {"pending_schedule": session["pending_schedule"]},
        user_id=user_id,
    )
    events = _build_schedule_events(session, req.term)
    return {
        "ok": True,
        "pending_schedule": session["pending_schedule"],
        "events": events,
        "schedule_validation": _validate_pending_schedule(
            session["pending_schedule"],
            req.term or session.get("term"),
        ),
    }


class ScheduleClearRequest(BaseModel):
    session_id: str = ""
    term: Optional[str] = None


@router.post("/schedule/clear")
async def clear_schedule(
    req: ScheduleClearRequest,
    request: Request,
    user: dict = Depends(current_user_optional),
):
    """Wipe every entry in this session's pending_schedule. Escape hatch
    when the user accumulates stuck entries (e.g. from legacy format
    that the section-equivalence fix can't auto-resolve)."""
    check_rate_limit(request, SCHEDULE_WRITE_LIMIT, user["id"])
    user_id = user["id"]
    active_session_id = _resolve_session_id(req.session_id, user_id, req.term)
    update_session(active_session_id, {"pending_schedule": []}, user_id=user_id)
    return {
        "ok": True,
        "pending_schedule": [],
        "events": [],
        "schedule_validation": _empty_schedule_validation(),
    }


class EndSessionRequest(BaseModel):
    session_id: str = ""


@router.post("/session/end")
async def end_session(
    req: EndSessionRequest,
    user: dict = Depends(current_user_optional),
):
    user_id = user["id"]
    get_memory_manager().on_session_end(user_id, req.session_id)
    return {"ok": True, "messages_archived": 0}


def _build_schedule_events(session, term: Optional[str] = None):
    """
    Materialize the session's pending_schedule into calendar events.

    Resolves `term` in this order:
      1. explicit arg (frontend's term selector)
      2. session state ("term" key set by the chat pipeline)
      3. catalog registry's default term (most recently loaded data)

    Uses db.get_sections's new envelope shape ({found, sections: [...]})
    plus the extended SectionRecord fields (section_code / days /
    start_time / end_time / instructors[]).
    """
    from app.data.db import get_sections, get_course_info
    from app.catalog import get_term_registry

    resolved_term = term or session.get("term")
    if not resolved_term:
        default_term = get_term_registry().default()
        if default_term:
            resolved_term = default_term.display()
    if not resolved_term:
        return []  # nothing we can ground sections in

    events = []
    for entry in session.get("pending_schedule", []):
        cid, sid = entry["course_id"], entry.get("section") or "A"

        course_env = get_course_info(cid)
        title = (
            course_env.get("course", {}).get("title", cid)
            if course_env.get("found") else cid
        )

        sec_env = get_sections(cid, resolved_term)
        sections = sec_env.get("sections", []) if sec_env.get("found") else []
        if not sections:
            continue

        # Match by section_num (what the frontend picker sends: "A" /
        # "A1") — NOT section_code (the 5-digit registrar number).
        # Section_code matching was the legacy path and never hit;
        # the frontend has always passed section_num here.
        sec = next((s for s in sections if (s.get("section_num") or "") == sid), None)
        if sec is None:
            # Fall back to section_code match for old callers that
            # somehow stored a code, then to first Lec/Sem.
            sec = next((s for s in sections if s.get("section_code") == sid), None)
        if sec is None:
            lec_order = {"Lec": 0, "Sem": 1, "Stu": 2}
            sec = sorted(
                sections,
                key=lambda s: lec_order.get(s.get("section_type") or "", 99),
            )[0]

        start, end = sec.get("start_time"), sec.get("end_time")
        days_str = sec.get("days")
        if not (start and end and days_str):
            continue  # async / TBA sections have no calendar slot

        instructors = sec.get("instructors") or []
        primary_instructor = instructors[0] if instructors else ""

        for day in calendar_day_names(days_str):
            events.append({
                "course_id":   cid,
                "title":       title,
                "section_num": sec.get("section_num") or sid,    # "A" / "A1"
                "section_code": sec.get("section_code", ""),    # "34190" — 5-digit registrar code
                "section_type": sec.get("section_type"),         # "Lec" / "Dis" / "Lab"
                "instructor":  primary_instructor,
                "day":         day,
                "start":       start,
                "end":         end,
                "location":    sec.get("location") or "",
                # Keep legacy `section` key (= section_code) for callers
                # that haven't migrated yet (the frontend grid currently
                # reads only course_id / day / start / end / location).
                "section":     sec.get("section_code", ""),
            })
    return events
