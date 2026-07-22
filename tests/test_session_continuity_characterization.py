from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import BackgroundTasks

from app.data import sessions as sessions_data
from app.modules import state as state_module
from app.routers import chat as chat_router
from app.routers.chat import ChatRequest


async def _collect_stream_events(req: ChatRequest) -> list[dict]:
    events: list[dict] = []
    async for raw in chat_router._stream_chat(
        req,
        BackgroundTasks(),
        user_id="demo_001",
    ):
        assert raw.startswith("data: ")
        payload = raw.removeprefix("data: ").strip()
        events.append(json.loads(payload))
    return events


def _meta_event(events: list[dict]) -> dict:
    return next(event for event in events if event["type"] == "meta")


@pytest.fixture
def deterministic_chat_pipeline(monkeypatch):
    async def fake_handle_agent(
        user_message,
        _state,
        _memory_context,
        *,
        queue,
        **_kwargs,
    ):
        reply = f"offline reply: {user_message}"
        await queue.put({"type": "token", "text": reply})
        return reply, [], [], None

    monkeypatch.setattr(chat_router, "_handle_agent", fake_handle_agent)


def test_new_empty_session_persists_turns_without_splitting_state_by_id(
    deterministic_chat_pipeline,
):
    first_events = asyncio.run(
        _collect_stream_events(
            ChatRequest(
                message="first turn: I am CS and taking ICS33, prefer easy classes",
                session_id="",
                term="Spring 2025",
            )
        )
    )
    first_meta = _meta_event(first_events)
    persistent_sid = first_meta["session_id"]

    assert persistent_sid.startswith("sess_")
    assert first_meta["session_state"]["term"] == "2025 Spring"
    assert first_meta["session_state"]["major"] == "Computer Science"
    assert first_meta["session_state"]["selected_courses"] == ["ICS33"]
    assert first_meta["session_state"]["difficulty_preference"] == "easy"
    assert not hasattr(state_module, "_sessions")
    persisted_state = sessions_data.get_session_state("demo_001", persistent_sid)
    assert persisted_state["selected_courses"] == ["ICS33"]
    assert persisted_state["difficulty_preference"] == "easy"
    assert [turn["role"] for turn in sessions_data.read_turns(
        "demo_001",
        persistent_sid,
    )] == ["user", "assistant"]

    second_events = asyncio.run(
        _collect_stream_events(
            ChatRequest(
                message="second turn: recommend one more non-conflicting course",
                session_id=persistent_sid,
                term="Spring 2025",
            )
        )
    )
    second_meta = _meta_event(second_events)

    assert second_meta["session_id"] == persistent_sid
    assert second_meta["session_state"]["term"] == "2025 Spring"
    assert second_meta["session_state"]["major"] == "Computer Science"
    assert second_meta["session_state"]["selected_courses"] == ["ICS33"]
    assert second_meta["session_state"]["difficulty_preference"] == "easy"
    persisted_state = sessions_data.get_session_state("demo_001", persistent_sid)
    assert persisted_state["selected_courses"] == ["ICS33"]
    assert persisted_state["difficulty_preference"] == "easy"
    assert [turn["role"] for turn in sessions_data.read_turns(
        "demo_001",
        persistent_sid,
    )] == ["user", "assistant", "user", "assistant"]


def test_new_session_second_turn_should_see_first_turn_state(
    deterministic_chat_pipeline,
):
    first_events = asyncio.run(
        _collect_stream_events(
            ChatRequest(
                message="first turn: I am CS and taking ICS33, prefer easy classes",
                session_id="",
                term="Spring 2025",
            )
        )
    )
    persistent_sid = _meta_event(first_events)["session_id"]

    second_events = asyncio.run(
        _collect_stream_events(
            ChatRequest(
                message="second turn: recommend one more non-conflicting course",
                session_id=persistent_sid,
                term="Spring 2025",
            )
        )
    )
    second_state = _meta_event(second_events)["session_state"]

    assert second_state["major"] == "Computer Science"
    assert second_state["selected_courses"] == ["ICS33"]
    assert second_state["difficulty_preference"] == "easy"


def test_legacy_non_persistent_session_id_is_rejected():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        chat_router._resolve_session_id(
            "demo_session",
            "demo_001",
            "Spring 2025",
        )

    assert exc_info.value.status_code == 400
