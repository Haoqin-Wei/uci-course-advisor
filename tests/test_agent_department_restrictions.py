from __future__ import annotations

import asyncio
import json

from app.agent import loop as agent_loop
from app.agent import tools as agent_tools
from app.llm.adapter import AGENT_SYSTEM_PROMPT
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response


async def _collect(events) -> list[dict]:
    return [event async for event in events]


def _schema_by_name(name: str) -> dict:
    return next(
        schema for schema in agent_tools.TOOL_SCHEMAS
        if schema["function"]["name"] == name
    )


def test_department_restrictions_tool_schema_is_fixed_workflow() -> None:
    schema = _schema_by_name("get_department_restrictions")["function"]

    assert "Fixed Registrar WebSoc workflow" in schema["description"]
    assert "Do not use general web_search first" in schema["description"]
    assert schema["parameters"]["required"] == ["term", "department"]
    assert set(schema["parameters"]["properties"]) == {
        "term",
        "department",
        "restriction_type",
        "follow_links",
    }


def test_department_restrictions_dispatcher_fetches_websoc_and_linked_pages(
    monkeypatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_fetch(**kwargs):
        calls.append(("fetch", kwargs))
        return {
            "ok": True,
            "workflow_id": "websoc_department_restrictions",
            "term": kwargs["term"],
            "department": kwargs["department"],
            "source_url": "https://www.reg.uci.edu/perl/WebSoc?Dept=ART",
            "fields": {
                "major_restriction_removed_at": "Monday, August 24th, 2026 at noon",
            },
            "links": [],
        }

    def fake_deep_read(result):
        calls.append(("deep_read", {"source_url": result["source_url"]}))
        return {"ok": True, "pages": []}

    monkeypatch.setattr(
        agent_tools.websoc_workflow,
        "fetch_websoc_department_restrictions",
        fake_fetch,
    )
    monkeypatch.setattr(
        agent_tools.websoc_workflow,
        "fetch_linked_official_pages",
        fake_deep_read,
    )

    result = agent_tools.dispatch(
        "get_department_restrictions",
        {
            "term": "Fall 2026",
            "department": "ART",
            "restriction_type": "major_restriction",
        },
        context={},
    )

    assert result["ok"] is True
    assert result["restriction_type"] == "major_restriction"
    assert result["linked_pages"] == {"ok": True, "pages": []}
    assert calls == [
        ("fetch", {"term": "Fall 2026", "department": "ART"}),
        ("deep_read", {"source_url": "https://www.reg.uci.edu/perl/WebSoc?Dept=ART"}),
    ]


def test_department_restrictions_humanized_chip_label() -> None:
    label = agent_tools.humanize_tool_call(
        "get_department_restrictions",
        {"department": "I&C SCI", "term": "Fall 2026"},
    )

    assert label == "读取 WebSoc 部门说明 · I&C SCI · Fall 2026"


def test_agent_prompt_encodes_department_restriction_rules() -> None:
    prompt = AGENT_SYSTEM_PROMPT

    assert "Department restrictions (HARD RULE)" in prompt
    assert "call `get_department_restrictions(term, department)` first" in prompt
    assert "Do NOT use general `web_search` or DuckDuckGo first" in prompt
    assert "WebSoc comments point to an official UCI department page" in prompt


def test_agent_can_dispatch_department_restrictions_with_sse_chip(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_tools.websoc_workflow,
        "fetch_websoc_department_restrictions",
        lambda **kwargs: {
            "ok": True,
            "workflow_id": "websoc_department_restrictions",
            "term": kwargs["term"],
            "department": kwargs["department"],
            "source_url": "https://www.reg.uci.edu/perl/WebSoc?Dept=ART",
            "fields": {
                "major_restriction_removed_at": "Monday, August 24th, 2026 at noon",
            },
            "links": [],
        },
    )
    monkeypatch.setattr(
        agent_tools.websoc_workflow,
        "fetch_linked_official_pages",
        lambda _result: {"ok": True, "pages": []},
    )

    messages = [{"role": "user", "content": "ART 专业限制什么时候解除？"}]
    client = ScriptedLLMClient(
        tool_response(
            tool_call(
                "get_department_restrictions",
                {
                    "term": "Fall 2026",
                    "department": "ART",
                    "restriction_type": "major_restriction",
                },
                call_id="call_restrictions",
            )
        ),
        text_response("ART restrictions are removed on Monday, August 24th, 2026 at noon."),
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

    assert events[0]["type"] == "tool_call_start"
    assert events[0]["name"] == "get_department_restrictions"
    assert events[0]["label"] == "读取 WebSoc 部门说明 · ART · Fall 2026"
    assert events[1]["ok"] is True
    tool_payload = json.loads(messages[2]["content"])
    assert tool_payload["workflow_id"] == "websoc_department_restrictions"
    assert (
        tool_payload["fields"]["major_restriction_removed_at"]
        == "Monday, August 24th, 2026 at noon"
    )
    client.assert_exhausted()
