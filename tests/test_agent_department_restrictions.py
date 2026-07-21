from __future__ import annotations

import asyncio
import json
import logging

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
    assert schema["parameters"]["required"] == ["term"]
    assert set(schema["parameters"]["properties"]) == {
        "term",
        "department",
        "course_id",
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


def test_department_restrictions_dispatcher_resolves_department_from_course_id(
    monkeypatch,
) -> None:
    calls: list[dict] = []

    def fake_fetch(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "workflow_id": "websoc_department_restrictions",
            "term": kwargs["term"],
            "department": kwargs["department"],
            "source_url": "https://www.reg.uci.edu/perl/WebSoc?Dept=COMPSCI",
            "fields": {},
            "links": [],
        }

    monkeypatch.setattr(
        agent_tools.websoc_workflow,
        "fetch_websoc_department_restrictions",
        fake_fetch,
    )

    result = agent_tools.dispatch(
        "get_department_restrictions",
        {
            "term": "Fall 2026",
            "course_id": "CS161",
            "restriction_type": "major_restriction",
            "follow_links": False,
        },
        context={},
    )

    assert result["ok"] is True
    assert result["department"] == "COMPSCI"
    assert result["course_id"] == "COMPSCI 161"
    assert result["department_resolved_from"] == "course_id"
    assert calls == [{"term": "Fall 2026", "department": "COMPSCI"}]


def test_department_restrictions_dispatcher_rejects_missing_department() -> None:
    result = agent_tools.dispatch(
        "get_department_restrictions",
        {"term": "Fall 2026", "course_id": "not a course", "follow_links": False},
        context={},
    )

    assert result["ok"] is False
    assert result["error_code"] == "department_required"


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
    assert "get_department_restrictions(term, course_id=...)" in prompt
    assert "Do NOT use general `web_search` or DuckDuckGo first" in prompt
    assert "WebSoc comments point to an official UCI department page" in prompt
    assert "cite the Registrar WebSoc `source_url` as a markdown link" in prompt


def test_agent_can_dispatch_department_restrictions_with_sse_chip(
    monkeypatch,
    caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="app.agent.loop")
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

    messages = [{"role": "user", "content": "Fall 2026 ART 专业限制什么时候解除？"}]
    client = ScriptedLLMClient(
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
    assert events[0]["server_forced"] is True
    assert events[1]["ok"] is True
    assert events[1]["server_forced"] is True
    prompt = "\n".join(
        message.get("content") or "" for message in client.calls[0].messages
    )
    assert "server already executed" in prompt
    assert "Primary workflow results" in prompt
    assert "Monday, August 24th, 2026 at noon" in prompt
    assert "event=workflow_primary_tool_done" in caplog.text
    assert "workflow_id" in caplog.text
    assert "source_url" in caplog.text
    assert "https://www.reg.uci.edu/perl/WebSoc?Dept=ART" in caplog.text
    assert "restriction_fields" in caplog.text
    assert "Monday, August 24th, 2026 at noon" in caplog.text
    client.assert_exhausted()


def test_agent_asks_for_term_before_forced_restriction_workflow(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_tools.websoc_workflow,
        "fetch_websoc_department_restrictions",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("workflow must not run without an explicit term")
        ),
    )
    messages = [{"role": "user", "content": "ART 专业限制什么时候解除？"}]
    client = ScriptedLLMClient()

    events = asyncio.run(
        _collect(
            agent_loop.run_agent(
                messages,
                client=client,
                model="fake-model",
                user_id="student_001",
                term="Spring 2026",
            )
        )
    )

    assert events[-1]["clarification_required"] is True
    assert "Fall 2026" in events[-1]["text"]
    assert client.calls == []
