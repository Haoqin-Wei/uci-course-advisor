from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import BackgroundTasks

from app.data import sessions as sessions_data
from app.routers import chat as chat_router
from app.routers.chat import ChatRequest


RECOMMENDATION_CARDS = [
    {
        "course_id": "COMPSCI161",
        "title": "Design and Analysis of Algorithms",
        "units": "4",
        "reason": "Useful upper-division CS requirement.",
        "primary_code": "20000",
        "requires_secondary": True,
        "sections": [
            {
                "section_code": "20000",
                "section_type": "Lec",
                "section_num": "A",
                "status": "OPEN",
                "days": "TuTh",
                "time_display": "TuTh 10:00-11:20",
                "instructors": ["TESTER, A."],
            },
            {
                "section_code": "20001",
                "section_type": "Dis",
                "section_num": "A1",
                "status": "OPEN",
                "days": "F",
                "time_display": "F 09:00-09:50",
                "instructors": ["STAFF"],
            },
        ],
    },
    {
        "course_id": "IN4MATX43",
        "title": "Introduction to Software Engineering",
        "units": "4",
        "reason": "Good project-based complement.",
        "primary_code": "30000",
        "requires_secondary": False,
        "sections": [
            {
                "section_code": "30000",
                "section_type": "Lec",
                "section_num": "A",
                "status": "OPEN",
                "days": "MWF",
                "time_display": "MWF 13:00-13:50",
                "instructors": ["BUILDER, B."],
            },
        ],
    },
]

FOLLOWUPS = [
    "Check if these conflict with my current schedule",
    "Rank these by easiest grading",
]

VALIDATION_REPORT = {
    "overall": "ok",
    "issues": [],
    "checked_courses": ["COMPSCI161", "IN4MATX43"],
}


async def _collect_stream_events(req: ChatRequest, user_id: str) -> list[dict]:
    events: list[dict] = []
    async for raw in chat_router._stream_chat(
        req,
        BackgroundTasks(),
        user_id=user_id,
    ):
        assert raw.startswith("data: ")
        events.append(json.loads(raw.removeprefix("data: ").strip()))
    return events


def _meta_event(events: list[dict]) -> dict:
    return next(event for event in events if event["type"] == "meta")


@pytest.fixture
def recommendation_cards_pipeline(monkeypatch):
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
        _user_message,
        _state,
        _memory_context,
        *,
        queue,
        **_kwargs,
    ):
        reply = "Here are two offline recommendation cards."
        await queue.put({"type": "token", "text": reply})
        return reply, RECOMMENDATION_CARDS, FOLLOWUPS, VALIDATION_REPORT

    monkeypatch.setattr(chat_router, "extract_info_from_message", fake_extract_info)
    monkeypatch.setattr(chat_router, "classify_intent", fake_classify_intent)
    monkeypatch.setattr(chat_router, "_handle_agent", fake_handle_agent)


def test_recommendation_cards_in_sse_meta_are_persisted_and_restored(
    app_client,
    recommendation_cards_pipeline,
):
    session_id = sessions_data.create_session(
        "demo_001",
        title="Card persistence fixture",
        term_scope="Spring 2025",
    )

    events = asyncio.run(
        _collect_stream_events(
            ChatRequest(
                message="recommend two classes with cards",
                session_id=session_id,
                term="Spring 2025",
            ),
            user_id="demo_001",
        )
    )
    meta = _meta_event(events)

    assert [event["type"] for event in events] == ["token", "meta", "done"]
    assert events[0]["text"] == "Here are two offline recommendation cards."
    assert meta["session_id"] == session_id
    assert meta["cards"] == RECOMMENDATION_CARDS
    assert meta["followups"] == FOLLOWUPS
    assert meta["validation_report"] == VALIDATION_REPORT

    turns = sessions_data.read_turns("demo_001", session_id)

    assert [turn["role"] for turn in turns] == ["user", "assistant"]
    assert "cards" not in turns[0]
    assert "followups" not in turns[0]
    assert "validation" not in turns[0]
    assert turns[1]["content"] == "Here are two offline recommendation cards."
    assert turns[1]["cards"] == RECOMMENDATION_CARDS
    assert turns[1]["followups"] == FOLLOWUPS
    assert turns[1]["validation"] == VALIDATION_REPORT

    response = app_client.get(f"/api/sessions/demo_001/{session_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == session_id
    assert payload["turn_count"] == 2
    assert payload["turns"][1]["cards"] == RECOMMENDATION_CARDS
    assert payload["turns"][1]["followups"] == FOLLOWUPS
    assert payload["turns"][1]["validation"] == VALIDATION_REPORT


def test_recommendation_cards_can_be_restored_with_since_turn(
    app_client,
    recommendation_cards_pipeline,
):
    session_id = sessions_data.create_session(
        "demo_001",
        title="Card since-turn fixture",
        term_scope="Spring 2025",
    )

    asyncio.run(
        _collect_stream_events(
            ChatRequest(
                message="recommend two classes with cards",
                session_id=session_id,
                term="Spring 2025",
            ),
            user_id="demo_001",
        )
    )

    response = app_client.get(
        f"/api/sessions/demo_001/{session_id}",
        params={"since_turn": 1},
    )

    assert response.status_code == 200
    payload = response.json()
    assert [turn["turn_index"] for turn in payload["turns"]] == [2]
    assert payload["turns"][0]["role"] == "assistant"
    assert payload["turns"][0]["cards"] == RECOMMENDATION_CARDS
