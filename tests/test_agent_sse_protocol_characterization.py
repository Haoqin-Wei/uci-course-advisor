from __future__ import annotations

import asyncio
import json

from fastapi import BackgroundTasks

from app.agent import loop as agent_loop
from app.data import sessions as sessions_data
from app.modules import state as state_module
from app.routers import chat as chat_router
from app.routers.chat import ChatRequest, ContinueRequest
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response


async def _queue_items(queue: asyncio.Queue) -> list[dict]:
    items: list[dict] = []
    while not queue.empty():
        items.append(await queue.get())
    return items


async def _collect_sse(async_lines) -> list[dict]:
    events: list[dict] = []
    async for raw in async_lines:
        assert raw.startswith("data: ")
        events.append(json.loads(raw.removeprefix("data: ").strip()))
    return events


async def _collect_agent_events(events) -> list[dict]:
    return [event async for event in events]


def test_handle_agent_forwards_tool_limit_and_merges_fallback_cards(monkeypatch):
    from app.llm import adapter

    async def fake_stream_agent_response(*_args, **_kwargs):
        yield {"type": "token", "text": "Looking. "}
        yield {
            "type": "tool_call_start",
            "name": "get_policy",
            "args": {"topic": "ge"},
            "label": "Check policy",
        }
        yield {
            "type": "tool_call_done",
            "name": "get_policy",
            "ok": True,
            "label": "Check policy",
        }
        yield {
            "type": "cards_proposed",
            "cards": [{"course_id": "COMPSCI161", "title": "Design"}],
        }
        yield {
            "type": "cards_proposed",
            "from_fallback": True,
            "cards": [
                {"course_id": "COMPSCI161", "title": "Duplicate"},
                {"course_id": "IN4MATX43", "title": "Software"},
            ],
        }
        yield {
            "type": "limit_reached",
            "reason": "max_iterations",
            "iterations": 6,
            "tool_calls": 16,
            "continuation_id": "continue-token",
        }
        yield {"type": "token", "text": "Final."}
        yield {
            "type": "final",
            "text": "Looking. Final.",
            "iterations": 6,
            "tool_calls": 16,
            "truncated": True,
        }

    monkeypatch.setattr(adapter, "stream_agent_response", fake_stream_agent_response)
    queue: asyncio.Queue = asyncio.Queue()

    result = asyncio.run(
        chat_router._handle_agent(
            "recommend classes",
            {"term": "Spring 2025"},
            {"system_prompt_block": "", "prefetched_context": ""},
            user_id="demo_001",
            term="Spring 2025",
            system_prompt=None,
            queue=queue,
        )
    )
    queued = asyncio.run(_queue_items(queue))

    assert result == (
        "Looking. Final.",
        [
            {"course_id": "COMPSCI161", "title": "Design"},
            {"course_id": "IN4MATX43", "title": "Software"},
        ],
        [],
        None,
    )
    assert [event["type"] for event in queued] == [
        "token",
        "tool_call_start",
        "tool_call_done",
        "limit_reached",
        "token",
    ]
    assert queued[1] == {
        "type": "tool_call_start",
        "name": "get_policy",
        "label": "Check policy",
        "args": {"topic": "ge"},
    }
    assert queued[2] == {
        "type": "tool_call_done",
        "name": "get_policy",
        "label": "Check policy",
        "ok": True,
    }
    assert queued[3] == {
        "type": "limit_reached",
        "reason": "max_iterations",
        "iterations": 6,
        "tool_calls": 16,
        "continuation_id": "continue-token",
    }


def test_handle_agent_preflight_error_returns_none_without_sse(monkeypatch):
    from app.llm import adapter

    async def fake_stream_agent_response(*_args, **_kwargs):
        yield {"type": "error", "message": "LLM call failed: offline"}

    monkeypatch.setattr(adapter, "stream_agent_response", fake_stream_agent_response)
    queue: asyncio.Queue = asyncio.Queue()

    result = asyncio.run(
        chat_router._handle_agent(
            "recommend classes",
            {"term": "Spring 2025"},
            {"system_prompt_block": "", "prefetched_context": ""},
            user_id="demo_001",
            term="Spring 2025",
            system_prompt=None,
            queue=queue,
        )
    )

    assert result is None
    assert queue.empty()


def test_handle_agent_midflight_error_is_forwarded_without_fallback(monkeypatch):
    from app.llm import adapter

    async def fake_stream_agent_response(*_args, **_kwargs):
        yield {"type": "token", "text": "Partial answer."}
        yield {"type": "error", "message": "tool crashed"}

    monkeypatch.setattr(adapter, "stream_agent_response", fake_stream_agent_response)
    queue: asyncio.Queue = asyncio.Queue()

    result = asyncio.run(
        chat_router._handle_agent(
            "recommend classes",
            {"term": "Spring 2025"},
            {"system_prompt_block": "", "prefetched_context": ""},
            user_id="demo_001",
            term="Spring 2025",
            system_prompt=None,
            queue=queue,
        )
    )
    queued = asyncio.run(_queue_items(queue))

    assert result == ("Partial answer.", [], [], None)
    assert queued == [
        {"type": "token", "text": "Partial answer."},
        {"type": "error", "message": "tool crashed"},
    ]


def test_stream_chat_falls_back_when_agent_errors_before_streaming(monkeypatch):
    from app.llm import adapter

    async def fake_extract_info(_message: str) -> dict:
        return {}

    async def fake_classify_intent(_message: str) -> dict:
        return {
            "intent": "course_recommendation",
            "confidence": 1.0,
            "entities": {},
            "source": "test",
        }

    async def fake_stream_agent_response(*_args, **_kwargs):
        yield {"type": "error", "message": "LLM call failed: offline"}

    async def fake_handle_recommendation(
        _message,
        _state,
        _memory_context,
        *,
        on_token,
        **_kwargs,
    ):
        reply = "legacy fallback reply"
        await on_token(reply)
        return reply, [], [], None

    monkeypatch.setattr(chat_router, "extract_info_from_message", fake_extract_info)
    monkeypatch.setattr(chat_router, "classify_intent", fake_classify_intent)
    monkeypatch.setattr(adapter, "stream_agent_response", fake_stream_agent_response)
    monkeypatch.setattr(chat_router, "_handle_recommendation", fake_handle_recommendation)

    events = asyncio.run(
        _collect_sse(
            chat_router._stream_chat(
                ChatRequest(
                    message="recommend classes",
                    session_id="",
                    term="Spring 2025",
                ),
                BackgroundTasks(),
                user_id="demo_001",
            )
        )
    )

    assert [event["type"] for event in events] == ["token", "meta", "done"]
    assert events[0]["text"] == "legacy fallback reply"
    assert events[1]["session_state"]["term"] == "Spring 2025"
    assert "error" not in {event["type"] for event in events}


def test_stream_continue_resumes_once_and_persists_assistant_text(monkeypatch):
    from app.llm import adapter

    client = ScriptedLLMClient(
        tool_response(tool_call("get_policy", {}, call_id="call_policy")),
        text_response("Best effort."),
        text_response("Resumed answer."),
    )
    monkeypatch.setattr(agent_loop, "MAX_ITERATIONS", 1)

    initial_events = asyncio.run(
        _collect_agent_events(
            agent_loop.run_agent(
                [{"role": "user", "content": "Help me"}],
                client=client,
                model="fake-model",
                user_id="demo_001",
                term="Spring 2025",
            )
        )
    )
    continuation_id = next(
        event["continuation_id"]
        for event in initial_events
        if event["type"] == "limit_reached"
    )

    monkeypatch.setattr(adapter, "LLM_ENABLED", True)
    monkeypatch.setattr(adapter, "_get_client", lambda: client)
    monkeypatch.setattr(adapter, "LLM_MODEL", "fake-model")
    session_id = sessions_data.create_session(
        "demo_001",
        title="Continue fixture",
        term_scope="Spring 2025",
    )

    resumed_events = asyncio.run(
        _collect_sse(
            chat_router._stream_continue(
                ContinueRequest(
                    session_id=session_id,
                    continuation_id=continuation_id,
                ),
                user_id="demo_001",
            )
        )
    )

    assert [event["type"] for event in resumed_events] == ["token", "final", "done"]
    assert resumed_events[0]["text"] == "Resumed answer."
    assert resumed_events[1]["text"] == "Resumed answer."
    assert not hasattr(state_module, "_sessions")
    assert [turn["role"] for turn in sessions_data.read_turns("demo_001", session_id)] == [
        "assistant"
    ]
    assert sessions_data.read_turns("demo_001", session_id)[0]["content"] == (
        "Resumed answer."
    )

    replay_events = asyncio.run(
        _collect_sse(
            chat_router._stream_continue(
                ContinueRequest(
                    session_id=session_id,
                    continuation_id=continuation_id,
                ),
                user_id="demo_001",
            )
        )
    )

    assert replay_events == [
        {
            "type": "error",
            "message": "continuation_id not found or expired",
        },
        {"type": "done"},
    ]
    client.assert_exhausted()
