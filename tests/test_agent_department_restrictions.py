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
        source_url = "https://www.reg.uci.edu/perl/WebSoc?Dept=ART"
        return {
            "ok": True,
            "workflow_id": "websoc_department_restrictions",
            "term": kwargs["term"],
            "department": kwargs["department"],
            "source_url": source_url,
            "fields": {
                "major_restriction_removed_at": "Monday, August 24th, 2026 at noon",
            },
            "links": [],
            "fetches": [
                {
                    "method": "GET",
                    "url": "https://www.reg.uci.edu/perl/WebSoc",
                    "final_url": "https://www.reg.uci.edu/perl/WebSoc",
                    "host": "www.reg.uci.edu",
                    "source_role": "registrar_websoc_form",
                    "status_code": 200,
                    "ok": True,
                    "bytes": 100,
                    "duration_ms": 5,
                    "depth": 0,
                    "parent_url": None,
                    "error": None,
                },
                {
                    "method": "POST",
                    "url": source_url,
                    "final_url": source_url,
                    "host": "www.reg.uci.edu",
                    "source_role": "registrar_websoc_results",
                    "status_code": 200,
                    "ok": True,
                    "bytes": 200,
                    "duration_ms": 10,
                    "depth": 0,
                    "parent_url": None,
                    "error": None,
                },
            ],
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
    assert result["restriction_type"] == "school_major"
    assert result["linked_pages"]["ok"] is True
    assert result["linked_pages"]["pages"] == []
    assert result["evidence_bundle"]["evidence_status"] == "verified"
    assert result["verified_facts"]["primary"]["restriction_type"] == "school_major"
    assert len(result["fetch_summary"]) == 2
    assert result["fetch_summary"][0]["provides_evidence"] is False
    assert result["fetch_summary"][1]["provides_evidence"] is True
    assert "fetches" not in result
    assert "school_comments" not in result
    assert "comment_blocks" not in result
    assert calls == [
        ("fetch", {"term": "2026 Fall", "department": "ART"}),
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
    assert calls == [{"term": "2026 Fall", "department": "COMPSCI"}]


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
    assert "`evidence_bundle` is the ONLY authority" in prompt
    assert "never substitute a New Only/NOR date" in prompt


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
    assert events[0]["label"] == "读取 WebSoc 部门说明 · ART · 2026 Fall"
    assert events[0]["server_forced"] is True
    assert events[1]["ok"] is True
    assert events[1]["server_forced"] is True
    prompt = "\n".join(
        message.get("content") or "" for message in client.calls[0].messages
    )
    assert "server already executed" in prompt
    assert "Primary workflow results" in prompt
    assert "Monday, August 24th, 2026 at noon" in prompt
    assert "Add at most two short sentences" in prompt
    assert '"linked_pages"' not in prompt
    assert '"timeline_events"' not in prompt
    assert "event=workflow_primary_tool_done" in caplog.text
    assert "workflow_id" in caplog.text
    assert "source_url" in caplog.text
    assert "https://www.reg.uci.edu/perl/WebSoc?Dept=ART" in caplog.text
    assert "event=agent_web_research_summary" in caplog.text
    assert "fetched_urls=" in caplog.text
    assert "restriction_fields" not in caplog.text
    assert "Monday, August 24th, 2026 at noon" not in caplog.text
    client.assert_exhausted()


def test_agent_uses_effective_term_for_forced_restriction_workflow(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_fetch(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "term": kwargs["term"],
            "department": kwargs["department"],
            "fields": {},
            "links": [],
        }

    monkeypatch.setattr(
        agent_tools.websoc_workflow,
        "fetch_websoc_department_restrictions",
        fake_fetch,
    )
    monkeypatch.setattr(
        agent_tools.websoc_workflow,
        "fetch_linked_official_pages",
        lambda _result: {"ok": True, "pages": []},
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

    assert events[0]["type"] == "tool_call_start"
    assert events[0]["args"]["term"] == "2026 Spring"
    assert calls == [{"term": "2026 Spring", "department": "ART"}]
    assert events[-1]["type"] == "final"
    assert events[-1]["deterministic_restriction_answer"] is True
    assert "未能从已抓取的官方来源验证" in events[-1]["text"]
    assert client.calls == []
    client.assert_exhausted()
