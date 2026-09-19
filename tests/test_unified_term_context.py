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


def test_week9_chat_keeps_future_focus_until_user_explicitly_asks_current(monkeypatch):
    from datetime import datetime
    from app.terms import LOS_ANGELES
    from tests.test_automatic_term_planning import calendar_service

    service = calendar_service(datetime(2026, 11, 20, tzinfo=LOS_ANGELES))
    monkeypatch.setattr(chat_router, "get_term_resolution_service", lambda: service)
    sid = sessions_data.create_session("demo_001", default_term="2025 Winter")
    sessions_data.update_session_meta("demo_001", sid, term_mode="manual")
    captured = []

    async def answer(message, state, memory, *, queue, **kwargs):
        captured.append(dict(state))
        text = "本次查询 " + ", ".join(state["query_terms"])
        await queue.put({"type": "token", "text": text})
        return text, [], [], None

    monkeypatch.setattr(chat_router, "_handle_agent", answer)
    for question in ("Winter 的 ICS 33 谁教", "那 ICS 32 呢", "本学期 ICS 32 谁教"):
        events = asyncio.run(_collect_events(ChatRequest(message=question, session_id=sid)))
        assert _meta(events)["default_term"] == "2027 Winter"
        assert _meta(events)["current_term"] == "2026 Fall"
    assert [s["query_terms"] for s in captured] == [["2027 Winter"], ["2027 Winter"], ["2026 Fall"]]
    assert captured[0]["inferred_year"] is True
    assert captured[1]["query_course_ids"] == ["ICS32"]
    meta = sessions_data.get_session_meta("demo_001", sid)
    assert meta["term_mode"] == "auto"
    assert meta["default_term"] == "2027 Winter"
    assert meta["recent_query_focus"]["terms"] == ["2026 Fall"]


def test_chat_ignores_frontend_term_and_uses_backend_default_term(monkeypatch):
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
    assert meta["default_term"] == "2025 Spring"
    assert meta["query_terms"] == ["2025 Spring"]
    assert meta["default_term_changed"] is False
    persisted = sessions_data.get_session_meta("demo_001", meta["session_id"])
    assert persisted["term_mode"] == "auto"
    assert persisted["default_term"] == "2025 Spring"


def test_successful_single_available_term_does_not_change_planning_term(
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

    assert meta["default_term"] == "2025 Spring"
    assert meta["query_terms"] == ["2026 Fall"]
    assert meta["default_term_changed"] is False
    assert persisted["term_mode"] == "auto"
    assert persisted["default_term"] == "2025 Spring"


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

    assert meta["default_term"] == "2025 Spring"
    assert meta["query_terms"] == ["2026 Fall"]
    persisted = sessions_data.get_session_meta("demo_001", meta["session_id"])
    assert persisted["term_mode"] == "auto"


def test_unknown_term_tool_success_does_not_change_planning_term(monkeypatch):
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

    assert meta["default_term"] == "2025 Spring"
    persisted = sessions_data.get_session_meta("demo_001", meta["session_id"])
    assert persisted["term_mode"] == "auto"
    assert meta["query_terms"] == ["2026 Fall"]


def test_known_unavailable_term_reaches_agent_for_tool_or_context_handling(
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

    captured = {}

    async def fake_handle(_message, state, _memory, *, queue, **_kwargs):
        captured["term"] = state["term"]
        await queue.put({"type": "token", "text": "当前数据暂未发布。"})
        return "当前数据暂未发布。", [], [], None

    monkeypatch.setattr(chat_router, "_handle_agent", fake_handle)
    events = asyncio.run(
        _collect_events(
            ChatRequest(message="show courses in 2027 Winter", session_id="")
        )
    )
    meta = _meta(events)

    assert captured["term"] == "2027 Winter"
    assert events[0]["text"] == "当前数据暂未发布。"
    assert meta["default_term"] == "2025 Spring"
    assert meta["query_terms"] == ["2027 Winter"]
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
    assert meta["query_terms"] == ["2026 Winter", "2026 Fall"]
    assert meta["query_term_source"] == "comparison"
    assert meta["default_term"] == "2025 Spring"
    persisted = sessions_data.get_session_meta("demo_001", meta["session_id"])
    assert persisted["term_mode"] == "auto"


def test_q1_q2_q3_keeps_discussion_focus_while_migrating_manual_default(
    monkeypatch,
):
    session_id = sessions_data.create_session(
        "demo_001",
        title="Q1 Q2 Q3",
        default_term="2025 Fall",
    )
    sessions_data.update_session_meta(
        "demo_001",
        session_id,
        default_term="2025 Fall",
        term_mode="manual",
        term_source="user_ui",
        term_updated_by="user_ui",
    )
    captured: list[dict] = []

    async def fake_handle(_message, state, _memory, *, queue, **_kwargs):
        captured.append({
            "query_terms": list(state["query_terms"]),
            "response_language": state["response_language"],
            "default_term": state["default_term"],
        })
        await queue.put({"type": "token", "text": "已按本轮范围查询。"})
        return "已按本轮范围查询。", [], [], None

    monkeypatch.setattr(chat_router, "_handle_agent", fake_handle)
    for message in (
        "2024 Fall 有没有 ICS 33？",
        "那 2025 Fall 呢？",
        "对比一下这两个学期",
    ):
        asyncio.run(
            _collect_events(ChatRequest(message=message, session_id=session_id))
        )

    assert [item["query_terms"] for item in captured] == [
        ["2024 Fall"],
        ["2025 Fall"],
        ["2024 Fall", "2025 Fall"],
    ]
    assert all(item["response_language"] == "zh" for item in captured)
    assert all(item["default_term"] == "2025 Spring" for item in captured)
    persisted = sessions_data.get_session_meta("demo_001", session_id)
    assert persisted["default_term"] == "2025 Spring"
    assert persisted["term_mode"] == "auto"


def test_followup_reuses_latest_complete_discussion_term_without_changing_planning_term(
    monkeypatch,
):
    session_id = sessions_data.create_session(
        "demo_001",
        title="Discussion term fixture",
        term_scope="2025 Spring",
    )
    sessions_data.append_turn(
        "demo_001",
        session_id,
        "user",
        "请查一下 2025 Fall 的课程。",
    )
    sessions_data.append_turn(
        "demo_001",
        session_id,
        "assistant",
        "你想继续比较哪些课程？",
    )
    sessions_data.update_session_meta(
        "demo_001",
        session_id,
        recent_query_focus={
            "course_ids": [],
            "terms": ["2025 Fall"],
            "updated_at": "2026-07-31T00:00:00+00:00",
        },
    )
    captured = {}

    async def fake_handle(_message, state, _memory, *, queue, **_kwargs):
        captured.update(state)
        await queue.put({"type": "token", "text": "继续按 2025 Fall 查询。"})
        return "继续按 2025 Fall 查询。", [], [], None

    monkeypatch.setattr(chat_router, "_handle_agent", fake_handle)
    meta = _meta(
        asyncio.run(
            _collect_events(
                ChatRequest(message="这个学期有哪些独有的课？", session_id=session_id)
            )
        )
    )

    assert captured["term"] == "2025 Fall"
    assert captured["query_terms"] == ["2025 Fall"]
    assert captured["default_term"] == "2025 Spring"
    assert meta["default_term"] == "2025 Spring"


def test_missing_year_reaches_agent_with_inferred_term(monkeypatch):
    captured = {}

    async def fake_handle(_message, state, _memory, *, queue, recent_turns, **_kwargs):
        captured["term"] = state["term"]
        captured["recent_turns"] = recent_turns
        await queue.put({"type": "token", "text": "按 2025 Fall 查询。"})
        return "按 2025 Fall 查询。", [], [], None

    monkeypatch.setattr(chat_router, "_handle_agent", fake_handle)
    events = asyncio.run(
        _collect_events(ChatRequest(message="show Fall courses", session_id=""))
    )

    assert events[0]["text"] == "按 2025 Fall 查询。"
    assert captured["term"] == "2025 Fall"
    assert captured["recent_turns"] == []
    assert _meta(events)["default_term"] == "2025 Spring"
    assert _meta(events)["query_terms"] == ["2025 Fall"]
    assert _meta(events)["inferred_year"] is True


def test_tool_arguments_use_canonical_effective_term():
    resolved, error = agent_tools.resolve_tool_arguments(
        "get_sections",
        {"course_id": "COMPSCI 161"},
        context={"allowed_query_terms": ["Fall 2026"]},
    )
    assert error is None
    assert resolved["term"] == "2026 Fall"

    resolved, error = agent_tools.resolve_tool_arguments(
        "get_sections",
        {"course_id": "COMPSCI 161", "term": "2025 Spring"},
        context={"allowed_query_terms": ["2025 Spring"]},
    )
    assert error is None
    assert resolved["term"] == "2025 Spring"

    _, error = agent_tools.resolve_tool_arguments(
        "get_sections",
        {"course_id": "COMPSCI 161", "term": "2026 Fall"},
        context={"allowed_query_terms": ["2025 Spring"]},
    )
    assert "outside the allowed" in str(error)

    _, error = agent_tools.resolve_tool_arguments(
        "get_sections",
        {"course_id": "COMPSCI 161", "term": "2027 Winter"},
        context={"allowed_query_terms": ["2025 Spring", "2026 Fall"]},
    )
    assert "outside the allowed" in str(error)


def test_successful_tool_terms_are_canonical_and_deduplicated():
    meta = {
        "successful_tool_calls": [
            {"name": "get_sections", "args": {"term": "Fall 2026"}},
            {"name": "get_sections", "args": {"term": "2026 Winter"}},
            {"name": "get_sections", "args": {"term": "2026 Fall"}},
        ]
    }

    assert chat_router._tool_terms_from_agent_meta(meta) == [
        "2026 Fall",
        "2026 Winter",
    ]
