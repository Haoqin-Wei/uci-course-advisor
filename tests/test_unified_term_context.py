from __future__ import annotations

import asyncio
import json

from fastapi import BackgroundTasks

from app.agent import tools as agent_tools
from app.data import sessions as sessions_data
from app.routers import chat as chat_router
from app.routers.chat import ChatRequest
from app.terms.store import JsonFileTermStateStore


async def _collect_events(request: ChatRequest) -> list[dict]:
    events: list[dict] = []
    async for raw in chat_router._stream_chat(
        request,
        BackgroundTasks(),
        user_id="demo_001",
    ):
        events.append(json.loads(raw.removeprefix("data: ").strip()))
    return events


def _meta(events: list[dict]) -> dict:
    return next(event for event in events if event["type"] == "meta")


def _seed_available_terms(runtime_paths, *terms: str) -> None:
    store = JsonFileTermStateStore(runtime_paths.term_state)
    state = store.load()
    assert state is not None
    for term in terms:
        state.availability[term] = {
            "available": True,
            "course_count": 2,
            "section_count": 3,
        }
        if term not in state.websoc_terms:
            state.websoc_terms.append(term)
    store.save(state)


def test_chat_ignores_frontend_term_and_uses_backend_effective_term(monkeypatch):
    captured: dict = {}

    async def fake_handle(_message, state, _memory, *, queue, term, **_kwargs):
        captured["state"] = dict(state)
        captured["term"] = term
        await queue.put({"type": "token", "text": "grounded reply"})
        return "grounded reply", [], [], None

    monkeypatch.setattr(chat_router, "_handle_agent", fake_handle)
    events = asyncio.run(
        _collect_events(
            ChatRequest(
                message="recommend one course",
                session_id="",
                term="2099 Fall",
            )
        )
    )
    meta = _meta(events)

    assert captured["term"] == "2025 Spring"
    assert captured["state"]["term"] == "2025 Spring"
    assert meta["effective_term"] == "2025 Spring"
    persisted = sessions_data.get_session_meta("demo_001", meta["session_id"])
    assert persisted["term_mode"] == "auto"
    assert "term_mode" not in meta
    assert "term_update" not in meta


def test_successful_single_available_term_pins_conversation(
    runtime_paths,
    monkeypatch,
):
    _seed_available_terms(runtime_paths, "2026 Fall")

    async def fake_handle(_message, _state, _memory, *, queue, **_kwargs):
        await queue.put({"type": "token", "text": "fall answer"})
        return "fall answer", [], [], None

    monkeypatch.setattr(chat_router, "_handle_agent", fake_handle)
    meta = _meta(
        asyncio.run(
            _collect_events(
                ChatRequest(message="show 2026 Fall courses", session_id="")
            )
        )
    )
    persisted = sessions_data.get_session_meta("demo_001", meta["session_id"])

    assert meta["effective_term"] == "2026 Fall"
    assert "term_mode" not in meta
    assert "term_update" not in meta
    assert persisted["term_scope"] == "2026 Fall"
    assert persisted["term_mode"] == "pinned"


def test_failed_turn_does_not_pin_explicit_term(runtime_paths, monkeypatch):
    _seed_available_terms(runtime_paths, "2026 Fall")

    async def fake_handle(
        _message,
        _state,
        _memory,
        *,
        queue,
        execution_meta,
        **_kwargs,
    ):
        execution_meta["error"] = True
        await queue.put({"type": "token", "text": "partial answer"})
        return "partial answer", [], [], None

    monkeypatch.setattr(chat_router, "_handle_agent", fake_handle)
    meta = _meta(
        asyncio.run(
            _collect_events(
                ChatRequest(message="show 2026 Fall courses", session_id="")
            )
        )
    )

    assert meta["effective_term"] == "2025 Spring"
    persisted = sessions_data.get_session_meta("demo_001", meta["session_id"])
    assert persisted["term_mode"] == "auto"


def test_unknown_term_can_pin_only_after_tool_finds_term_data(monkeypatch):
    async def fake_handle(
        _message,
        _state,
        _memory,
        *,
        queue,
        execution_meta,
        **_kwargs,
    ):
        execution_meta["successful_tools"] = ["get_sections"]
        execution_meta["successful_tool_calls"] = [
            {
                "name": "get_sections",
                "args": {"course_id": "COMPSCI 161", "term": "2026 Fall"},
                "term_data_available": True,
            }
        ]
        await queue.put({"type": "token", "text": "verified answer"})
        return "verified answer", [], [], None

    monkeypatch.setattr(chat_router, "_handle_agent", fake_handle)
    meta = _meta(
        asyncio.run(
            _collect_events(
                ChatRequest(message="is COMPSCI 161 offered in 2026 Fall?", session_id="")
            )
        )
    )

    assert meta["effective_term"] == "2026 Fall"
    persisted = sessions_data.get_session_meta("demo_001", meta["session_id"])
    assert persisted["term_mode"] == "pinned"
    assert persisted["term_scope"] == "2026 Fall"
    assert "query_terms" not in meta


def test_known_unavailable_term_is_rejected_without_running_agent(
    runtime_paths,
    monkeypatch,
):
    store = JsonFileTermStateStore(runtime_paths.term_state)
    state = store.load()
    assert state is not None
    state.availability["2027 Winter"] = {
        "available": False,
        "course_count": 0,
        "section_count": 0,
    }
    store.save(state)

    async def unexpected_agent(*_args, **_kwargs):
        raise AssertionError("known unavailable term must not reach the agent")

    monkeypatch.setattr(chat_router, "_handle_agent", unexpected_agent)
    events = asyncio.run(
        _collect_events(
            ChatRequest(message="show courses in 2027 Winter", session_id="")
        )
    )
    meta = _meta(events)

    assert "has not been published" in events[0]["text"]
    assert meta["effective_term"] == "2025 Spring"
    persisted = sessions_data.get_session_meta("demo_001", meta["session_id"])
    assert persisted["term_mode"] == "auto"


def test_multi_term_query_does_not_change_conversation_term(runtime_paths, monkeypatch):
    _seed_available_terms(runtime_paths, "2026 Winter", "2026 Fall")
    captured = {}

    async def fake_handle(_message, state, _memory, *, queue, **_kwargs):
        captured["query_terms"] = state["query_terms"]
        await queue.put({"type": "token", "text": "comparison"})
        return "comparison", [], [], None

    monkeypatch.setattr(chat_router, "_handle_agent", fake_handle)
    meta = _meta(
        asyncio.run(
            _collect_events(
                ChatRequest(
                    message="compare 2026 Winter with 2026 Fall",
                    session_id="",
                )
            )
        )
    )

    assert captured["query_terms"] == ["2026 Winter", "2026 Fall"]
    assert "query_terms" not in meta
    assert meta["effective_term"] == "2025 Spring"
    persisted = sessions_data.get_session_meta("demo_001", meta["session_id"])
    assert persisted["term_mode"] == "auto"


def test_invalid_ambiguous_term_does_not_silently_use_default(monkeypatch):
    async def unexpected_agent(*_args, **_kwargs):
        raise AssertionError("ambiguous term must not reach the agent")

    monkeypatch.setattr(chat_router, "_handle_agent", unexpected_agent)
    events = asyncio.run(
        _collect_events(ChatRequest(message="show Fall courses", session_id=""))
    )

    assert "couldn't map that term unambiguously" in events[0]["text"]
    assert _meta(events)["effective_term"] == "2025 Spring"


def test_tool_arguments_use_canonical_effective_term():
    resolved, error = agent_tools.resolve_tool_arguments(
        "get_sections",
        {"course_id": "COMPSCI 161"},
        context={"term": "Fall 2026"},
    )
    assert error is None
    assert resolved["term"] == "2026 Fall"

    _, error = agent_tools.resolve_tool_arguments(
        "get_sections",
        {"course_id": "COMPSCI 161", "term": "Fall"},
        context={"term": "2025 Spring"},
    )
    assert "four-digit year" in str(error)


def test_validation_preserves_all_successful_tool_terms():
    meta = {
        "successful_tool_calls": [
            {"name": "get_sections", "args": {"term": "Fall 2026"}},
            {"name": "get_sections", "args": {"term": "2026 Winter"}},
            {"name": "get_sections", "args": {"term": "2026 Fall"}},
        ]
    }

    assert chat_router._validation_terms_from_agent_meta(meta) == [
        "2026 Fall",
        "2026 Winter",
    ]
