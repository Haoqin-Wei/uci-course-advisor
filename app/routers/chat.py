"""
Chat Router — orchestrates the SKILL.md pipeline.

Two-channel memory architecture:
    Channel A (immediate, every turn):
        extract_info_llm() pulls hard facts from the user's message.
        _capture_hard_facts() routes them to the right destinations:
          • course status → session lists + facts.json
          • identity → profile.json
          • stated preferences (difficulty / goal) → facts.json

    Channel B (periodic, background):
        Every REFLECTION_INTERVAL turns, after the response is sent,
        FastAPI BackgroundTasks fires _run_reflection_task(). That task
        spawns an independent LLM call for SOFT pattern detection.

Validation (Phase 1):
    After generate_answer_llm() returns, the answer is run through the
    Phase 1 validator suite (course_exists / instructor / offered_term /
    consistency). Hallucinated course IDs and unknown professors get
    annotated as footer issues. validation_report is attached to
    ChatResponse for inspection.

Logging:
    INFO-level logs at every capture and reflection step so the operator
    can watch the memory pipeline live in the uvicorn terminal.
"""

import asyncio
import logging
import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from app.auth.deps import current_user_optional
from app.modules.intent import classify_intent
from app.modules.state import (
    get_or_create_session, update_session,
    load_student_into_session, get_known_fields,
)
from app.modules.clarification import (
    detect_missing_fields, needs_clarification,
    build_clarification_response, extract_info_from_message,
)
from app.modules.query import (
    query_course_recommendations, query_single_course,
)
from app.modules.answer import (
    generate_recommendation_answer, generate_single_query_answer,
)
from app.modules.followup import generate_followups, generate_single_query_followups
from app.memory import get_memory_manager

# ── Phase 3.3 / 3.5 — session storage + decision detection ──
from app.data import sessions as sessions_data
from app.modules import decision_detector
# ─────────────────────────────────────────────────────────────

# ── Validation Phase 1 ───────────────────────────────────
from app.catalog.term import Term, get_term_registry
from app.catalog.cache import get_catalog
from app.validation import (
    ValidationContext, validate, decide_action, apply_report, write_log,
)
# ─────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

router = APIRouter()

# Scan for course IDs anywhere in a string.
#
# We use explicit ASCII look-arounds instead of \b because Python's
# regex \b is Unicode-aware by default: in "我决定选CS122A", the
# character 选 is classified as a "word" character (it's alphanumeric
# in Unicode), so \b between 选 and CS does NOT match. The same
# problem hits course IDs sandwiched between Chinese characters
# anywhere ("选CS122A课"). The look-arounds below say "not preceded /
# followed by an ASCII letter or digit" — Chinese characters are
# fine as neighbors.
_COURSE_ID_SCAN = re.compile(
    r'(?<![A-Za-z0-9])[A-Z]{1,8}\d+[A-Z]?(?![A-Za-z0-9])',
    re.IGNORECASE,
)


class ChatRequest(BaseModel):
    message: str
    session_id: str = ""
    term: Optional[str] = None                          # 新增
    system_prompt: Optional[str] = None                 # 新增：前端自定义 LLM system prompt


class ChatResponse(BaseModel):
    reply: str
    cards: list[dict] = Field(default_factory=list)
    followups: list[str] = Field(default_factory=list)
    intent: str = ""
    session_state: dict = Field(default_factory=dict)
    pending_schedule: list[dict] = Field(default_factory=list)
    validation_report: Optional[dict] = None            # 新增


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


def _resolve_referenced_course(recent_turns: list[dict]) -> Optional[str]:
    """
    Scan recent conversation turns (newest first) for a course ID.

    Used by _handle_single_query when the current user message contains
    no explicit course ID but is likely a follow-up referring to
    something earlier ("那个课", "the first one", "上面提到的那门").

    Prefers user-side mentions (the subject the user steered the
    conversation toward) over assistant-side mentions (the assistant
    might rattle off many courses in a recommendation reply, only one
    of which is what the user wants more info on).

    Returns the most recent course ID seen, or None.
    """
    if not recent_turns:
        return None

    # First pass: walk user turns newest-first
    for turn in reversed(recent_turns):
        if turn.get("role") != "user":
            continue
        matches = _COURSE_ID_SCAN.findall(turn.get("content") or "")
        if matches:
            return matches[0].upper()

    # Second pass: fall back to assistant turns if no user mention
    for turn in reversed(recent_turns):
        if turn.get("role") != "assistant":
            continue
        matches = _COURSE_ID_SCAN.findall(turn.get("content") or "")
        if matches:
            return matches[0].upper()

    return None


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


# Identity fields — go to profile.json (long-term identity)
_IDENTITY_FIELDS_FOR_PROFILE = ("major", "year", "target_gpa", "graduation_term")

# Stated-preference fields — go to facts.json as event-style entries
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
    # ── Course status → session lists + facts.json ──
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

    # ── Identity → profile.json ──
    profile_updates = {}
    for f in _IDENTITY_FIELDS_FOR_PROFILE:
        v = extracted.get(f) if f in ("major", "year") else extracted.pop(f, None)
        if v not in (None, "", []):
            profile_updates[f] = v
    if profile_updates:
        mem.update_profile(user_id, profile_updates)
        logger.info("[Channel A] profile updated → %s", profile_updates)

    # ── Stated preferences → facts.json ──
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
    for m in _COURSE_ID_SCAN.finditer(text):
        cid = m.group().upper().replace(' ', '')
        if cid not in seen:
            seen.add(cid)
            result.append(cid)
    return result


# ── Main chat endpoint ───────────────────────────────────

@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_user_optional),
):
    # Authoritative user id comes from the signed session cookie via
    # current_user_optional. Anonymous callers fall back to demo_001
    # so the legacy demo keeps working.
    user_id = user["id"]
    mem = get_memory_manager()
    persistent_sid = _resolve_session_id(req.session_id, user_id, req.term)
    active_session_id = persistent_sid

    mem.initialize_session(active_session_id, user_id)

    session = get_or_create_session(active_session_id, user_id=user_id)
    if not session.get("major"):
        load_student_into_session(active_session_id, user_id, user_id=user_id)
        session = get_or_create_session(active_session_id, user_id=user_id)

    mem.on_turn_start(active_session_id, user_id)
    logger.info(
        "[turn %d] user=%s msg=%r",
        mem.turn_count(active_session_id), user_id, req.message[:120],
    )

    # ── Channel A ──
    extracted = await extract_info_from_message(req.message)
    if extracted:
        logger.info("[Channel A] extracted from message: %s", extracted)
        _capture_hard_facts(session, extracted, user_id, mem)
        session = update_session(active_session_id, session, user_id=user_id)
        if extracted:
            session = update_session(active_session_id, extracted, user_id=user_id)
    else:
        logger.info("[Channel A] nothing extracted from message")

    intent_result = await classify_intent(req.message)
    intent = intent_result["intent"]
    logger.info("[intent] %s (confidence=%s)", intent, intent_result.get("confidence"))

    llm_entities = intent_result.get("entities", {})
    if llm_entities:
        eu = {}
        for f in ("term", "major", "difficulty_preference", "recommendation_goal"):
            if llm_entities.get(f):
                eu[f] = llm_entities[f]
        if eu:
            session = update_session(active_session_id, eu, user_id=user_id)

    memory_context = {
        "system_prompt_block": mem.system_prompt_block(user_id),
        "prefetched_context": mem.prefetch(req.message, user_id),
    }

    # off_topic used to short-circuit with a hardcoded template here;
    # removed for the same reason as the streaming path — the
    # classifier sees only the latest message and can't tell "继续"
    # from genuine off-topic. Let the downstream handlers + LLM
    # handle these cases with conversation context instead.

    if needs_clarification(session, intent):
        missing = detect_missing_fields(session, intent)
        reply = build_clarification_response(missing)
        mem.sync_turn(user_id, req.message, reply, active_session_id)
        new_turn_index, did_auto_title = _persist_turn(
            user_id, persistent_sid, req.message, reply,
        )
        _maybe_schedule_auto_title(
            background_tasks, did_auto_title,
            user_id, persistent_sid, req.message, reply,
        )
        _detect_and_pin_decisions(user_id, persistent_sid, req.message, new_turn_index)
        _maybe_schedule_reflection(background_tasks, mem, active_session_id, user_id)
        return ChatResponse(reply=reply, intent=intent,
                            session_state=get_known_fields(active_session_id, user_id=user_id))

    state = get_known_fields(active_session_id, user_id=user_id)

    # ── Sync frontend's term to session state ──
    # The dropdown is authoritative for THIS turn — without this, the
    # downstream query falls back to state["term"] (often empty or stale)
    # and retrieves nothing, causing the LLM to hallucinate courses.
    if req.term and req.term != state.get("term"):
        update_session(active_session_id, {"term": req.term}, user_id=user_id)
        state = get_known_fields(active_session_id, user_id=user_id)
        logger.info("[term-sync] frontend term %r written to session %s",
                    req.term, active_session_id)

    validation_dict = None                              # 新增：默认 None

    if intent == "single_query":
        reply, cards, followups = await _handle_single_query(
            req.message, state, memory_context,
            system_prompt=req.system_prompt,            # 新增
        )
    else:
        # ── 新增：把 session_id / term / system_prompt 传进去 ──
        reply, cards, followups, validation_dict = await _handle_recommendation(
            req.message, state, memory_context,
            session_id=active_session_id, term_str=req.term,
            system_prompt=req.system_prompt,            # 新增
        )

    mem.sync_turn(user_id, req.message, reply, active_session_id)
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

    return ChatResponse(
        reply=reply, cards=cards, followups=followups,
        intent=intent, session_state=state,
        pending_schedule=session.get("pending_schedule", []),
        validation_report=validation_dict,              # 新增
    )


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


# ── Card builder ─────────────────────────────────────────

def _build_card(item: dict, state: dict) -> dict:
    c = item["course"]
    reasons = []
    major = state.get("major")
    if major and major in c.get("major_requirement", []):
        reasons.append(f"Counts toward {major}")
    grades = item.get("grade_distribution") or {}
    avg = grades.get("avg_gpa", 0)
    if avg >= 3.3:
        reasons.append(f"Generous grading (avg GPA {avg:.1f})")
    for sec in item.get("sections", []):
        pr = sec.get("professor_rating") or {}
        if pr.get("overall") and pr["overall"] >= 4.5:
            reasons.append(f"{sec['instructor']} rated {pr['overall']}/5")
            break
    return {
        "course_id": c["course_id"], "title": c["title"],
        "units": c["units"], "department": c.get("department", ""),
        "description": c.get("description", ""),
        "ge_category": c.get("ge_category"),
        "major_requirement": c.get("major_requirement", []),
        "prereq_met": item.get("prereq_met", True),
        "prereq_missing": item.get("prereq_missing", []),
        "has_conflict": item.get("has_conflict", False),
        "conflict_summary": item.get("conflict_summary", ""),
        "conflict_status":  item.get("conflict_status",  "none"),
        "grade_distribution": item.get("grade_distribution"),
        "sections": item.get("sections", []),
        "reason": ". ".join(reasons) if reasons else "Solid option.",
    }


async def _handle_recommendation(
    user_msg,
    state,
    memory_context=None,
    session_id=None,                                    # 新增
    term_str=None,                                      # 新增
    system_prompt=None,                                 # 新增
    on_token=None,                                      # 新增：流式回调
    # ── Phase 3.4 ─────────────────────────────────────
    recent_turns: Optional[list[dict]] = None,
    decisions: Optional[list[dict]] = None,
    summary: Optional[str] = None,
):
    from app.llm.adapter import generate_answer_llm
    results = query_course_recommendations(
        # No default — the agent path is primary now; if this legacy
        # handler runs without a term in session state, let it fail
        # explicitly rather than silently routing to a wrong term.
        term=state.get("term"), major=state.get("major"),
        completed_courses=state.get("completed_courses", []),
        selected_courses=state.get("selected_courses", []),
        difficulty_preference=state.get("difficulty_preference"),
        recommendation_goal=state.get("recommendation_goal"),
    )
    # Empty / whitespace-only override → adapter falls back to default.
    sp_override = system_prompt.strip() if (system_prompt and system_prompt.strip()) else None

    if on_token is not None:
        # Streaming path — yield chunks to caller as they arrive.
        from app.llm.adapter import stream_answer_llm
        chunks: list[str] = []
        async for chunk in stream_answer_llm(
            user_msg, results, state,
            memory_context=memory_context,
            system_prompt_override=sp_override,
            recent_turns=recent_turns,
            decisions=decisions,
            summary=summary,
        ):
            chunks.append(chunk)
            await on_token(chunk)
        answer = "".join(chunks) or None
    else:
        # Non-streaming path (existing behavior).
        answer = await generate_answer_llm(
            user_msg, results, state,
            memory_context=memory_context,
            system_prompt_override=sp_override,             # 新增
            recent_turns=recent_turns,
            decisions=decisions,
            summary=summary,
        )

    if answer:
        all_candidates = results.get("primary", []) + results.get("flagged", [])
        by_id = {item["course"]["course_id"].upper(): item for item in all_candidates}
        cards = []
        for cid in _course_ids_in_order(answer):
            if cid in by_id:
                cards.append(_build_card(by_id[cid], state))
        if not cards:
            cards = [_build_card(i, state) for i in results.get("primary", [])[:3]]
    else:
        answer = generate_recommendation_answer(results, state)
        cards = [_build_card(i, state) for i in results.get("primary", [])]

    # ── Validation Phase 1 ─────────────────────────────────
    # Pick a target term in order of priority:
    #   1. req.term (frontend explicit)
    #   2. state["term"] (session state)
    #   3. registry.default() (latest loaded term — demo fallback)
    validation_dict = None
    target_term = (
        Term.parse(term_str or "")
        or Term.parse(state.get("term", ""))
    )
    catalog = get_catalog(target_term) if target_term else None
    # Demo fallback: when the asked term has no loaded data, use whatever
    # term IS loaded. Remove this fallback once you have multi-term data.
    if catalog is None:
        fallback_term = get_term_registry().default()
        if fallback_term:
            catalog = get_catalog(fallback_term)
            if catalog:
                logger.info(
                    "[validation] term %r has no data; falling back to %s",
                    state.get("term"), fallback_term.term_id,
                )

    if catalog and answer:
        ctx = ValidationContext(
            llm_answer=answer,
            retrieved=results,
            catalog=catalog,
            session_state=state,
            user_message=user_msg,
        )
        report = validate(ctx)
        action = decide_action(report)
        answer, cards, changed = apply_report(answer, cards, report, action)
        write_log(ctx, report, action, changed, session_id=session_id)
        validation_dict = report.to_dict()
        logger.info(
            "[validation] overall=%s errors=%d warnings=%d action=%s",
            report.overall, len(report.errors), len(report.warnings), action.value,
        )
    else:
        logger.info("[validation] skipped (no catalog or no answer)")
    # ───────────────────────────────────────────────────────

    followups = generate_followups(results, state, "course_recommendation")
    return answer, cards, followups, validation_dict


async def _handle_single_query(message, state, memory_context=None, system_prompt=None, on_token=None,
                               recent_turns: Optional[list[dict]] = None,
                               decisions: Optional[list[dict]] = None,
                               summary: Optional[str] = None):
    from app.llm.adapter import generate_answer_llm
    sp_override = system_prompt.strip() if (system_prompt and system_prompt.strip()) else None

    async def _call_llm(payload_data):
        """Run generate_answer_llm OR stream_answer_llm depending on on_token."""
        if on_token is None:
            return await generate_answer_llm(
                message, payload_data, state,
                memory_context=memory_context,
                system_prompt_override=sp_override,
                recent_turns=recent_turns,
                decisions=decisions,
                summary=summary,
            )
        from app.llm.adapter import stream_answer_llm
        chunks: list[str] = []
        async for chunk in stream_answer_llm(
            message, payload_data, state,
            memory_context=memory_context,
            system_prompt_override=sp_override,
            recent_turns=recent_turns,
            decisions=decisions,
            summary=summary,
        ):
            chunks.append(chunk)
            await on_token(chunk)
        return "".join(chunks) or None

    # ── Phase 3 R3 cleanup: regex extraction + real catalog ──
    # Was: `for c in mock_data.COURSES: if c["course_id"].lower() in ml: ...`
    # Problems with the old approach:
    #   - lower()-substring match falsely matches "CS33" inside "CS331"
    #   - only finds courses present in mock_data (10 of 6687)
    #   - drags mock_data into the hot path even when LLM is fine
    # The fix: extract course IDs via the Unicode-safe regex (same one
    # _resolve_referenced_course uses), then query the REAL catalog
    # through db.query_single_course (loads from data/uci/courses.csv).
    for raw_course_id in _COURSE_ID_SCAN.findall(message):
        course_id = raw_course_id.upper()
        data = query_single_course(course_id, state.get("term"))
        if data:
            card = {
                "course_id": data["course"]["course_id"],
                "title": data["course"]["title"],
                "units": data["course"]["units"],
                "department": data["course"].get("department", ""),
                "description": data["course"].get("description", ""),
                "ge_category": data["course"].get("ge_category"),
                "major_requirement": data["course"].get("major_requirement", []),
                "prereq_met": True, "prereq_missing": [],
                "has_conflict": False,
                "grade_distribution": data.get("grade_distribution"),
                "sections": data.get("sections", []),
                "reason": "",
            }
            ans = await _call_llm(data)
            if not ans:
                logger.warning(
                    "[single_query] LLM returned empty for %s — using template fallback",
                    course_id,
                )
                ans = generate_single_query_answer(data)
                if on_token:
                    await on_token(ans)        # fallback also reaches the stream
            fu = generate_single_query_followups(course_id, state)
            return ans, [card], fu
    # (Legacy: the old code looped over mock_data.PROFESSOR_RATINGS here
    # to handle "how is professor X" queries. That mock table is gone —
    # the agent's get_professor_rating tool hits Anteater /instructors
    # directly. This branch is unreachable in the current pipeline; left
    # as a no-op rather than restored so the fallback path doesn't try
    # to invent professors out of nothing.)

    # ── Phase 3.4 polish: referential follow-up resolution ──
    # The user's current message has no explicit course ID or
    # professor name — but they're likely referring to something we
    # discussed earlier ("那个课怎么样" / "what about that course").
    #
    # Strategy:
    #   1. Scan recent_turns for the most recently mentioned course ID
    #   2. If found → run query_single_course on it so we get REAL
    #      grade distribution + sections (not LLM-fabricated stats)
    #   3. Pass the full data to the LLM, which now has both history
    #      and real data — produces a grounded answer + a course card
    #   4. If no course in history either → fall through to LLM with
    #      history alone, then to the hardcoded help message
    if recent_turns:
        referenced = _resolve_referenced_course(recent_turns)
        if referenced:
            data = query_single_course(referenced, state.get("term"))
            if data:
                logger.info("[single_query] referential resolve %r → %s (full data)",
                            message[:40], referenced)
                card = {
                    "course_id": data["course"]["course_id"],
                    "title": data["course"]["title"],
                    "units": data["course"]["units"],
                    "department": data["course"].get("department", ""),
                    "description": data["course"].get("description", ""),
                    "ge_category": data["course"].get("ge_category"),
                    "major_requirement": data["course"].get("major_requirement", []),
                    "prereq_met": True, "prereq_missing": [],
                    "has_conflict": False,
                    "grade_distribution": data.get("grade_distribution"),
                    "sections": data.get("sections", []),
                    "reason": "",
                }
                ans = await _call_llm(data)
                if ans:
                    fu = generate_single_query_followups(referenced, state)
                    return ans, [card], fu
                # LLM empty → fall through to history-only call

        # No course in history (or query returned None) — try LLM
        # with history alone. It may still produce a useful answer
        # for non-course follow-ups ("你刚才的建议靠谱吗").
        ans = await _call_llm({})
        if ans:
            return ans, [], []
        # else fall through to fallback

    fallback = ("I'm not sure which course or professor you mean. "
                "Try a course ID like ICS33 or a professor name.")
    if on_token:
        await on_token(fallback)
    return (fallback, [], [])


# ══════════════════════════════════════════════════════════
#  Streaming endpoint  /api/chat/stream
# ══════════════════════════════════════════════════════════
#
# Same logic as the /api/chat endpoint above, but the LLM-generated
# `reply` field is streamed back via Server-Sent Events instead of
# waiting for the full text. cards/followups/validation are sent as
# one final `meta` event.
#
# Why it matters:
#   - UX: user sees text appearing immediately (no "Send → 10s blank → big reply")
#   - Stop button: aborting the fetch closes the HTTP connection, FastAPI
#     cancels the producer task, the async iterator inside stream_answer_llm
#     gets CancelledError, AsyncOpenAI closes its connection to DeepSeek,
#     and DeepSeek stops generating tokens. No wasted API tokens.
#
# Wire format:
#   data: {"type": "token", "text": "<chunk>"}
#   data: {"type": "token", "text": "<chunk>"}
#   ...
#   data: {"type": "meta",  "cards": [...], "followups": [...], ...}
#   data: {"type": "done"}


# ── Agent-loop handler (replaces single_query / recommendation) ──

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
) -> Optional[tuple[str, list, list, Optional[dict]]]:
    """
    Drive a tool-using LLM turn via app.agent.loop and forward its
    events to the SSE queue.

    Pre-flight fallback: if the agent's FIRST event is an error (LLM
    call failed before any output reached the client), return None so
    the caller can fall back to the legacy single_query / recommendation
    handler. Mid-flight errors surface as visible error events — by
    that point the user has already seen partial output, falling back
    would produce duplicate text.

    Returns (reply_text, cards, followups, validation) on success.

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
            # error, the agent never streamed anything to the client,
            # so legacy fallback is safe.
            if not saw_any_event:
                saw_any_event = True
                if t == "error":
                    logger.warning("[agent] pre-flight error, falling back: %s",
                                   event.get("message"))
                    return None

            if t == "token":
                accumulated += event.get("text", "")
                await queue.put({"type": "token", "text": event["text"]})
            elif t == "tool_call_start":
                await queue.put({
                    "type":  "tool_call_start",
                    "name":  event.get("name"),
                    "label": event.get("label"),
                    "args":  event.get("args"),
                })
            elif t == "tool_call_done":
                await queue.put({
                    "type":  "tool_call_done",
                    "name":  event.get("name"),
                    "label": event.get("label"),
                    "ok":    event.get("ok", True),
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
                await queue.put({
                    "type": "error",
                    "message": event.get("message", "agent error"),
                })
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("[agent handler] failed: %s", e)
        if not accumulated:
            return None    # nothing shown → safe to fall back
        await queue.put({"type": "error", "message": str(e)})
        return (accumulated, proposed_cards, [], None)

    if not saw_any_event:
        # Generator yielded zero events — typically LLM_ENABLED=False.
        return None

    return (accumulated, proposed_cards, [], None)


from fastapi.responses import StreamingResponse


@router.post("/chat/stream")
async def chat_stream_endpoint(
    req: ChatRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_user_optional),
):
    return StreamingResponse(
        _stream_chat(req, background_tasks, user["id"]),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # tell nginx-like proxies not to buffer
        },
    )


async def _stream_chat(req: ChatRequest, background_tasks: BackgroundTasks, user_id: str):
    """Async generator yielding SSE-formatted events. `user_id` is resolved
    from the session cookie in the entry-point endpoint and passed in
    rather than re-derived here, since dependencies don't compose into
    inner async-generator scopes."""
    import asyncio
    import json as _json

    def sse(event: dict) -> str:
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
            persistent_sid = _resolve_session_id(req.session_id, user_id, req.term)
            active_session_id = persistent_sid

            session = get_or_create_session(active_session_id, user_id=user_id)
            if not session.get("major"):
                load_student_into_session(active_session_id, user_id, user_id=user_id)
                session = get_or_create_session(active_session_id, user_id=user_id)

            mem.on_turn_start(active_session_id, user_id)
            logger.info("[stream turn %d] user=%s session=%s msg=%r",
                        mem.turn_count(active_session_id), user_id,
                        persistent_sid, req.message[:120])

            # Channel A
            extracted = await extract_info_from_message(req.message)
            if extracted:
                _capture_hard_facts(session, extracted, user_id, mem)
                session = update_session(active_session_id, session, user_id=user_id)
                if extracted:
                    session = update_session(active_session_id, extracted, user_id=user_id)

            intent_result = await classify_intent(req.message)
            intent = intent_result["intent"]
            logger.info("[stream intent] %s", intent)

            llm_entities = intent_result.get("entities", {})
            if llm_entities:
                eu = {f: llm_entities[f]
                      for f in ("term", "major", "difficulty_preference", "recommendation_goal")
                      if llm_entities.get(f)}
                if eu:
                    session = update_session(active_session_id, eu, user_id=user_id)

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

            # NOTE: the off_topic short-circuit that used to live here
            # was removed. It rendered a hardcoded English "I'm your
            # UCI course advisor..." template whenever classify_intent
            # tagged a message off_topic — but the classifier only
            # sees the latest message, so short replies like "继续",
            # "需要", "yes" routinely tripped it. Now every message
            # flows into the agent loop, which has the conversation
            # history (recent_turns) and can interpret continuations
            # in context. The agent's system prompt handles genuinely
            # off-topic questions (weather, food, etc.) by politely
            # redirecting in the user's language.

            # NOTE: legacy `needs_clarification` short-circuit lived here.
            # It returned a hardcoded English "I'd love to help! A few
            # quick questions first..." template — which (a) ignored the
            # student's language and (b) ignored that the term was
            # already selected in the dropdown. The agent loop below
            # handles underspecified queries correctly: it sees the
            # selected term in its system context, calls tools to fill
            # gaps, and asks its own clarifying questions in the user's
            # language if any are truly needed.

            state = get_known_fields(active_session_id, user_id=user_id)
            if req.term and req.term != state.get("term"):
                update_session(active_session_id, {"term": req.term}, user_id=user_id)
                state = get_known_fields(active_session_id, user_id=user_id)
                logger.info("[stream term-sync] %r written", req.term)

            # ── Stream the LLM answer through on_token ──
            async def on_token(text: str):
                await queue.put({"type": "token", "text": text})

            validation_dict = None
            # Agent loop first; on pre-flight failure (LLM unreachable
            # or yielded no events) fall back to the legacy intent-
            # specific handlers. Mid-flight errors stay visible to the
            # user — we can't cleanly fall back after streaming has
            # started without producing duplicate text.
            agent_result = await _handle_agent(
                req.message, state, memory_context,
                user_id=user_id,
                term=req.term or state.get("term"),
                system_prompt=req.system_prompt,
                queue=queue,
                recent_turns=recent_turns,
                decisions=decisions,
                summary=summary,
            )
            if agent_result is not None:
                reply, cards, followups, validation_dict = agent_result
            elif intent == "single_query":
                logger.info("[stream] agent fallback → _handle_single_query")
                reply, cards, followups = await _handle_single_query(
                    req.message, state, memory_context,
                    system_prompt=req.system_prompt,
                    on_token=on_token,
                    recent_turns=recent_turns,
                    decisions=decisions,
                    summary=summary,
                )
            else:
                logger.info("[stream] agent fallback → _handle_recommendation")
                reply, cards, followups, validation_dict = await _handle_recommendation(
                    req.message, state, memory_context,
                    session_id=active_session_id, term_str=req.term,
                    system_prompt=req.system_prompt,
                    on_token=on_token,
                    recent_turns=recent_turns,
                    decisions=decisions,
                    summary=summary,
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
                "session_state": state,
                "pending_schedule": session.get("pending_schedule", []),
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
        while True:
            event = await queue.get()
            if event is DONE:
                yield sse({"type": "done"})
                break
            yield sse(event)
    except asyncio.CancelledError:
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


# ── Continue endpoint (resume after limit_reached) ───────

class ContinueRequest(BaseModel):
    session_id: str = ""
    continuation_id: str


@router.post("/chat/continue")
async def chat_continue_endpoint(
    req: ContinueRequest,
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
    return StreamingResponse(
        _stream_continue(req, user["id"]),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_continue(req: ContinueRequest, user_id: str):
    import json as _json
    from app.agent.loop import resume_agent
    from app.llm.adapter import _get_client, LLM_MODEL, LLM_ENABLED

    def sse(event: dict) -> str:
        return f"data: {_json.dumps(event, ensure_ascii=False)}\n\n"

    if not LLM_ENABLED:
        yield sse({"type": "error", "message": "LLM not enabled"})
        yield sse({"type": "done"})
        return

    # user_id is resolved from the signed session cookie by the caller.
    accumulated = ""
    saw_limit_again = False
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
        raise
    except Exception as e:
        logger.exception("[continue] failed: %s", e)
        yield sse({"type": "error", "message": str(e)})
    finally:
        yield sse({"type": "done"})


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


@router.post("/schedule/add")
async def add_to_schedule(
    req: ScheduleRequest,
    user: dict = Depends(current_user_optional),
):
    """Phase E4: each (course_id, section) pair is a separate schedule
    entry. Adding Lec A and Dis A1 of the same course produces TWO
    entries (and downstream, two calendar events) so the user sees
    both blocks on the day grid.

    Stores the normalized section_num so subsequent compares hit the
    fast path instead of going through _resolve_section_num every
    time."""
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
    if not is_dup:
        session.setdefault("pending_schedule", []).append(entry)
        session = update_session(
            active_session_id,
            {"pending_schedule": session["pending_schedule"]},
            user_id=user_id,
        )
    events = _build_schedule_events(session, req.term)
    return {"ok": True, "pending_schedule": session["pending_schedule"], "events": events}


@router.post("/schedule/remove")
async def remove_from_schedule(
    req: ScheduleRequest,
    user: dict = Depends(current_user_optional),
):
    """If `section` is provided, remove only that specific (course, section).
    Section-equivalence aware so legacy entries (5-digit codes stored
    where section_num is now expected) get matched and removed.
    If omitted, remove every entry for the course."""
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
    return {"ok": True, "pending_schedule": session["pending_schedule"], "events": events}


class ScheduleClearRequest(BaseModel):
    session_id: str = ""
    term: Optional[str] = None


@router.post("/schedule/clear")
async def clear_schedule(
    req: ScheduleClearRequest,
    user: dict = Depends(current_user_optional),
):
    """Wipe every entry in this session's pending_schedule. Escape hatch
    when the user accumulates stuck entries (e.g. from legacy format
    that the section-equivalence fix can't auto-resolve)."""
    user_id = user["id"]
    active_session_id = _resolve_session_id(req.session_id, user_id, req.term)
    update_session(active_session_id, {"pending_schedule": []}, user_id=user_id)
    return {"ok": True, "pending_schedule": [], "events": []}


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


DAY_MAP = {"M": "Mon", "Tu": "Tue", "W": "Wed", "Th": "Thu", "F": "Fri"}

def _parse_days(s):
    result, i = [], 0
    while i < len(s):
        if i+1 < len(s) and s[i:i+2] in DAY_MAP:
            result.append(DAY_MAP[s[i:i+2]]); i += 2
        elif s[i] in DAY_MAP:
            result.append(DAY_MAP[s[i]]); i += 1
        else:
            i += 1
    return result

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

        for day in _parse_days(days_str):
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
