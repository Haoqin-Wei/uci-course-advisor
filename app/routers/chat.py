"""
Chat Router — streams every product chat turn through the Agent loop.

The legacy non-streaming chat endpoint, pre-agent intent classifier,
template answer path, and old recommendation query path have been
removed. This router now handles:

  • deterministic hard-fact capture for explicit user statements
  • Agent-loop SSE streaming and continuation
  • direct Agent-loop answer streaming
  • session-backed schedule mutations
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from functools import lru_cache
from threading import RLock

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from app import config, observability
from app.response_language import response_language as _response_language
from app.response_language import display_term
from app.auth.deps import current_user_optional
from app.auth.rate_limit import RateLimit, check_rate_limit
from app.modules.state import (
    get_or_create_session, update_session,
    load_student_into_session, get_known_fields,
)
from app.memory import get_memory_manager
from app.academic import get_ai_academic_context
from app.scheduling import (
    build_pending_schedule_bundle_items,
    calendar_day_names,
    normalize_section_meeting,
    section_time_status,
    validate_schedule_bundle,
)

# ── Phase 3.3 / 3.5 — session storage + decision detection ──
from app.data import sessions as sessions_data
from app.modules import decision_detector
# ─────────────────────────────────────────────────────────────

# ── Catalog + term resolution ───────────────────────────
from app.catalog.term import Term
from app.catalog.cache import get_catalog
from app.catalog.coverage import get_term_coverage
from app.catalog.departments import colloquial_course_id
from app.catalog.normalization import iter_course_mentions, parse_course_mention
from app.terms import next_recent_focus_terms, parse_term_key, resolve_query_scope
from app.terms.conversation import sync_automatic_default
from app.terms.intent import refine_query_scope
from app.terms.service import get_term_resolution_service
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
    # Compatibility-only request field. The backend never trusts it for
    # conversation default or per-turn query scope.
    term: Optional[str] = None
    system_prompt: Optional[str] = None


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
            default_term=term_str,
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
    web_fetches: Optional[list] = None,
    query_terms: Optional[list[str]] = None,
    query_term_source: Optional[str] = None,
    course_ids: Optional[list[str]] = None,
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
        sessions_data.append_turn(
            user_id,
            persistent_sid,
            "user",
            user_msg,
            query_terms=query_terms,
            query_term_source=query_term_source,
            course_ids=course_ids,
        )
        idx = sessions_data.append_turn(
            user_id, persistent_sid, "assistant", assistant_reply,
            cards=cards,
            followups=followups,
            web_fetches=web_fetches,
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


def _capture_hard_facts(
    session: dict,
    extracted: dict,
    user_id: str,
    mem,
    *,
    session_id: str | None = None,
    source_turn_index: int | None = None,
    source_message: str | None = None,
) -> None:
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
                mem.add_fact(
                    user_id,
                    f"Currently taking {c}",
                    topic=f"course_status:{str(c).replace(' ', '').upper()}",
                    source_type="user_explicit",
                    source_session_id=session_id,
                    source_turn_index=source_turn_index,
                    source_quote=source_message,
                    confidence=1.0,
                )
            logger.info("[Channel A] currently_taking captured → %s", new_courses)

    if completed:
        new_courses = _merge_into_list(session, "completed_courses", completed)
        if new_courses:
            for c in new_courses:
                mem.add_fact(
                    user_id,
                    f"Completed {c}",
                    topic=f"course_status:{str(c).replace(' ', '').upper()}",
                    source_type="user_explicit",
                    source_session_id=session_id,
                    source_turn_index=source_turn_index,
                    source_quote=source_message,
                    confidence=1.0,
                )
            logger.info("[Channel A] completed captured → %s", new_courses)

    # ── Identity → MemoryManager profile ──
    profile_updates = {}
    for f in _IDENTITY_FIELDS_FOR_PROFILE:
        v = extracted.get(f) if f in ("major", "year") else extracted.pop(f, None)
        if v not in (None, "", []):
            profile_updates[f] = v
    if profile_updates:
        mem.update_profile(
            user_id,
            profile_updates,
            source_type="user_explicit",
            source_session_id=session_id,
            source_turn_index=source_turn_index,
        )
        logger.info("[Channel A] profile updated → %s", profile_updates)

    # ── Stated preferences → MemoryManager facts ──
    for field, template in _PREFERENCE_FIELDS_AS_FACTS.items():
        v = extracted.get(field)
        if v in (None, "", []):
            continue
        mem.add_fact(
            user_id,
            template.format(value=v),
            topic=f"profile:{field}",
            source_type="user_explicit",
            source_session_id=session_id,
            source_turn_index=source_turn_index,
            source_quote=source_message,
            confidence=1.0,
        )
        logger.info("[Channel A] preference fact → %s=%s", field, v)


# ── Channel B: background reflection (every N turns) ─────

async def _run_reflection_task(
    user_id: str,
    history: list[dict],
    session_id: str | None = None,
) -> None:
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
        if isinstance(pref, dict):
            text = str(pref.get("text") or "").strip()
            quote = str(pref.get("evidence_quote") or "").strip() or None
            try:
                confidence = float(pref.get("confidence", 0.65))
            except (TypeError, ValueError):
                confidence = 0.65
        else:
            # Compatibility with older adapters and test doubles.
            text = str(pref).strip()
            quote = None
            confidence = 0.60
        if not text:
            continue
        source_turn = None
        if quote:
            quote_folded = quote.casefold()
            for message in history:
                if message.get("role") != "user":
                    continue
                if quote_folded in str(message.get("content") or "").casefold():
                    source_turn = message.get("turn_index")
                    break
        mem.add_preference(
            user_id,
            text,
            source_type="llm_inferred",
            source_session_id=session_id,
            source_turn_index=source_turn,
            source_quote=quote,
            confidence=max(0.0, min(confidence, 1.0)),
        )


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
            session_id=session_id,
        )


def _tool_terms_from_agent_meta(agent_meta: dict) -> list[str]:
    """Return canonical terms used by successful Agent tool calls."""
    terms: list[str] = []
    for call in agent_meta.get("successful_tool_calls") or []:
        args = call.get("args") if isinstance(call, dict) else None
        args = args or {}
        for raw in ([args.get("term")] if args.get("term") else args.get("terms") or []):
            parsed = parse_term_key(str(raw))
            if parsed.kind == "single":
                canonical = parsed.terms[0].canonical_name
                if canonical not in terms:
                    terms.append(canonical)
    return terms


def _grounded_agent_fallback_reply(
    user_message: str,
    state: dict,
    *,
    term: Optional[str],
    reason: Optional[str] = None,
    recent_turns: Optional[list[dict]] = None,
) -> str:
    language = _response_language(user_message, recent_turns)
    course_reply = _deterministic_single_course_fallback_reply(
        user_message,
        state,
        term=term,
        reason=reason,
        language=language,
    )
    if course_reply:
        return course_reply

    if language == "zh":
        return (
            "目前无法连接回答服务。我不会凭空编造课程建议或班次信息。"
            "你可以稍后重试，或先询问一门具体课程；服务恢复后我会调用课程数据进行查询。"
        )

    return (
        "I can’t reach the agent right now, so I won’t invent "
        "course recommendations or section details. Please try again, or ask "
        "about a specific course "
        "and I’ll query the course data when the agent is available."
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
    language: str = "en",
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
    prefix = (
        "I can’t reach the agent right now. "
        "Here is the local catalog fallback for the single course you mentioned."
    )

    catalog = get_catalog(target_term)
    coverage = get_term_coverage(target_term)
    coverage_status = coverage.get("coverage_status") or "unknown"
    source_updated_at = coverage.get("updated_at") or ("未知" if language == "zh" else "unknown")
    term_label = display_term(target_term.display(), language)
    coverage_label = {
        "complete": "完整", "partial": "不完整", "stale": "已过期",
        "unavailable": "不可用", "unknown": "未知",
    }.get(coverage_status, "未知")
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
        if language == "zh":
            return (
                f"目前无法连接回答服务。课程已识别为 {ref.display()}，"
                f"但 {term_label} 的本地课程目录暂不可用，因此目前无法确认课程名称、"
                "学分、班次或先修要求。"
            )
        return (
            f"{prefix}\n\n"
            f"I parsed the course as {ref.display()}, but local catalog data for "
            f"{term_label} is unavailable. I cannot verify title, units, sections, "
            f"or prerequisites without the agent/tools."
        )

    record = catalog.get_course(ref)
    if not record:
        if language == "zh":
            coverage_note = (
                f"{term_label} 的本地数据状态为 {coverage_label}，"
                if coverage_status in {"partial", "stale"}
                else ""
            )
            return (
                f"目前无法连接回答服务。{coverage_note}"
                f"本地课程目录中未找到 {ref.display()}。"
                f"数据更新时间：{source_updated_at}。"
            )
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
    if language == "zh":
        lines = [
            "目前无法连接回答服务。以下内容直接来自本地课程目录。",
            "",
            f"**{record.ref.display()} · {record.title or '课程名称未知'}**",
            f"学分：{'未知' if units == 'unknown units' else units}",
        ]
        # The offline catalog contains English prose. Without the model we
        # cannot translate it faithfully; retain facts and state the gap.
        if record.description or record.prerequisite_text or record.restriction:
            lines.append("课程描述、先修要求和选课限制的中文说明暂不可用，请在服务恢复后继续核实。")
        if sections:
            lines.append(f"{term_label}：本地数据找到 {len(sections)} 个班次。")
        elif coverage_status == "complete":
            lines.append(f"{term_label}：本地数据中没有找到班次。")
        else:
            lines.append(
                f"{term_label}：数据状态为 {coverage_label}，暂时无法确认是否开课。"
            )
        lines.append(f"数据更新时间：{source_updated_at}。")
        return "\n".join(lines)

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
                        recent_turns=recent_turns,
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
                    **({"response_language": event["response_language"]}
                       if event.get("response_language") else {}),
                })
            elif t == "tool_call_done":
                ok = event.get("ok", True)
                if execution_meta is not None:
                    if event.get("restriction_evidence"):
                        execution_meta["restriction_evidence"] = event[
                            "restriction_evidence"
                        ]
                    if event.get("verified_facts"):
                        execution_meta["restriction_verified_facts"] = event[
                            "verified_facts"
                        ]
                    if event.get("fetch_summary") is not None:
                        execution_meta["web_fetches"] = event["fetch_summary"]
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
                tool_done_payload = {
                    "type":  "tool_call_done",
                    "name":  event.get("name"),
                    "label": event.get("label"),
                    "ok":    ok,
                }
                if event.get("fetch_summary") is not None:
                    tool_done_payload["fetch_summary"] = event["fetch_summary"]
                for field in ("section_count", "offering_status", "authoritative"):
                    if field in event:
                        tool_done_payload[field] = event[field]
                await queue.put(tool_done_payload)
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
                    "response_language": event.get("response_language")
                    or state.get("response_language")
                    or _response_language(user_message, recent_turns),
                })
            elif t == "final":
                # Tokens were already streamed; nothing extra to forward.
                if execution_meta is not None:
                    if event.get("restriction_evidence"):
                        execution_meta["restriction_evidence"] = event[
                            "restriction_evidence"
                        ]
                    if event.get("verified_facts"):
                        execution_meta["restriction_verified_facts"] = event[
                            "verified_facts"
                        ]
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
                recent_turns=recent_turns,
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
            recent_turns=recent_turns,
        )
        await queue.put({"type": "token", "text": fallback})
        return (fallback, [], [], None)

    if execution_meta is not None:
        execution_meta["successful_tools"] = sorted(successful_tools)
        execution_meta["successful_tool_calls"] = successful_tool_calls
    return (accumulated, proposed_cards, [], None)


from fastapi.responses import StreamingResponse


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
        _stream_chat(
            req,
            background_tasks,
            user["id"],
            trace_id=trace_id,
        ),
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
    llm_input_parts: list[str] = [req.message]

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

            # Establish the provider lifecycle before any profile/fact read or
            # write. Rich providers use this boundary for lazy migration and
            # per-session resources; simple providers keep it as a cheap load.
            mem.initialize_session(active_session_id, user_id)

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
            extracted = _extract_deterministic_hard_facts(req.message)
            if extracted:
                try:
                    source_turn_index = sessions_data.count_turns(
                        user_id,
                        active_session_id,
                    ) + 1
                except sessions_data.SessionNotFound:
                    source_turn_index = None
                _capture_hard_facts(
                    session,
                    extracted,
                    user_id,
                    mem,
                    session_id=active_session_id,
                    source_turn_index=source_turn_index,
                    source_message=req.message,
                )
                session = update_session(active_session_id, session, user_id=user_id)
                session = update_session(active_session_id, extracted, user_id=user_id)

            memory_context = {
                "system_prompt_block": mem.system_prompt_block(user_id),
                "prefetched_context": mem.prefetch(req.message, user_id),
                # Strict allow-list produced by the academic store. It never
                # contains email, user id, transcript text, or PDF metadata.
                "academic_context": get_ai_academic_context(user_id),
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
            llm_input_parts.extend(
                str(part)
                for part in (
                    memory_context.get("system_prompt_block"),
                    summary,
                    decisions,
                    [
                        {
                            "role": turn.get("role"),
                            "content": turn.get("content"),
                        }
                        for turn in recent_turns[-10:]
                    ],
                )
                if part
            )

            term_service = get_term_resolution_service()
            automatic_term = term_service.automatic_term()
            session_meta = sync_automatic_default(
                user_id,
                persistent_sid,
                automatic_term,
            )
            default_term = term_service.effective_for_conversation(session_meta)
            current_term = term_service.current_term()
            query_scope = resolve_query_scope(
                req.message,
                default_term.canonical_name,
                automatic_term.canonical_name,
                session_meta.get("recent_query_focus"),
                term_service.clock.now(),
                current_term=current_term.canonical_name if current_term else None,
                relative_base=term_service.relative_base().canonical_name,
            )
            query_scope = await refine_query_scope(
                query_scope, message=req.message, default_term=default_term.canonical_name,
                current_term=current_term.canonical_name if current_term else None,
                focus=session_meta.get("recent_query_focus"), recent_turns=recent_turns,
                uci_now=term_service.clock.now(),
            )
            query_terms = list(query_scope.canonical_terms)
            # Legacy Agent helpers still accept one representative term. The
            # authoritative full allowlist travels separately in state.
            query_term = (
                query_terms[0]
                if len(query_terms) == 1
                else default_term.canonical_name
            )
            response_language = _response_language(req.message, recent_turns)

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
            profile = mem.get_profile(user_id) or {}
            state["program_id"] = profile.get("program_id")
            state["catalog_year"] = profile.get("catalog_year")
            state["term"] = query_term
            state["default_term"] = default_term.canonical_name
            state["query_terms"] = query_terms
            state["current_term"] = current_term.canonical_name if current_term else None
            state["query_intent"] = query_scope.intent
            state["inferred_year"] = query_scope.inferred_year
            state["query_course_ids"] = list(query_scope.course_ids) or (
                list((session_meta.get("recent_query_focus") or {}).get("course_ids") or [])
                if query_scope.source in {"followup", "comparison", "history"} else []
            )
            state["query_term_source"] = query_scope.source
            state["query_scope_error"] = (
                query_scope.error.message if query_scope.error else None
            )
            state["response_language"] = response_language
            state["uci_now"] = term_service.clock.now()
            state["term_mode"] = session_meta.get("term_mode", "auto")
            state["term_source"] = session_meta.get(
                "term_source",
                default_term.source,
            )
            state["pending_schedule"] = session.get("pending_schedule", [])

            # Agent loop is the only chat chain. Pre-flight failures
            # return a deterministic grounded fallback from _handle_agent;
            # no legacy LLM recommendation path is started.
            agent_meta: dict = {}
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
            reply, cards, followups, _reserved = agent_result
            tool_terms = _tool_terms_from_agent_meta(agent_meta)

            observability.log_event(
                logger,
                logging.INFO,
                "term_resolution",
                session_id=persistent_sid,
                automatic_term=automatic_term.canonical_name,
                default_term=default_term.canonical_name,
                term_mode=session_meta.get("term_mode", "auto"),
                query_terms=query_terms,
                query_term_source=query_scope.source,
                tool_terms=tool_terms,
                default_term_changed=False,
            )

            mem.sync_turn(user_id, req.message, reply, active_session_id)

            # ── Phase 3.3 + 3.5 + Round 4: persist turn, schedule auto-title,
            #    then detect decisions ──
            new_turn_index, did_auto_title = _persist_turn(
                user_id, persistent_sid, req.message, reply,
                cards=cards, followups=followups,
                web_fetches=agent_meta.get("web_fetches"),
                query_terms=query_terms,
                query_term_source=query_scope.source,
                course_ids=list(query_scope.course_ids),
            )
            if reply and query_terms and query_scope.error is None:
                previous_focus = session_meta.get("recent_query_focus") or {}
                focus_course_ids = (
                    list(query_scope.course_ids)
                    or list(previous_focus.get("course_ids") or [])
                )
                focus_terms = next_recent_focus_terms(
                    req.message,
                    query_scope,
                    previous_focus,
                )
                sessions_data.update_session_meta(
                    user_id,
                    persistent_sid,
                    recent_query_focus={
                        "course_ids": focus_course_ids[:8],
                        "terms": list(focus_terms),
                        "intent": query_scope.intent,
                        "updated_at": datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                    },
                )
            _maybe_schedule_auto_title(
                background_tasks, did_auto_title,
                user_id, persistent_sid, req.message, reply,
            )
            _detect_and_pin_decisions(user_id, persistent_sid, req.message, new_turn_index)

            _maybe_schedule_reflection(background_tasks, mem, active_session_id, user_id)

            meta_event = {
                "type": "meta",
                "session_id": persistent_sid,
                "cards": cards,
                "followups": followups,
                "final_answer": reply,
                "response_language": response_language,
                "default_term": default_term.canonical_name,
                "term_mode": session_meta.get("term_mode", "auto"),
                "term_source": session_meta.get(
                    "term_source",
                    default_term.source,
                ),
                "query_terms": query_terms,
                "query_term_source": query_scope.source,
                "query_intent": query_scope.intent,
                "current_term": current_term.canonical_name if current_term else None,
                "inferred_year": query_scope.inferred_year,
                "default_term_changed": False,
                "available_terms": term_service.available_terms(
                    include=default_term.canonical_name,
                ),
            }
            if agent_meta.get("web_fetches"):
                meta_event["web_fetches"] = agent_meta["web_fetches"]
            await queue.put(meta_event)
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
                input_text="\n".join(llm_input_parts),
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
        _stream_continue(
            req,
            user["id"],
            trace_id=trace_id,
        ),
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
    final_text = ""
    final_event: Optional[dict] = None
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
                yield sse(event)
            elif t == "final":
                final_event = dict(event)
            elif t == "limit_reached":
                saw_limit_again = True
                observability.increment("sse.continue_limit_reached")
                yield sse(event)
            elif t != "token":
                yield sse(event)

        final_text = accumulated or str((final_event or {}).get("text") or "")
        if final_text:
            if not accumulated:
                yield sse({"type": "token", "text": final_text})
            completed = dict(final_event or {})
            completed.update({"type": "final", "text": final_text})
            yield sse(completed)
            yield sse({
                "type": "meta",
                "final_answer": final_text,
            })

        # Persist the resumed reply to the session log so refresh /
        # session reload doesn't lose it. We don't run the full memory
        # pipeline here (no new user message); just append the text.
        if final_text:
            try:
                sessions_data.append_turn(
                    user_id,
                    req.session_id,
                    "assistant",
                    final_text,
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
                output_text=final_text or accumulated,
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
    # Cards and calendar entries pass their own term. None resolves from
    # the conversation's backend-owned effective term.
    term: Optional[str] = None
    # Retained for old callers. M13 overlap metadata is always non-blocking.
    confirm_conflicts: bool = False


def _canonical_schedule_term(value: str) -> str:
    resolution = get_term_resolution_service().resolve_explicit(value)
    if resolution.error is not None or resolution.kind != "single":
        detail = (
            resolution.error.message
            if resolution.error is not None
            else "schedule term must resolve to one UCI term"
        )
        raise HTTPException(status_code=400, detail=detail)
    return resolution.terms[0].canonical_name


def _schedule_effective_term(
    user_id: str,
    session_id: str,
    explicit_term: Optional[str],
) -> str:
    if explicit_term:
        return _canonical_schedule_term(explicit_term)
    meta = sessions_data.get_session_meta(user_id, session_id)
    return get_term_resolution_service().effective_for_conversation(meta).canonical_name


def _legacy_schedule_term(session: dict, meta: dict) -> Optional[str]:
    candidates: set[str] = set()
    for value in (
        session.get("term"),
        meta.get("default_term"),
        meta.get("term_scope"),
    ):
        parsed = parse_term_key(str(value or ""))
        if parsed.kind == "single":
            candidates.add(parsed.terms[0].canonical_name)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _migrate_schedule_entries(
    user_id: str,
    session_id: str,
    session: dict,
) -> tuple[dict, bool]:
    """Canonicalize entry terms; mark ungrounded legacy rows unknown."""
    meta = sessions_data.get_session_meta(user_id, session_id)
    legacy_term = _legacy_schedule_term(session, meta)
    migrated: list[dict] = []
    changed = False
    for raw_entry in session.get("pending_schedule", []):
        if not isinstance(raw_entry, dict):
            changed = True
            continue
        entry = dict(raw_entry)
        raw_term = entry.get("term")
        parsed = parse_term_key(str(raw_term or ""))
        if parsed.kind == "single":
            term = parsed.terms[0].canonical_name
        elif raw_term:
            term = "unknown"
        else:
            term = legacy_term or "unknown"
        if entry.get("term") != term:
            entry["term"] = term
            changed = True
        legacy_notices = entry.pop("verification_notices", None)
        if legacy_notices is not None:
            changed = True
        if legacy_notices and not entry.get("notices"):
            entry["notices"] = list(legacy_notices)
        for legacy_key in (
            "verification_status",
            "verified_at",
            "source_badges",
            "corrections_applied",
        ):
            if legacy_key in entry:
                entry.pop(legacy_key, None)
                changed = True
        migrated.append(entry)

    if changed:
        session = update_session(
            session_id,
            {"pending_schedule": migrated},
            user_id=user_id,
        )
    return session, changed


# Fixed-size stripes bound memory while keeping each session's read/modify/write
# atomic when FastAPI runs schedule handlers in its worker pool.
_SCHEDULE_LOCKS = tuple(RLock() for _ in range(64))


def _schedule_lock(user_id, session_id):
    return _SCHEDULE_LOCKS[hash((user_id, session_id)) % len(_SCHEDULE_LOCKS)]


def _schedule_lookups(user_id: str, session_id: str):
    """Reuse this session's server-generated cards; memoize other lookups.

    Adding/removing a shown section does not need a new WebSoc request.
    Explicit refresh still fetches live data. Never accept section times
    from a mutation request or borrow cards from another user/session/term.
    """
    from app.data.db import get_sections, get_course_info

    def course_key(value):
        ref = parse_course_mention(str(value or ""))
        return ref.display() if ref else str(value or "").strip()

    cards = {}
    titles = {}
    for turn in reversed(sessions_data.read_turns(user_id, session_id)):
        if turn.get("role") != "assistant":
            continue
        for card in reversed(turn.get("cards") or []):
            if not isinstance(card, dict):
                continue
            cid = course_key(card.get("course_id"))
            parsed = parse_term_key(str(card.get("term") or ""))
            if not cid or parsed.kind != "single":
                continue
            key = (cid, parsed.terms[0].canonical_name)
            cards.setdefault(key, card)
            if card.get("title"):
                titles.setdefault(cid, card["title"])

    @lru_cache(maxsize=None)
    def sections(course_id, term):
        card = cards.get((course_key(course_id), term))
        if card and card.get("sections"):
            return {
                "found": True,
                "source": card.get("section_source"),
                "sections": [normalize_section_meeting(s) for s in card["sections"] if isinstance(s, dict)],
            }
        return get_sections(course_id, term)

    @lru_cache(maxsize=None)
    def course_info(course_id):
        title = titles.get(course_key(course_id))
        if title:
            return {"found": True, "course": {"title": title}}
        return get_course_info(course_id)

    return sections, course_info


def _resolve_section_num(course_id: str, sec: Optional[str],
                         term: Optional[str], *, section_lookup=None) -> Optional[str]:
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
    if not term or term == "unknown":
        return sec_str        # best effort; can't resolve without term
    from app.data.db import get_sections
    env = (section_lookup or get_sections)(course_id, term)
    if not env.get("found"):
        return sec_str
    for s in env.get("sections", []):
        if s.get("section_num") == sec_str or s.get("section_code") == sec_str:
            return s.get("section_num") or s.get("section_code")
    return sec_str


def _entries_match(
    entry: dict,
    course_id: str,
    req_section: Optional[str],
    term: str,
    *,
    section_lookup=None,
) -> bool:
    """True if `entry` refers to the same (course, section) as the
    request, surviving the section_num-vs-section_code mismatch
    described in _resolve_section_num."""
    if entry.get("course_id") != course_id:
        return False
    if entry.get("term") != term:
        return False
    if entry.get("section") == req_section and req_section:
        return True
    entry_norm = _resolve_section_num(course_id, entry.get("section"), term, section_lookup=section_lookup)
    req_norm   = _resolve_section_num(course_id, req_section, term, section_lookup=section_lookup)
    return entry_norm == req_norm and entry_norm is not None


def _empty_schedule_validation() -> dict:
    return {
        "valid": True,
        "warnings": [],
        "conflicts": [],
        "unknowns": [],
    }


def _append_schedule_notice(entry: dict, message: str) -> None:
    notices = entry.setdefault("notices", [])
    if message not in notices:
        notices.append(message)


def _validate_pending_schedule(pending_schedule: list[dict], *, section_lookup=None) -> dict:
    if not pending_schedule:
        return _empty_schedule_validation()

    from app.data.db import get_sections

    bundle_items = build_pending_schedule_bundle_items(
        pending_schedule,
        term=None,
        section_lookup=section_lookup or get_sections,
    )
    validation = validate_schedule_bundle(bundle_items)
    unknown_term_entries = [
        entry
        for entry in pending_schedule
        if isinstance(entry, dict) and entry.get("term") == "unknown"
    ]
    for entry in unknown_term_entries:
        validation["unknowns"].append(
            {
                "type": "unknown_term",
                "scope": "pending_schedule",
                "message": (
                    f"{entry.get('course_id') or 'Schedule entry'} "
                    "has no reliable legacy term"
                ),
                "sections": [],
            }
        )
    validation["valid"] = not validation["conflicts"] and not validation["unknowns"]
    return validation


@router.post("/schedule/add")
def add_to_schedule(
    req: ScheduleRequest,
    request: Request,
    user: dict = Depends(current_user_optional),
):
    with _schedule_lock(user["id"], req.session_id):
        return _add_to_schedule(req, request, user)


def _add_to_schedule(req, request, user):
    """Phase E4: each (course_id, section) pair is a separate schedule
    entry. Adding Lec A and Dis A1 of the same course produces TWO
    entries (and downstream, two calendar events) so the user sees
    both blocks on the day grid.

    Stores the normalized section_num so subsequent compares hit the
    fast path instead of going through _resolve_section_num every
    time."""
    check_rate_limit(request, SCHEDULE_WRITE_LIMIT, user["id"])
    user_id = user["id"]
    active_session_id = _resolve_session_id(req.session_id, user_id, None)
    session = get_or_create_session(active_session_id, user_id=user_id)
    session, _ = _migrate_schedule_entries(user_id, active_session_id, session)
    effective_term = _schedule_effective_term(
        user_id,
        active_session_id,
        req.term,
    )
    section_lookup, course_lookup = _schedule_lookups(user_id, active_session_id)
    sec_norm = _resolve_section_num(req.course_id, req.section, effective_term, section_lookup=section_lookup)
    sec_canon = sec_norm or req.section
    entry = {
        "course_id": req.course_id,
        "section": sec_canon,
        "status": "pending",
        "term": effective_term,
    }
    # Dedup using section-equivalence (handles legacy entries that stored
    # the 5-digit registrar code where the new picker stores section_num).
    is_dup = any(
        _entries_match(e, req.course_id, sec_canon, effective_term, section_lookup=section_lookup)
        for e in session.get("pending_schedule", [])
    )
    existing_terms = sorted(
        {
            entry.get("term")
            for entry in session.get("pending_schedule", [])
            if entry.get("term") and entry.get("term") != "unknown"
        }
    )
    if not is_dup:
        session.setdefault("pending_schedule", []).append(entry)
        session = update_session(
            active_session_id,
            {"pending_schedule": session["pending_schedule"]},
            user_id=user_id,
        )
    schedule_validation = _validate_pending_schedule(session.get("pending_schedule", []), section_lookup=section_lookup)
    events = _build_schedule_events(session, section_lookup=section_lookup, course_lookup=course_lookup)
    cross_term_notice = None
    if not is_dup and existing_terms and effective_term not in existing_terms:
        cross_term_notice = {
            "added_term": effective_term,
            "existing_terms": existing_terms,
        }
    return {
        "ok": True,
        "pending_schedule": session["pending_schedule"],
        "events": events,
        "schedule_validation": schedule_validation,
        "cross_term_notice": cross_term_notice,
    }


@router.post("/schedule/remove")
def remove_from_schedule(
    req: ScheduleRequest,
    request: Request,
    user: dict = Depends(current_user_optional),
):
    with _schedule_lock(user["id"], req.session_id):
        return _remove_from_schedule(req, request, user)


def _remove_from_schedule(req, request, user):
    """If `section` is provided, remove only that specific (course, section).
    Section-equivalence aware so legacy entries (5-digit codes stored
    where section_num is now expected) get matched and removed.
    If omitted, remove every entry for the course."""
    check_rate_limit(request, SCHEDULE_WRITE_LIMIT, user["id"])
    user_id = user["id"]
    active_session_id = _resolve_session_id(req.session_id, user_id, None)
    session = get_or_create_session(active_session_id, user_id=user_id)
    session, _ = _migrate_schedule_entries(user_id, active_session_id, session)
    effective_term = (
        "unknown"
        if req.term == "unknown"
        else _schedule_effective_term(user_id, active_session_id, req.term)
    )
    section_lookup, course_lookup = _schedule_lookups(user_id, active_session_id)
    if req.section:
        session["pending_schedule"] = [
            e for e in session.get("pending_schedule", [])
            if not _entries_match(e, req.course_id, req.section, effective_term, section_lookup=section_lookup)
        ]
    else:
        session["pending_schedule"] = [
            e
            for e in session.get("pending_schedule", [])
            if not (
                e.get("course_id") == req.course_id
                and e.get("term") == effective_term
            )
        ]
    session = update_session(
        active_session_id,
        {"pending_schedule": session["pending_schedule"]},
        user_id=user_id,
    )
    events = _build_schedule_events(session, section_lookup=section_lookup, course_lookup=course_lookup)
    return {
        "ok": True,
        "pending_schedule": session["pending_schedule"],
        "events": events,
        "schedule_validation": _validate_pending_schedule(session["pending_schedule"], section_lookup=section_lookup),
    }


class ScheduleClearRequest(BaseModel):
    session_id: str = ""
    term: Optional[str] = None


class ScheduleRefreshRequest(BaseModel):
    session_id: str = ""


_SCHEDULE_REFRESH_NOTICE_PREFIXES = (
    "Section could not be resolved",
    "Meeting time is TBA",
    "This section is marked cancelled",
    "This section is currently full",
    "Live schedule refresh",
)


def _refreshable_schedule_notices(entry: dict) -> list[str]:
    return [
        str(item)
        for item in entry.get("notices") or []
        if not str(item).startswith(_SCHEDULE_REFRESH_NOTICE_PREFIXES)
    ]


def _apply_live_schedule_result(entry: dict, result: Optional[dict]) -> dict:
    """Refresh one entry without deleting the user's planning intent."""

    refreshed = dict(entry)
    refreshed["notices"] = _refreshable_schedule_notices(entry)
    if not isinstance(result, dict) or not result.get("sections"):
        _append_schedule_notice(
            refreshed,
            "Live schedule refresh was unavailable; the planning item was kept.",
        )
        return refreshed

    section_key = str(entry.get("section") or "")
    matched = next(
        (
            section
            for section in result.get("sections") or []
            if str(section.get("section_num") or "") == section_key
            or str(section.get("section_code") or "") == section_key
        ),
        None,
    )
    if matched is None:
        refreshed["materialization_status"] = "unresolved"
        _append_schedule_notice(
            refreshed,
            "Live schedule refresh could not resolve this exact section; no calendar block was created.",
        )
        return refreshed

    snapshot_fields = (
        "section_code",
        "section_num",
        "section_type",
        "days",
        "start_time",
        "end_time",
        "time_is_tba",
        "time_display",
        "final_exam",
        "units",
        "location",
        "instructors",
        "status",
        "is_cancelled",
        "seats_open",
    )
    snapshot = {
        field: matched.get(field)
        for field in snapshot_fields
    }
    snapshot["source"] = result.get("source")
    snapshot["retrieved_at"] = result.get("retrieved_at")
    refreshed["materialized_section"] = snapshot
    refreshed["materialization_status"] = (
        "resolved"
        if section_time_status(matched, matched) != "unknown"
        else "tba"
    )
    stale = bool(result.get("stale")) or result.get("source") == "last_known_live"
    refreshed["data_status"] = "cached" if stale else "current"
    refreshed["refreshed_at"] = result.get("retrieved_at")
    sources = list(refreshed.get("sources") or [])
    source = result.get("source")
    if source and source not in sources:
        sources.append(source)
    refreshed["sources"] = sources
    if stale:
        _append_schedule_notice(
            refreshed,
            "Live schedule refresh used the latest cached result; confirm current status in WebReg.",
        )
    else:
        _append_schedule_notice(
            refreshed,
            "Live schedule refresh confirmed the section; recheck eligibility in official UCI systems.",
        )
    status = str(matched.get("status") or "").upper()
    if matched.get("is_cancelled") or status == "CANCELLED":
        _append_schedule_notice(refreshed, "This section is marked cancelled.")
    if status == "FULL":
        _append_schedule_notice(refreshed, "This section is currently full.")
    if refreshed["materialization_status"] == "tba":
        _append_schedule_notice(
            refreshed,
            "Meeting time is TBA; the planning item remains in Schedule.",
        )
    return refreshed


@router.post("/schedule/refresh")
async def refresh_schedule(
    req: ScheduleRefreshRequest,
    request: Request,
    user: dict = Depends(current_user_optional),
):
    """Explicitly refresh live section state without deleting any entry."""

    from app.data.db import get_live_sections

    check_rate_limit(request, SCHEDULE_WRITE_LIMIT, user["id"])
    user_id = user["id"]
    active_session_id = _resolve_session_id(req.session_id, user_id, None)
    session = get_or_create_session(active_session_id, user_id=user_id)
    session, _ = _migrate_schedule_entries(user_id, active_session_id, session)
    entries = [
        dict(entry)
        for entry in session.get("pending_schedule", [])
        if isinstance(entry, dict)
    ]
    unique_keys = {
        (str(entry.get("course_id") or ""), str(entry.get("term") or ""))
        for entry in entries
        if entry.get("course_id")
        and entry.get("term")
        and entry.get("term") != "unknown"
    }
    task_by_key = {
        key: asyncio.create_task(
            asyncio.to_thread(
                get_live_sections,
                key[0],
                key[1],
                force_refresh=True,
                request_timeout_s=2.5,
            )
        )
        for key in unique_keys
    }
    done, pending = await asyncio.wait(
        task_by_key.values(),
        timeout=3.0,
    ) if task_by_key else (set(), set())
    for task in pending:
        task.cancel()
    result_by_key: dict[tuple[str, str], Optional[dict]] = {}
    for key, task in task_by_key.items():
        if task not in done:
            result_by_key[key] = None
            continue
        try:
            result_by_key[key] = task.result()
        except Exception as exc:
            logger.warning(
                "[schedule] live refresh degraded for %s %s: %s",
                key[0],
                key[1],
                exc,
            )
            result_by_key[key] = None

    payload = await asyncio.to_thread(
        _finish_schedule_refresh, user_id, active_session_id, result_by_key,
    )
    return {**payload, "timed_out": bool(pending)}


def _finish_schedule_refresh(user_id, session_id, result_by_key):
    with _schedule_lock(user_id, session_id):
        # The user may add/remove sections while the live requests run.
        session = get_or_create_session(session_id, user_id=user_id)
        refreshed_entries = []
        for entry in session.get("pending_schedule", []):
            key = (entry.get("course_id"), entry.get("term"))
            refreshed_entries.append(
                _apply_live_schedule_result(entry, result_by_key[key])
                if key in result_by_key else entry
            )
        session = update_session(session_id, {"pending_schedule": refreshed_entries}, user_id=user_id)
        cached_lookup, course_lookup = _schedule_lookups(user_id, session_id)

        def section_lookup(course_id, term):
            result = result_by_key.get((course_id, term))
            if result and result.get("sections"):
                return {**result, "found": True}
            return cached_lookup(course_id, term)

        return {
            "ok": True,
            "pending_schedule": session.get("pending_schedule", []),
            "events": _build_schedule_events(session, section_lookup=section_lookup, course_lookup=course_lookup),
            "schedule_validation": _validate_pending_schedule(
                session.get("pending_schedule", []), section_lookup=section_lookup,
            ),
        }


@router.post("/schedule/clear")
def clear_schedule(
    req: ScheduleClearRequest,
    request: Request,
    user: dict = Depends(current_user_optional),
):
    with _schedule_lock(user["id"], req.session_id):
        return _clear_schedule(req, request, user)


def _clear_schedule(req, request, user):
    """Wipe every entry in this session's pending_schedule. Escape hatch
    when the user accumulates stuck entries (e.g. from legacy format
    that the section-equivalence fix can't auto-resolve)."""
    check_rate_limit(request, SCHEDULE_WRITE_LIMIT, user["id"])
    user_id = user["id"]
    active_session_id = _resolve_session_id(req.session_id, user_id, None)
    update_session(active_session_id, {"pending_schedule": []}, user_id=user_id)
    return {
        "ok": True,
        "pending_schedule": [],
        "events": [],
        "schedule_validation": _empty_schedule_validation(),
    }


@router.get("/schedule")
def get_schedule(
    session_id: str,
    user: dict = Depends(current_user_optional),
):
    with _schedule_lock(user["id"], session_id):
        return _get_schedule(session_id, user)


def _get_schedule(session_id, user):
    user_id = user["id"]
    active_session_id = _resolve_session_id(session_id, user_id, None)
    session = get_or_create_session(active_session_id, user_id=user_id)
    session, migrated = _migrate_schedule_entries(
        user_id,
        active_session_id,
        session,
    )
    section_lookup, course_lookup = _schedule_lookups(user_id, active_session_id)
    return {
        "ok": True,
        "pending_schedule": session.get("pending_schedule", []),
        "events": _build_schedule_events(session, section_lookup=section_lookup, course_lookup=course_lookup),
        "schedule_validation": _validate_pending_schedule(
            session.get("pending_schedule", []), section_lookup=section_lookup,
        ),
        "migrated": migrated,
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


def _build_schedule_events(session, *, section_lookup=None, course_lookup=None):
    """
    Materialize the session's pending_schedule into calendar events.

    Every entry must carry its own canonical term. Unknown legacy entries
    remain visible in ``pending_schedule`` but cannot be materialized into
    timed events. Uses db.get_sections's envelope shape ({found, sections: [...]})
    plus the extended SectionRecord fields (section_code / days /
    start_time / end_time / instructors[]).
    """
    from app.data.db import get_sections, get_course_info

    events = []
    for entry in session.get("pending_schedule", []):
        cid, sid = entry["course_id"], entry.get("section") or "A"
        entry_term = entry.get("term")
        if not entry_term or entry_term == "unknown":
            continue

        course_env = (course_lookup or get_course_info)(cid)
        title = (
            course_env.get("course", {}).get("title", cid)
            if course_env.get("found") else cid
        )

        # A server-verified refresh is newer than the bundled catalog. Using
        # the catalog first can hide real meetings behind an old TBA record.
        snapshot = entry.get("materialized_section")
        sec = snapshot if isinstance(snapshot, dict) and sid in (
            str(snapshot.get("section_num") or ""),
            str(snapshot.get("section_code") or ""),
        ) else None
        if sec is None:
            sec_env = (section_lookup or get_sections)(cid, entry_term)
            sections = sec_env.get("sections", []) if sec_env.get("found") else []
            # Never substitute an unrelated first section on an exact miss.
            sec = next((s for s in sections if sid in (
                str(s.get("section_num") or ""), str(s.get("section_code") or ""),
            )), None)
        if sec is None or section_time_status(sec, sec) == "unknown":
            continue

        sec = normalize_section_meeting(sec)
        start, end = sec.get("start_time"), sec.get("end_time")
        days_str = sec.get("days")
        if not (start and end and days_str):
            continue  # async / TBA sections have no calendar slot

        instructors = sec.get("instructors") or []
        primary_instructor = instructors[0] if instructors else ""

        for day in calendar_day_names(days_str):
            events.append({
                "course_id":   cid,
                "term":        entry_term,
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
