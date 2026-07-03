from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import BackgroundTasks

from app.routers import chat as chat_router
from app.routers.chat import ChatRequest


async def _collect_stream_events(req: ChatRequest, user_id: str) -> list[dict]:
    events: list[dict] = []
    async for raw in chat_router._stream_chat(
        req,
        BackgroundTasks(),
        user_id=user_id,
    ):
        assert raw.startswith("data: ")
        payload = raw.removeprefix("data: ").strip()
        events.append(json.loads(payload))
    return events


def _meta_event(events: list[dict]) -> dict:
    return next(event for event in events if event["type"] == "meta")


@pytest.fixture
def captured_stream_agent_context(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_extract_info(_message: str) -> dict:
        return {}

    async def fake_classify_intent(_message: str) -> dict:
        return {
            "intent": "course_recommendation",
            "confidence": 1.0,
            "entities": {},
            "source": "test",
        }

    async def fake_handle_agent(
        user_message,
        state,
        memory_context,
        *,
        queue,
        **kwargs,
    ):
        captured["user_message"] = user_message
        captured["state"] = dict(state)
        captured["memory_context"] = dict(memory_context)
        captured["kwargs"] = kwargs

        reply = "offline profile context reply"
        await queue.put({"type": "token", "text": reply})
        return reply, [], [], None

    monkeypatch.setattr(
        chat_router,
        "extract_info_from_message",
        fake_extract_info,
    )
    monkeypatch.setattr(chat_router, "classify_intent", fake_classify_intent)
    monkeypatch.setattr(chat_router, "_handle_agent", fake_handle_agent)

    return captured


def test_seeded_profile_can_build_memory_prompt_block(seeded_user):
    from app.memory.manager import get_memory_manager

    manager = get_memory_manager()
    manager.initialize_session(seeded_user.session_id, seeded_user.user_id)

    block = manager.system_prompt_block(seeded_user.user_id)

    assert "PERSISTENT STUDENT PROFILE:" in block
    assert "major: Computer Science" in block
    assert "completed_courses: ['ACENG20A']" in block
    assert "selected_courses: ['ICS33', 'STATS67']" in block
    assert "Prefers morning classes" in block


def test_stream_chat_passes_seeded_profile_in_memory_context(
    seeded_user,
    captured_stream_agent_context,
):
    events = asyncio.run(
        _collect_stream_events(
            ChatRequest(
                message="recommend morning classes using my saved profile",
                session_id=seeded_user.session_id,
                term="Spring 2025",
            ),
            user_id=seeded_user.user_id,
        )
    )

    meta = _meta_event(events)
    memory_context = captured_stream_agent_context["memory_context"]
    state = captured_stream_agent_context["state"]

    assert meta["session_id"] == seeded_user.session_id
    assert meta["session_state"]["term"] == "Spring 2025"

    prompt_block = memory_context["system_prompt_block"]
    assert "PERSISTENT STUDENT PROFILE:" in prompt_block
    assert "major: Computer Science" in prompt_block
    assert "completed_courses: ['ACENG20A']" in prompt_block
    assert "selected_courses: ['ICS33', 'STATS67']" in prompt_block
    assert "Prefers morning classes" in prompt_block

    # Characterization of the current M1 behavior: profile reaches the
    # agent as memory prompt context, but not as structured session state.
    assert state["major"] is None
    assert state["completed_courses"] == []
    assert state["selected_courses"] == []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "M2.3 should unwrap the get_student_profile envelope so durable "
        "profile fields also hydrate structured chat state."
    ),
)
def test_stream_chat_should_hydrate_agent_state_from_seeded_profile(
    seeded_user,
    captured_stream_agent_context,
):
    asyncio.run(
        _collect_stream_events(
            ChatRequest(
                message="recommend classes using my saved profile",
                session_id=seeded_user.session_id,
                term="Spring 2025",
            ),
            user_id=seeded_user.user_id,
        )
    )

    state = captured_stream_agent_context["state"]

    assert state["major"] == "Computer Science"
    assert state["completed_courses"] == ["ACENG20A"]
    assert state["selected_courses"] == ["ICS33", "STATS67"]
