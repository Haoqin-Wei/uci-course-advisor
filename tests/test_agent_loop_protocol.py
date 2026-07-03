from __future__ import annotations

import asyncio

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


def test_limit_reached_fallback_and_continue(monkeypatch):
    def fallback_response(call):
        assert call.tools is not None
        assert [
            schema["function"]["name"] for schema in call.tools
        ] == ["propose_recommendation"]
        assert "tool-call budget" in call.messages[-1]["content"]
        return text_response("Best effort.")

    client = ScriptedLLMClient(
        tool_response(tool_call("get_policy", {}, call_id="call_policy")),
        fallback_response,
        text_response("Resumed answer."),
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
    assert limit_event["reason"] == "max_iterations"
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
