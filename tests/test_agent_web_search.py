from __future__ import annotations

import asyncio
import json

from app.agent import loop as agent_loop
from app.agent import tools as agent_tools
from app.data import web_search
from app.llm.adapter import AGENT_SYSTEM_PROMPT
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response


async def _collect(events) -> list[dict]:
    return [event async for event in events]


def _schema_by_name(name: str) -> dict:
    return next(
        schema for schema in agent_tools.TOOL_SCHEMAS
        if schema["function"]["name"] == name
    )


def test_web_search_tool_schema_requires_reason() -> None:
    schema = _schema_by_name("web_search")["function"]

    assert schema["parameters"]["required"] == ["query", "reason"]
    assert set(schema["parameters"]["properties"]) == {
        "query",
        "reason",
        "preferred_domains",
        "max_results",
    }
    assert "full pages" in schema["description"]


def test_web_search_dispatcher_uses_fake_provider(monkeypatch) -> None:
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "fake")
    web_search.set_fake_results(
        [
            {
                "title": "UCI Registrar",
                "url": "https://reg.uci.edu/",
                "snippet": "Official registrar page.",
            }
        ]
    )

    result = agent_tools.dispatch(
        "web_search",
        {
            "query": "UCI registrar",
            "reason": "user explicitly asked to search official UCI web pages",
            "max_results": 1,
        },
        context={"user_id": "student_001"},
    )

    assert result["ok"] is True
    assert result["results"][0]["source_class"] == "official_uci"
    assert result["results"][0]["trust_level"] == "high"


def test_web_search_dispatcher_rejects_empty_or_placeholder_reason(monkeypatch) -> None:
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "fake")

    result = agent_tools.dispatch(
        "web_search",
        {"query": "UCI calendar", "reason": "n/a"},
        context={"user_id": "student_001"},
    )

    assert result["ok"] is False
    assert result["error_code"] == "invalid_reason"


def test_web_search_humanized_chip_label() -> None:
    label = agent_tools.humanize_tool_call(
        "web_search",
        {"query": "UCI add drop deadline"},
    )

    assert label == "联网搜索 · UCI add drop deadline"


def test_search_skill_prompt_encodes_db_first_and_conflict_rules() -> None:
    prompt = AGENT_SYSTEM_PROMPT

    assert "Search Skill — web search and evidence chain" in prompt
    assert "Local DB tools are the default and highest-trust source" in prompt
    assert "Do NOT call `web_search` when" in prompt
    assert "coverage is `complete`" in prompt
    assert "partial`, `stale`, or `unavailable`" in prompt
    assert "Any web claim without a URL cannot be used as a factual source" in prompt
    assert "Reddit/forum/social is anecdotal only" in prompt
    assert "本地数据库显示" in prompt
    assert "网页来源显示" in prompt
    assert "Sources:" in prompt


def test_agent_can_dispatch_explicit_web_search_with_sse_chip(monkeypatch) -> None:
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "fake")
    web_search.set_fake_results(
        [
            {
                "title": "UCI Registrar — Quarterly Academic Calendar",
                "url": "https://reg.uci.edu/calendars/quarterly/2025-2026/quarterly25-26.html",
                "snippet": "Official UCI calendar.",
            }
        ]
    )
    messages = [{"role": "user", "content": "上网查一下 UCI add/drop deadline"}]
    client = ScriptedLLMClient(
        tool_response(
            tool_call(
                "web_search",
                {
                    "query": "UCI add drop deadline registrar",
                    "reason": "user explicitly asked to look up the latest UCI deadline online",
                    "preferred_domains": ["reg.uci.edu"],
                },
                call_id="call_web",
            )
        ),
        text_response("See [UCI Registrar](https://reg.uci.edu/)."),
    )

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
        "tool_call_start",
        "tool_call_done",
        "token",
        "final",
    ]
    assert events[0]["name"] == "web_search"
    assert events[0]["label"] == "联网搜索 · UCI add drop deadline registrar"
    assert events[1]["ok"] is True
    tool_payload = json.loads(messages[2]["content"])
    assert tool_payload["ok"] is True
    assert tool_payload["results"][0]["source_class"] == "official_uci"
    client.assert_exhausted()


def test_agent_web_search_disabled_is_reported_without_crashing() -> None:
    messages = [{"role": "user", "content": "search the web for this"}]
    client = ScriptedLLMClient(
        tool_response(
            tool_call(
                "web_search",
                {
                    "query": "UCI current deadline",
                    "reason": "user explicitly asked to search the web",
                },
                call_id="call_web",
            )
        ),
        text_response("Web search is unavailable right now."),
    )

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

    assert events[1]["type"] == "tool_call_done"
    assert events[1]["ok"] is False
    tool_payload = json.loads(messages[2]["content"])
    assert tool_payload["ok"] is False
    assert tool_payload["error_code"] == "web_search_disabled"
    assert events[-1]["text"] == "Web search is unavailable right now."
    client.assert_exhausted()
