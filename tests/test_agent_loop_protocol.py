from __future__ import annotations

import asyncio
import json

import pytest

from app.agent import loop as agent_loop
from tests.fakes.llm import (
    ScriptedLLMClient,
    text_response,
    tool_call,
    tool_response,
)


async def _collect(events) -> list[dict]:
    return [event async for event in events]


def test_run_agent_streams_tokens_then_final():
    client = ScriptedLLMClient(text_response("Hello", " world"))
    messages = [{"role": "user", "content": "Hi"}]

    events = asyncio.run(
        _collect(
            agent_loop.run_agent(
                messages,
                client=client,
                model="fake-model",
                user_id="student_001",
            )
        )
    )

    assert [event["type"] for event in events] == ["token", "token", "final"]
    assert events[-1] == {
        "type": "final",
        "text": "Hello world",
        "iterations": 1,
        "tool_calls": 0,
    }
    assert client.calls[0].model == "fake-model"
    assert client.calls[0].stream is True
    assert client.calls[0].tools
    client.assert_exhausted()


def test_run_agent_dispatches_tool_then_returns_final():
    client = ScriptedLLMClient(
        tool_response(
            tool_call("get_policy", {}, call_id="call_policy"),
            leading_tokens=("Checking policy. ",),
            reasoning_tokens=("Need the policy index.",),
        ),
        text_response("Policy lookup complete."),
    )
    messages = [{"role": "user", "content": "What policies are available?"}]

    events = asyncio.run(
        _collect(
            agent_loop.run_agent(
                messages,
                client=client,
                model="fake-model",
                user_id="student_001",
                term="Spring 2025",
            )
        )
    )

    assert [event["type"] for event in events] == [
        "token",
        "tool_call_start",
        "tool_call_done",
        "token",
        "final",
    ]
    assert events[1]["name"] == "get_policy"
    assert events[2]["ok"] is True
    assert events[-1]["text"] == "Policy lookup complete."
    assert messages[1]["role"] == "assistant"
    assert messages[1]["reasoning_content"] == "Need the policy index."
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "call_policy"
    assert client.calls[1].messages[-1]["role"] == "tool"
    client.assert_exhausted()


def test_run_agent_passes_pending_schedule_to_tool_context(monkeypatch):
    pending_schedule = [
        {"course_id": "COMPSCI161", "section": "A", "status": "pending"}
    ]
    seen_contexts = []

    def fake_dispatch(name, args, *, context):
        seen_contexts.append(context)
        return {"ok": True, "name": name, "args": args}

    monkeypatch.setattr(agent_loop.agent_tools, "dispatch", fake_dispatch)
    client = ScriptedLLMClient(
        tool_response(tool_call("get_policy", {}, call_id="call_policy")),
        text_response("Done."),
    )

    events = asyncio.run(
        _collect(
            agent_loop.run_agent(
                [{"role": "user", "content": "Check policy"}],
                client=client,
                model="fake-model",
                user_id="student_001",
                term="Spring 2025",
                pending_schedule=pending_schedule,
            )
        )
    )

    assert events[-1]["text"] == "Done."
    assert seen_contexts[0]["pending_schedule"] == pending_schedule


def test_identical_term_read_tool_calls_reuse_first_result(monkeypatch):
    dispatch_calls: list[tuple[str, dict]] = []

    def fake_dispatch(name, args, *, context):
        dispatch_calls.append((name, dict(args)))
        return {
            "ok": True,
            "found": False,
            "course_id": args["course_id"],
            "term": args["term"],
            "sections": [],
        }

    monkeypatch.setattr(agent_loop.agent_tools, "dispatch", fake_dispatch)
    repeated_args = {"course_id": "ECON167", "term": "2025 Winter"}
    client = ScriptedLLMClient(
        tool_response(
            tool_call(
                "get_sections",
                repeated_args,
                call_id="call_sections_1",
                index=0,
            ),
            tool_call(
                "get_sections",
                repeated_args,
                call_id="call_sections_2",
                index=1,
            ),
        ),
        text_response("Done."),
    )

    events = asyncio.run(
        _collect(
            agent_loop.run_agent(
                [{"role": "user", "content": "Check these records."}],
                client=client,
                model="fake-model",
                user_id="student_001",
                term="2025 Winter",
                allowed_query_terms=["2025 Winter"],
                query_term_source="explicit",
            )
        )
    )

    assert dispatch_calls == [
        ("get_sections", {"course_id": "ECON167", "term": "2025 Winter"})
    ]
    done_events = [event for event in events if event["type"] == "tool_call_done"]
    assert [event["reused"] for event in done_events] == [False, True]
    assert events[-1]["text"] == "Done."
    client.assert_exhausted()


def test_authoritative_offering_result_blocks_redundant_web_search(monkeypatch):
    dispatch_calls: list[str] = []

    def fake_dispatch(name, args, *, context):
        dispatch_calls.append(name)
        assert name == "get_sections"
        return {
            "ok": True,
            "found": False,
            "source": "registrar_websoc",
            "source_url": "https://www.reg.uci.edu/perl/WebSoc",
            "authoritative": True,
            "offering_status": "not_offered",
            "course_id": "ECON 167",
            "term": "2025 Winter",
            "sections": [],
        }

    monkeypatch.setattr(agent_loop.agent_tools, "dispatch", fake_dispatch)
    client = ScriptedLLMClient(
        tool_response(
            tool_call(
                "get_sections",
                {"course_id": "ECON167", "term": "2025 Winter"},
                call_id="call_sections",
                index=0,
            ),
            tool_call(
                "web_search",
                {
                    "query": "UCI ECON 167 Winter 2025 schedule of classes WebSoc",
                    "reason": "Double-check the offering.",
                },
                call_id="call_search",
                index=1,
            ),
        ),
        text_response("The official result is definitive."),
    )
    messages = [{"role": "user", "content": "Was ECON 167 offered in Winter 2025?"}]

    events = asyncio.run(
        _collect(
            agent_loop.run_agent(
                messages,
                client=client,
                model="fake-model",
                user_id="student_001",
                term="2025 Winter",
                allowed_query_terms=["2025 Winter"],
                query_term_source="explicit",
            )
        )
    )

    assert dispatch_calls == ["get_sections"]
    done_events = [event for event in events if event["type"] == "tool_call_done"]
    assert done_events[0]["offering_status"] == "not_offered"
    assert done_events[0]["authoritative"] is True
    assert done_events[0]["section_count"] == 0
    assert done_events[1]["ok"] is False
    blocked_payload = next(
        json.loads(message["content"])
        for message in messages
        if message.get("role") == "tool"
        and message.get("tool_call_id") == "call_search"
    )
    assert blocked_payload["error_code"] == "definitive_offering_already_resolved"
    client.assert_exhausted()


def test_run_agent_surfaces_llm_create_error():
    client = ScriptedLLMClient(RuntimeError("simulated outage"))

    events = asyncio.run(
        _collect(
            agent_loop.run_agent(
                [{"role": "user", "content": "Hi"}],
                client=client,
                model="fake-model",
                user_id="student_001",
            )
        )
    )

    assert events == [
        {
            "type": "error",
            "message": "LLM call failed: simulated outage",
        }
    ]
    client.assert_exhausted()


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("reason", ["max_iterations", "max_tool_calls"])
def test_limit_reached_fallback_and_continue(monkeypatch, language, reason):
    language_instruction = f"Respond in {'Chinese' if language == 'zh' else 'English'}."

    def fallback_response(call):
        assert call.tools is not None
        assert [
            schema["function"]["name"] for schema in call.tools
        ] == ["propose_recommendation"]
        assert "tool-call budget" in call.messages[-1]["content"]
        assert language_instruction in call.messages[-1]["content"]
        return text_response("Best effort.")

    calls = [tool_call("get_policy", {}, call_id="call_policy")]
    if reason == "max_tool_calls":
        monkeypatch.setattr(agent_loop, "MAX_TOTAL_TOOLS", 1)
        calls.append(tool_call("get_policy", {}, call_id="call_policy_extra", index=1))

    def continue_response(call):
        assert language_instruction in call.messages[-1]["content"]
        return text_response("Resumed answer.")

    client = ScriptedLLMClient(
        tool_response(*calls),
        fallback_response,
        tool_response(*calls),
        fallback_response,
        continue_response,
    )
    monkeypatch.setattr(agent_loop, "MAX_ITERATIONS", 1)

    events = asyncio.run(
        _collect(
            agent_loop.run_agent(
                [{"role": "user", "content": "Help me"}],
                client=client,
                model="fake-model",
                user_id="student_001",
                term="Spring 2025",
                response_language=language,
            )
        )
    )

    assert [event["type"] for event in events] == [
        "tool_call_start",
        "tool_call_done",
        "limit_reached",
        "token",
        "final",
    ]
    limit_event = events[2]
    assert limit_event["reason"] == reason
    assert limit_event["response_language"] == language
    assert limit_event["iterations"] == 1
    assert limit_event["tool_calls"] == 1
    assert limit_event["continuation_id"]
    assert events[-1]["text"] == "Best effort."
    assert events[-1]["truncated"] is True

    continuation_id = limit_event["continuation_id"]
    resumed = asyncio.run(
        _collect(
            agent_loop.resume_agent(
                continuation_id,
                client=client,
                model="fake-model",
            )
        )
    )
    second_limit = next(event for event in resumed if event["type"] == "limit_reached")
    assert second_limit["response_language"] == language
    assert second_limit["reason"] == reason
    resumed = asyncio.run(
        _collect(
            agent_loop.resume_agent(
                second_limit["continuation_id"],
                client=client,
                model="fake-model",
            )
        )
    )
    assert [event["type"] for event in resumed] == ["token", "final"]
    assert resumed[-1]["text"] == "Resumed answer."

    replayed = asyncio.run(
        _collect(
            agent_loop.resume_agent(
                continuation_id,
                client=client,
                model="fake-model",
            )
        )
    )
    assert replayed == [
        {
            "type": "error",
            "message": "continuation_id not found or expired",
        }
    ]
    client.assert_exhausted()
