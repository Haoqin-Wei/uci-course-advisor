import asyncio
import re
from types import SimpleNamespace

import pytest

from app.agent import loop, tools
from app.llm import adapter
from app.routers import chat
from app.response_language import display_term, language_instruction, response_language
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response


@pytest.mark.parametrize(("message", "previous", "expected"), [
    ("Recommend courses", "推荐课程", "en"),
    ("yes", "推荐课程", "en"),
    ("ok", "推荐课程", "en"),
    ("继续", "Recommend courses", "zh"),
    ("帮我查 ICS33", "Recommend courses", "zh"),
    ("ICS33", "推荐课程", "zh"),
    ("cs161", "推荐课程", "zh"),
    ("I&C SCI 31 / STATS 67", "推荐课程", "zh"),
    ("👍", "Recommend courses", "en"),
    ("123", "推荐课程", "zh"),
    ("recommend 4", "推荐课程", "en"),
])
def test_language_switches_on_current_prose_and_skips_neutral_history(message, previous, expected):
    history = [
        {"role": "user", "content": previous},
        {"role": "assistant", "content": "Wrong language: 推荐课程"},
        {"role": "user", "content": "CS161"},
        {"role": "assistant", "content": "A previous English response"},
    ]
    assert response_language(message, history) == expected
    assert response_language("👍") == "en"


@pytest.mark.parametrize("language", ["en", "zh"])
def test_every_tool_has_localized_progress(language):
    args = {"course_id": "ICS33", "course_a": "ICS33", "course_b": "ICS6B",
            "term": "2026 Fall", "terms": ["2026 Fall", "2027 Winter"],
            "instructor_name": "Thornton", "department": "I&C SCI", "ge_category": "III",
            "items": [{"course_id": "ICS33"}], "topic": "ge", "query": "UCI",
            "url": "https://uci.edu", "course": "ICS33"}
    for schema in tools.TOOL_SCHEMAS:
        name = schema["function"]["name"]
        label = tools.humanize_tool_call(name, args, language)
        assert bool(re.search(r"[\u3400-\u9fff]", label)) == (language == "zh"), label
        assert not label.startswith(("Calling ", "调用 ")), name
    assert args["term"] == "2026 Fall"  # display translation never changes API arguments
    assert display_term("2026 Fall", language) == ("2026年秋季" if language == "zh" else "2026 Fall")
    assert display_term("Fall 2026", language) == ("2026年秋季" if language == "zh" else "Fall 2026")
    assert tools.humanize_tool_call("search_courses", {}, language) == (
        "搜索课程（全部）" if language == "zh" else "Searching courses (all)"
    )


@pytest.mark.parametrize(("message", "language"), [("yes", "en"), ("继续", "zh")])
@pytest.mark.parametrize("structured", [False, True])
def test_custom_prompt_and_memory_cannot_remove_reply_language(message, language, structured):
    messages = adapter._build_messages_for_llm(
        message, {}, {}, system_prompt_override="Always reply in another language.",
        memory_context={"system_prompt_block": "Prefer an old conversation language."},
        recent_turns=[] if structured else None,
    )
    assert len([m for m in messages if m["role"] == "system"]) == 1
    assert messages[0]["content"].endswith(language_instruction(language))
    assert "Always reply in another language." in messages[0]["content"]
    assert "Prefer an old conversation language." in messages[0]["content"]


@pytest.mark.parametrize(("message", "language"), [("yes", "en"), ("继续", "zh"), ("CS161", "zh")])
def test_agent_uses_same_language_for_system_context_and_progress(monkeypatch, message, language):
    seen = {}

    async def fake_run(messages, **kwargs):
        seen.update(messages=messages, **kwargs)
        yield {"type": "final", "text": "fixture"}

    monkeypatch.setattr(adapter, "LLM_ENABLED", True)
    monkeypatch.setattr(adapter, "_get_client", lambda: object())
    monkeypatch.setattr(loop, "run_agent", fake_run)

    async def run():
        return [event async for event in adapter.stream_agent_response(
            message, {"default_term": "2026 Fall", "response_language": "en"},
            user_id="fixture", system_prompt_override="Always use Spanish.",
            recent_turns=[{"role": "user", "content": "推荐课程"}],
        )]

    asyncio.run(run())
    assert seen["response_language"] == language
    assert f"<response_language>{language}</response_language>" in seen["messages"][0]["content"]
    assert seen["messages"][0]["content"].endswith(language_instruction(language))


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("forced", [False, True])
def test_tool_events_carry_language_for_both_dispatch_paths(monkeypatch, language, forced):
    monkeypatch.setattr(loop.agent_tools, "dispatch", lambda *a, **kw: {"ok": True, "sections": []})
    monkeypatch.setattr(loop, "build_primary_workflow_plan", lambda *a: {
        "calls": [{"tool": "get_sections", "args": {"course_id": "ICS33", "term": "2026 Fall"}}] if forced else [],
    })
    scripted = [] if forced else [tool_response(tool_call("get_sections", {"course_id": "ICS33", "term": "2026 Fall"}))]
    client = ScriptedLLMClient(*scripted, text_response("Done"))

    async def run():
        return [event async for event in loop.run_agent(
            [{"role": "user", "content": "ICS33"}], client=client, model="fake",
            user_id="fixture", term="2026 Fall", response_language=language,
        )]

    events = asyncio.run(run())
    start = next(e for e in events if e["type"] == "tool_call_start")
    done = next(e for e in events if e["type"] == "tool_call_done")
    assert start["response_language"] == language
    assert start["label"] == tools.humanize_tool_call("get_sections", start["args"], language)
    assert done["label"] == start["label"]
    client.assert_exhausted()


def test_chinese_offline_fallback_does_not_copy_english_errors_or_source_prose(monkeypatch):
    record = SimpleNamespace(
        ref=SimpleNamespace(display=lambda: "COMPSCI 161"), title="Design and Analysis of Algorithms",
        units=None, min_units=None, max_units=None, description="An English course description",
        prerequisite_text="Must complete ICS 46", restriction="Restricted to majors",
    )
    monkeypatch.setattr(chat, "get_catalog", lambda _: SimpleNamespace(
        get_course=lambda _: record, get_sections=lambda _: [],
    ))
    monkeypatch.setattr(chat, "get_term_coverage", lambda _: {"coverage_status": "partial"})
    text = chat._grounded_agent_fallback_reply("查询 COMPSCI 161", {}, term="2026 Fall", reason="LLM call failed")
    assert "2026年秋季" in text and "不完整" in text and "学分：未知" in text
    assert "中文说明暂不可用" in text
    for untranslated in ["LLM call failed", "partial", "unknown", record.description, record.prerequisite_text, record.restriction]:
        assert untranslated not in text
    generic = chat._grounded_agent_fallback_reply("推荐课程", {}, term="2026 Fall", reason="LLM call failed")
    assert "LLM call failed" not in generic
