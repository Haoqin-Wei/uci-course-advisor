from __future__ import annotations

import asyncio
import json

import pytest

from app import observability
from app.agent import loop as agent_loop
from app.agent.workflow_router import (
    WORKFLOW_REGISTRY,
    build_primary_workflow_plan,
    build_route_hint_message,
    route_search_workflows,
    route_solution,
)
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response


async def _collect(events) -> list[dict]:
    return [event async for event in events]


def test_router_classifies_live_availability_without_agentic_search() -> None:
    route = route_search_workflows("CS161 现在还有几个位置？", term="Fall 2026")

    assert route is not None
    assert route["route_type"] == "workflow"
    assert route["mode"] == "developer_workflow"
    assert route["workflow_ids"] == ["websoc_live_availability"]
    assert route["intents"] == ["availability"]
    assert route["recommended_tools"] == ["get_live_sections"]
    assert route["course_ids"] == ["COMPSCI 161"]
    assert route["search_tools_are_supplemental"] is True
    assert route["source_conflict_policy"] == "present_both"


def test_router_classifies_department_restrictions_and_department_alias() -> None:
    route = route_search_workflows(
        "Fall 2026 ICS 专业限制什么时候解除？",
        term="Spring 2026",
    )

    assert route is not None
    assert route["intents"] == ["department_restriction"]
    assert route["recommended_tools"] == ["get_department_restrictions"]
    assert route["departments"] == ["I&C SCI"]
    assert route["explicit_terms"] == ["2026 Fall"]
    assert route["term"] == "2026 Fall"
    assert route["selected_term"] == "Spring 2026"

    plan = build_primary_workflow_plan(route)
    assert plan["clarification"] is None
    assert plan["calls"] == [
        {
            "workflow_id": "websoc_department_restrictions",
            "tool": "get_department_restrictions",
            "args": {
                "term": "2026 Fall",
                "follow_links": True,
                "department": "I&C SCI",
            },
        }
    ]


def test_department_restriction_plan_uses_resolved_effective_term() -> None:
    route = route_solution("ART 专业限制什么时候解除？", term="Spring 2026")
    plan = build_primary_workflow_plan(route)

    assert plan["clarification"] is None
    assert plan["calls"][0]["args"]["term"] == "Spring 2026"


@pytest.mark.parametrize(
    "query",
    [
        "Fall 2026 ICS enrollment restrictions",
        "Fall 2026 ICS authorization code requirements",
        "Fall 2026 ART B restriction什么时候解除",
        "Fall 2026 ART 选课限制什么时候解除",
        "Fall 2026 ART 外专业什么时候能选",
    ],
)
def test_restriction_workflow_covers_supported_restriction_phrases(query) -> None:
    route = route_solution(query, term="Spring 2026")

    assert "websoc_department_restrictions" in route["workflow_ids"]


def test_router_classifies_combined_availability_and_restriction_question() -> None:
    route = route_search_workflows(
        "Fall 2026 CS161 现在还有位置吗？major restriction 什么时候解除？",
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
    assert "execute get_live_sections first, then get_department_restrictions" in hint["content"]
    assert "optional supplemental tools" in hint["content"]
    assert "present both claims and both sources" in hint["content"]


def test_router_ignores_unrelated_agentic_search_questions() -> None:
    assert route_search_workflows("Who is teaching databases at UCI?", term="Fall 2026") is None
    route = route_solution("Who is teaching databases at UCI?", term="Fall 2026")
    assert route == {
        "route_type": "agentic",
        "mode": "agentic",
        "workflow_ids": [],
        "intents": [],
        "recommended_tools": [],
        "term": "Fall 2026",
    }


def test_workflow_registry_is_explicit_and_developer_maintained() -> None:
    assert [rule.workflow_id for rule in WORKFLOW_REGISTRY] == [
        "websoc_live_availability",
        "websoc_department_restrictions",
    ]
    assert [rule.tools for rule in WORKFLOW_REGISTRY] == [
        ("get_live_sections",),
        ("get_department_restrictions",),
    ]


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
    assert "Developer workflow registry match" in first_call_messages[0]["content"]
    assert "get_live_sections" in first_call_messages[0]["content"]
    assert messages[0]["role"] == "user"
    assert "Developer workflow registry match" not in json.dumps(messages, ensure_ascii=False)
    assert events[0]["name"] == "get_live_sections"
    metrics = observability.snapshot_metrics()
    assert metrics["counters"]["workflow_router.matches{intent=availability}"] == 1
    assert metrics["counters"]["solution_router.routes{route=workflow}"] == 1
