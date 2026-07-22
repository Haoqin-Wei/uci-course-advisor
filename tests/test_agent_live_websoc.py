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


def test_get_live_sections_tool_schema_is_available() -> None:
    schema = _schema_by_name("get_live_sections")["function"]

    assert "Live UCI WebSoc availability" in schema["description"]
    assert schema["parameters"]["required"] == ["course_id", "term"]
    assert set(schema["parameters"]["properties"]) == {
        "course_id",
        "term",
        "section_codes",
        "force_refresh",
    }


def test_get_live_sections_dispatcher_uses_selected_term(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_get_live_sections(course_id, term, section_codes=None, force_refresh=False):
        calls.append(
            {
                "course_id": course_id,
                "term": term,
                "section_codes": section_codes,
                "force_refresh": force_refresh,
            }
        )
        return {
            "found": True,
            "source": "live_anteater_websoc",
            "is_live": True,
            "sections": [],
        }

    monkeypatch.setattr(agent_tools.db, "get_live_sections", fake_get_live_sections)

    result = agent_tools.dispatch(
        "get_live_sections",
        {
            "course_id": "CS161",
            "section_codes": ["34070"],
            "force_refresh": True,
        },
        context={"term": "Fall 2026"},
    )

    assert result["source"] == "live_anteater_websoc"
    assert calls == [
        {
            "course_id": "CS161",
            "term": "2026 Fall",
            "section_codes": ["34070"],
            "force_refresh": True,
        }
    ]


def test_get_live_sections_humanized_chip_label() -> None:
    label = agent_tools.humanize_tool_call(
        "get_live_sections",
        {"course_id": "COMPSCI 161", "term": "Fall 2026"},
    )

    assert label == "实时查询 WebSoc · COMPSCI 161 · Fall 2026"


def test_agent_prompt_encodes_live_availability_rules() -> None:
    prompt = AGENT_SYSTEM_PROMPT

    assert "Live availability (HARD RULE)" in prompt
    assert "call `get_live_sections(course, term)`" in prompt
    assert "New Only Reserved/NOR" in prompt
    assert "source` is `local_not_live`" in prompt
    assert "Live WebSoc via Anteater API" in prompt
    assert "as of" in prompt


def test_agent_can_dispatch_live_sections_with_sse_chip(monkeypatch) -> None:
    def fake_get_live_sections(course_id, term, section_codes=None, force_refresh=False):
        return {
            "found": True,
            "source": "live_anteater_websoc",
            "is_live": True,
            "term": term,
            "course_id": course_id,
            "retrieved_at": "2026-07-18T11:00:00Z",
            "sections": [
                {
                    "section_code": "34070",
                    "status": "OPEN",
                    "enrolled": 86,
                    "max_capacity": 100,
                    "seats_open": 14,
                    "updated_at": "2026-07-18T10:00:00Z",
                }
            ],
        }

    monkeypatch.setattr(agent_tools.db, "get_live_sections", fake_get_live_sections)

    messages = [{"role": "user", "content": "CS161 现在还有位置吗？"}]
    client = ScriptedLLMClient(
        tool_response(
            tool_call(
                "get_live_sections",
                {
                    "course_id": "CS161",
                    "term": "Fall 2026",
                    "force_refresh": True,
                },
                call_id="call_live",
            )
        ),
        text_response("CS161 is OPEN as of 2026-07-18T10:00:00Z."),
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
    assert events[0]["name"] == "get_live_sections"
    assert events[0]["label"] == "实时查询 WebSoc · CS161 · 2026 Fall"
    assert events[1]["ok"] is True
    tool_payload = json.loads(messages[2]["content"])
    assert tool_payload["source"] == "live_anteater_websoc"
    assert tool_payload["sections"][0]["seats_open"] == 14
    client.assert_exhausted()
