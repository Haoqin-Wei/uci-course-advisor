from __future__ import annotations

import asyncio
import json

from app import observability
from app.agent import loop as agent_loop
from app.agent.workflow_router import build_route_hint_message, route_search_workflows
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response


async def _collect(events) -> list[dict]:
    return [event async for event in events]


def test_router_classifies_live_availability_without_agentic_search() -> None:
    route = route_search_workflows("CS161 现在还有几个位置？", term="Fall 2026")

    assert route is not None
    assert route["mode"] == "fixed_workflow_first"
    assert route["intents"] == ["availability"]
    assert route["recommended_tools"] == ["get_live_sections"]
    assert route["course_ids"] == ["COMPSCI 161"]
    assert route["no_agentic_web_search_first"] is True


def test_router_classifies_department_restrictions_and_department_alias() -> None:
    route = route_search_workflows("ICS 专业限制什么时候解除？", term="Fall 2026")

    assert route is not None
    assert route["intents"] == ["department_restriction"]
    assert route["recommended_tools"] == ["get_department_restrictions"]
    assert route["departments"] == ["I&C SCI"]


def test_router_classifies_combined_availability_and_restriction_question() -> None:
    route = route_search_workflows(
        "CS161 现在还有位置吗？major restriction 什么时候解除？",
        term="Fall 2026",
    )

    assert route is not None
    assert route["intents"] == ["availability", "department_restriction"]
    assert route["recommended_tools"] == [
        "get_live_sections",
        "get_department_restrictions",
    ]
    hint = build_route_hint_message(route)
    assert hint is not None
    assert "call get_live_sections first, then get_department_restrictions" in hint["content"]
    assert "before using general web_search" in hint["content"]


def test_router_ignores_unrelated_agentic_search_questions() -> None:
    assert route_search_workflows("Who is teaching databases at UCI?", term="Fall 2026") is None


def test_agent_loop_injects_route_hint_only_for_llm_request(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.agent.tools.db.get_live_sections",
        lambda **kwargs: {
            "found": True,
            "source": "live_anteater_websoc",
            "sections": [],
        },
    )
    messages = [{"role": "user", "content": "CS161 现在还有位置吗？"}]
    client = ScriptedLLMClient(
        tool_response(
            tool_call(
                "get_live_sections",
                {"course_id": "CS161", "term": "Fall 2026"},
                call_id="call_live",
            )
        ),
        text_response("No sections returned."),
    )

    events = asyncio.run(
        _collect(
            agent_loop.run_agent(
                messages,
                client=client,
                model="fake-model",
                user_id="student_001",
                term="Fall 2026",
            )
        )
    )

    first_call_messages = client.calls[0].messages
    assert first_call_messages[0]["role"] == "system"
    assert "Workflow route hint" in first_call_messages[0]["content"]
    assert "get_live_sections" in first_call_messages[0]["content"]
    assert messages[0]["role"] == "user"
    assert "Workflow route hint" not in json.dumps(messages, ensure_ascii=False)
    assert events[0]["name"] == "get_live_sections"
    metrics = observability.snapshot_metrics()
    assert metrics["counters"]["workflow_router.matches{intent=availability}"] == 1
