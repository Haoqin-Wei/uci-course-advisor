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
    do ONE more LLM call with tools disabled so the user still gets
    a best-effort answer instead of a blank screen.

Cancellation:
    asyncio.CancelledError propagates out unchanged — the user hit
    Stop, the SSE consumer cancelled the producer, we cancel the
    LLM call. Same model as stream_answer_llm.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from typing import AsyncIterator, Optional

from app import observability
from app.agent import tools as agent_tools
from app.agent.deep_search_history import (
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

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 12
# Bumped 12 → 16 after Phase E1 landed. A typical recommendation flow
# is 1 enumeration call (search_courses) + propose_recommendation +
# ~3 enrichment calls per recommended course (grades / prereq /
# professor) → ~13-15 tools. 12 was too tight; the LLM would burn the
# budget on enrichment and never reach propose_recommendation.
MAX_TOTAL_TOOLS = 16

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
    """Summarize only URLs that the tool actually fetched, not search hits."""
    if not isinstance(result, dict):
        return None
    fetched_urls: list[str] = []
    failure_count = 0
    if tool_name == "get_department_restrictions":
        if result.get("source_url"):
            fetched_urls.append(result["source_url"])
        linked = result.get("linked_pages")
        if isinstance(linked, dict):
            fetched_urls.extend(
                page["url"]
                for page in (linked.get("pages") or [])
                if isinstance(page, dict) and page.get("url")
            )
            failure_count = len(linked.get("errors") or [])
    elif tool_name == "fetch_page":
        fetched = result.get("final_url") or result.get("source_url")
        if fetched:
            fetched_urls.append(fetched)
        failure_count = 0 if result.get("ok") else 1
    else:
        return None

    fetched_urls = list(dict.fromkeys(fetched_urls))
    return {
        "fetched_urls": fetched_urls,
        "success_count": len(fetched_urls),
        "failure_count": failure_count,
    }


def _result_has_term_data(result: dict) -> bool:
    """Whether a term-scoped tool found at least one course with a section."""
    if not isinstance(result, dict) or result.get("found") is False:
        return False
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
    return {
        "role": "system",
        "content": (
            "The server already executed the developer-owned primary workflow before "
            "this model call. Do not call these primary tools again. Use the results "
            "below as the primary evidence, report structured failures honestly, and "
            "only use web_search/fetch_page for optional supplemental evidence.\n"
            f"Workflow route: {json.dumps(route, ensure_ascii=False, default=str)}\n"
            f"Primary workflow results: {json.dumps(results, ensure_ascii=False, default=str)}"
        ),
    }


def _clarification_text(user_query: str, clarification: dict) -> str:
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", user_query or ""))
    key = "message_zh" if has_cjk else "message_en"
    return str(clarification.get(key) or clarification.get("message_en") or "")


def _stash_continuation(
    messages: list,
    *,
    user_id: str,
    term: Optional[str],
    pending_schedule: Optional[list[dict]],
    iterations_used: int,
    tool_calls_used: int,
    deep_search_state: DeepSearchRunState,
    history_hint_message: Optional[dict[str, str]],
) -> str:
    _gc_continuations()
    cid = secrets.token_urlsafe(16)
    _continuation_store[cid] = {
        "messages": list(messages),  # shallow copy — entries are dicts we won't mutate
        "user_id":  user_id,
        "term":     term,
        "pending_schedule": list(pending_schedule or []),
        "iterations_used":  iterations_used,
        "tool_calls_used":  tool_calls_used,
        "deep_search_state": deep_search_state,
        "history_hint_message": history_hint_message,
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
    pending_schedule: Optional[list[dict]] = None,
) -> AsyncIterator[dict]:
    """
    Run the agent loop on a prebuilt messages list. `messages` is
    mutated in place (assistant + tool messages are appended each
    round) so the caller can inspect the full trace.

    `term` is the student's currently-selected term (frontend
    drop-down). It's stored on the tool context so dispatchers can
    inject it as a default when the model forgets to pass `term=...`.
    """
    user_query = _latest_user_content(messages)
    deep_search_state = DeepSearchRunState(query=user_query)
    matches, history_hint_message = load_history_hint(user_query)
    deep_search_state.history_matches = matches
    async for event in _run_loop(
        messages, client=client, model=model,
        user_id=user_id, term=term,
        pending_schedule=pending_schedule,
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
            "fill."
        ),
    }]
    logger.info("[agent] resuming continuation %s (was %d iters / %d tools)",
                continuation_id, snap["iterations_used"], snap["tool_calls_used"])

    async for event in _run_loop(
        messages, client=client, model=model,
        user_id=snap["user_id"], term=snap["term"],
        pending_schedule=snap.get("pending_schedule") or [],
        start_iteration=0, start_tool_count=0,
        user_query=snap["deep_search_state"].query,
        deep_search_state=snap["deep_search_state"],
        history_hint_message=snap.get("history_hint_message"),
    ):
        yield event


async def _run_loop(
    messages: list[dict],
    *,
    client,
    model: str,
    user_id: str,
    term: Optional[str],
    pending_schedule: Optional[list[dict]],
    start_iteration: int,
    start_tool_count: int,
    user_query: str,
    deep_search_state: DeepSearchRunState,
    history_hint_message: Optional[dict[str, str]],
) -> AsyncIterator[dict]:
    """The actual iteration body, shared by run_agent and resume_agent."""
    tool_context = {
        "user_id": user_id,
        "term": term,
        "pending_schedule": list(pending_schedule or []),
        "user_query": user_query,
        "deep_search_state": deep_search_state,
    }
    workflow_route = route_solution(user_query, term=term)
    primary_plan = build_primary_workflow_plan(workflow_route)
    clarification = primary_plan.get("clarification")
    if clarification:
        clarification_text = _clarification_text(user_query, clarification)
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
    primary_result_records: list[dict] = []
    for primary_call in primary_plan.get("calls") or []:
        tool_name = primary_call["tool"]
        args, term_error = agent_tools.resolve_tool_arguments(
            tool_name,
            primary_call["args"],
            context=tool_context,
        )
        label = agent_tools.humanize_tool_call(tool_name, args)
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
        }
        result = (
            {"error": f"invalid term for {tool_name}: {term_error}"}
            if term_error
            else agent_tools.dispatch(tool_name, args, context=tool_context)
        )
        if asyncio.iscoroutine(result):
            result = await result
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
        yield {
            "type": "tool_call_done",
            "name": tool_name,
            "ok": tool_ok,
            "label": label,
            "args": args,
            "term_data_available": _result_has_term_data(result),
            "server_forced": True,
        }

    tool_context["_forced_workflow_results"] = forced_results
    route_hint_message = build_route_hint_message(workflow_route)
    workflow_result_message = _workflow_result_message(
        workflow_route,
        primary_result_records,
    )
    hint_messages = [
        hint
        for hint in (
            route_hint_message,
            workflow_result_message,
            history_hint_message,
        )
        if hint is not None
    ]
    observability.increment("solution_router.routes", route=workflow_route["route_type"])
    if workflow_route["route_type"] == "workflow":
        for intent in workflow_route.get("intents", []):
            observability.increment("workflow_router.matches", intent=intent)
    for iteration in range(start_iteration, MAX_ITERATIONS):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=_messages_with_run_hints(messages, hint_messages),
                tools=agent_tools.TOOL_SCHEMAS,
                tool_choice="auto",
                stream=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[agent] LLM call failed (iter %d): %s: %s",
                         iteration, type(e).__name__, e)
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

        # ── If no tool calls came back, this iteration's content IS
        #    the final answer. We've already streamed the tokens; emit
        #    the "final" event with the full text for persistence.
        if not tool_calls_acc:
            final_text = accumulated_content
            verification_missing = history_refresh_missing(deep_search_state)
            if verification_missing:
                final_text = verification_required_text(user_query)
            trace_result = record_run_trace(deep_search_state, final_text)
            final_event = {
                "type": "final",
                "text": final_text,
                "iterations": iteration + 1,
                "tool_calls": total_tool_calls,
            }
            if verification_missing:
                final_event["verification_required"] = True
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
                    pending_schedule=pending_schedule,
                    deep_search_state=deep_search_state,
                    history_hint_message=history_hint_message,
                    client=client, model=model,
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

            label = agent_tools.humanize_tool_call(tc["name"], args)
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
                   "name": tc["name"], "args": args, "label": label}

            forced_cache = tool_context.get("_forced_workflow_results") or {}
            if term_error:
                result = {"error": f"invalid term for {tc['name']}: {term_error}"}
            elif tc["name"] in forced_cache:
                result = forced_cache[tc["name"]]
                observability.log_event(
                    logger,
                    logging.INFO,
                    "workflow_primary_tool_reused",
                    tool=tc["name"],
                    label=label,
                    server_forced=True,
                )
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
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(result, default=str),
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
            yield {"type": "tool_call_done",
                   "name": tc["name"],
                   "ok": tool_ok,
                   "label": label,
                   "args": args,
                   "term_data_available": _result_has_term_data(result)}

            # Side-channel: propose_recommendation stages structured
            # cards on the tool_context dict (the LLM-visible return is
            # a short ack). Pop them here and emit a cards_proposed
            # event so the SSE consumer can ship them in the meta
            # event without round-tripping kilobytes through the
            # model's context window.
            staged = tool_context.pop("_proposed_cards", None)
            if staged:
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
        pending_schedule=pending_schedule,
        deep_search_state=deep_search_state,
        history_hint_message=history_hint_message,
        client=client, model=model,
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
    pending_schedule: Optional[list[dict]],
    deep_search_state: DeepSearchRunState,
    history_hint_message: Optional[dict[str, str]],
    client,
    model: str,
) -> AsyncIterator[dict]:
    """
    Common tail behavior when a budget limit trips:
      1. Stash `messages` and emit limit_reached with the continuation_id
      2. Run one more LLM call WITHOUT tools so the user gets a
         best-effort answer using whatever was gathered (the fallback)
      3. Emit a final event marked truncated=True

    Cancellation (user hits Stop while the fallback is streaming)
    propagates unchanged.
    """
    cid = _stash_continuation(
        messages,
        user_id=user_id, term=term,
        pending_schedule=pending_schedule,
        iterations_used=iterations_used,
        tool_calls_used=tool_calls_used,
        deep_search_state=deep_search_state,
        history_hint_message=history_hint_message,
    )
    yield {
        "type": "limit_reached",
        "reason": reason,
        "iterations": iterations_used,
        "tool_calls": tool_calls_used,
        "continuation_id": cid,
    }

    # Build a fallback prompt that nudges the model to wrap up with
    # what it has. We DON'T mutate `messages` (it's been stashed) —
    # we build a throwaway list for this one call.
    fallback_messages = list(messages) + [{
        "role": "user",
        "content": (
            "[System notice: the tool-call budget for this turn has "
            "been reached. Write your best final answer NOW using only "
            "the information already gathered above. "
            "ONE exception: if this is a course-recommendation turn and "
            "you have not yet called `propose_recommendation`, you may "
            "(and SHOULD) call it once now to stage the card list — "
            "all other tools are disabled. Otherwise, write a clean "
            "prose answer. Do NOT emit XML, DSML, `<invoke>`, or any "
            "raw tool-call markup — you have at most ONE legitimate "
            "tool available, use it via the normal tool_calls channel."
        ),
    }]

    # Allow ONE last propose_recommendation call so the LLM has a way
    # to stage cards even on the fallback path. Everything else stays
    # disabled — otherwise the loop could spin forever.
    fallback_tools = [
        s for s in agent_tools.TOOL_SCHEMAS
        if s["function"]["name"] == "propose_recommendation"
    ]
    tool_context: dict = {
        "user_id": user_id,
        "term": term,
        "pending_schedule": list(pending_schedule or []),
    }

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=fallback_messages,
            tools=fallback_tools,
            tool_choice="auto",
            stream=True,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("[agent] fallback LLM call failed: %s: %s",
                     type(e).__name__, e)
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

    # Dispatch any propose_recommendation the LLM staged in the
    # fallback. We don't loop again — this is the last call. No
    # tool_call_start/done chips: by this point the message body has
    # already streamed, and a trailing "staging cards…" chip would
    # render out of order. Cards arrive silently via cards_proposed.
    for tc in fallback_tool_calls.values():
        if tc["name"] != "propose_recommendation":
            continue
        try:
            args = json.loads(tc["arguments"] or "{}")
        except json.JSONDecodeError as e:
            logger.warning("[agent fallback] bad propose_recommendation args: %s", e)
            continue
        logger.info("[agent fallback] late propose_recommendation: items=%d term=%r",
                    len(args.get("items") or []), args.get("term"))
        result = agent_tools.dispatch(tc["name"], args, context=tool_context)
        if "error" in result:
            logger.warning("[agent fallback] propose_recommendation dispatch failed: %s",
                           result.get("error"))
            continue
        staged = tool_context.pop("_proposed_cards", None)
        if staged:
            logger.info("[agent fallback] staged %d cards", len(staged))
            # from_fallback=True signals to the caller that this batch
            # was synthesized late under truncation pressure; the LLM
            # may only have full data for a subset of the original
            # picks. The caller should MERGE these into any earlier
            # batch rather than overwrite — otherwise we silently
            # shrink the user's recommendation.
            yield {"type": "cards_proposed", "cards": staged, "from_fallback": True}

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
    trace_result = record_run_trace(deep_search_state, cleaned)
    final_event = {
        "type": "final",
        "text": cleaned,
        "iterations": iterations_used,
        "tool_calls": tool_calls_used,
        "truncated": True,
    }
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
