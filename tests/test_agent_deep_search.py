from __future__ import annotations

import asyncio
import json

from app.agent import loop as agent_loop
from app.agent import tools as agent_tools
from app.agent.deep_search_state import DeepSearchRunState
from app.data import deep_search, web_search
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response


async def _collect(events) -> list[dict]:
    return [event async for event in events]


def _schema_by_name(name: str) -> dict:
    return next(
        schema for schema in agent_tools.TOOL_SCHEMAS
        if schema["function"]["name"] == name
    )


def test_agent_tool_log_summary_keeps_urls_without_page_content() -> None:
    summary = agent_loop._summarize_tool_result(
        {
            "ok": True,
            "source_url": "https://example.com/start",
            "final_url": "https://example.com/final",
            "status_code": 200,
            "title": "Restriction update",
            "summary": "Major restrictions end at noon.",
            "key_passages": [{"text": "Major restrictions end at noon."}],
            "links": [{"url": "https://example.com/details"}],
        }
    )

    assert summary == {
        "ok": True,
        "source_url": "https://example.com/start",
        "final_url": "https://example.com/final",
        "status_code": 200,
        "link_count": 1,
        "link_urls": ["https://example.com/details"],
        "key_passage_count": 1,
    }


def _page(url: str, *, link: str | None = None) -> dict:
    link_html = f'<a href="{link}">Next official page</a>' if link else ""
    return {
        "url": url,
        "html": f"<html><title>{url}</title><body><p>Useful public information for testing.</p>{link_html}</body></html>",
    }


def test_fetch_page_tool_schema_is_model_directed() -> None:
    schema = _schema_by_name("fetch_page")["function"]
    assert schema["parameters"]["required"] == ["url"]
    assert set(schema["parameters"]["properties"]) == {"url", "parent_url"}
    assert "does not recursively follow links" in schema["description"]
    assert "at most 8 unique pages" in schema["description"]


def test_run_state_tracks_depth_and_rejects_duplicate_url() -> None:
    root = "https://reg.uci.edu/start/?utm_source=search"
    child = "https://reg.uci.edu/policies/details/"
    deep_search.set_fake_pages([_page(root, link=child), _page(child)])
    state = DeepSearchRunState(query="UCI policy")
    context = {"deep_search_state": state}

    first = agent_tools.dispatch("fetch_page", {"url": root}, context=context)
    second = agent_tools.dispatch(
        "fetch_page",
        {"url": child, "parent_url": root},
        context=context,
    )
    duplicate = agent_tools.dispatch(
        "fetch_page",
        {"url": "https://REG.uci.edu/policies/details?utm_campaign=x#top"},
        context=context,
    )

    assert first["ok"] is True
    assert first["depth"] == 1
    assert second["ok"] is True
    assert second["depth"] == 2
    assert duplicate["ok"] is False
    assert duplicate["error_code"] == "already_visited"
    assert duplicate["visited"]["depth"] == 2
    assert [item["depth"] for item in state.visited_path()] == [1, 2]


def test_run_state_requires_real_parent_link() -> None:
    root = "https://example.com/root"
    unrelated = "https://example.com/not-linked"
    deep_search.set_fake_pages([_page(root), _page(unrelated)])
    state = DeepSearchRunState()
    assert state.fetch(root)["ok"] is True

    result = state.fetch(unrelated, parent_url=root)
    assert result["ok"] is False
    assert result["error_code"] == "parent_link_mismatch"
    assert state.fetch_attempts == 1


def test_page_and_depth_budgets_block_additional_fetches() -> None:
    pages = [_page(f"https://example.com/page-{index}") for index in range(1, 10)]
    deep_search.set_fake_pages(pages)
    page_limited = DeepSearchRunState(max_pages=8)
    for page in pages[:8]:
        assert page_limited.fetch(page["url"])["ok"] is True
    denied = page_limited.fetch(pages[8]["url"])
    assert denied["error_code"] == "deep_search_limit_reached"
    assert denied["budget"]["limit_reason"] == "max_pages"
    assert page_limited.fetch_attempts == 8

    chain = [f"https://uci.edu/depth-{index}" for index in range(1, 10)]
    deep_search.set_fake_pages(
        [_page(url, link=chain[index + 1] if index + 1 < len(chain) else None) for index, url in enumerate(chain)]
    )
    depth_limited = DeepSearchRunState(max_pages=20)
    parent = None
    for expected_depth, url in enumerate(chain[:8], start=1):
        result = depth_limited.fetch(url, parent_url=parent)
        assert result["ok"] is True
        assert result["depth"] == expected_depth
        parent = url
    denied_depth = depth_limited.fetch(chain[8], parent_url=parent)
    assert denied_depth["error_code"] == "deep_search_limit_reached"
    assert denied_depth["budget"]["limit_reason"] == "max_depth"
    assert denied_depth["attempted_depth"] is None


def test_web_search_after_limit_is_fallback_and_cannot_unlock_fetch(monkeypatch) -> None:
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "fake")
    deep_search.set_fake_pages([_page("https://example.com/first")])
    web_search.set_fake_results(
        [{"title": "Another result", "url": "https://example.edu/other", "snippet": "More context"}]
    )
    state = DeepSearchRunState(max_pages=1)
    context = {"user_id": "student_001", "deep_search_state": state}
    assert agent_tools.dispatch(
        "fetch_page", {"url": "https://example.com/first"}, context=context
    )["ok"] is True

    fallback = agent_tools.dispatch(
        "web_search",
        {"query": "another source", "reason": "deep search reached its hard page budget"},
        context=context,
    )
    assert fallback["ok"] is True
    assert fallback["fallback_after_deep_limit"] is True
    assert fallback["fetch_page_allowed"] is False
    assert fallback["results"][0]["depth"] == 0
    assert state.fallback_search_used is True

    still_denied = agent_tools.dispatch(
        "fetch_page", {"url": fallback["results"][0]["url"]}, context=context
    )
    assert still_denied["error_code"] == "deep_search_limit_reached"


def test_agent_dispatches_fetch_page_with_run_scoped_state() -> None:
    url = "https://reg.uci.edu/policy"
    deep_search.set_fake_pages([_page(url)])
    messages = [{"role": "user", "content": "Read the registrar policy page"}]
    client = ScriptedLLMClient(
        tool_response(tool_call("fetch_page", {"url": url}, call_id="call_page")),
        tool_response(tool_call("fetch_page", {"url": f"{url}#again"}, call_id="call_repeat")),
        text_response("The page was checked once."),
    )

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

    assert [event["name"] for event in events if event["type"] == "tool_call_start"] == [
        "fetch_page",
        "fetch_page",
    ]
    first_payload = json.loads(messages[2]["content"])
    repeated_payload = json.loads(messages[4]["content"])
    assert first_payload["ok"] is True
    assert repeated_payload["error_code"] == "already_visited"
    assert events[-1]["text"] == "The page was checked once."


def test_fetch_page_humanized_chip_label() -> None:
    assert agent_tools.humanize_tool_call(
        "fetch_page", {"url": "https://reg.uci.edu/policy"}
    ) == "Reading webpage · https://reg.uci.edu/policy"


def test_restriction_workflow_does_not_delegate_missing_evidence_to_model(
    monkeypatch,
) -> None:
    supplement_url = "https://ics.uci.edu/course-enrollment-restrictions/"
    monkeypatch.setattr(
        agent_tools.websoc_workflow,
        "fetch_websoc_department_restrictions",
        lambda **kwargs: {
            "ok": True,
            "workflow_id": "websoc_department_restrictions",
            "source_url": "https://www.reg.uci.edu/perl/WebSoc",
            "term": kwargs["term"],
            "department": kwargs["department"],
            "links": [{"url": supplement_url}],
        },
    )
    monkeypatch.setattr(
        agent_tools.websoc_workflow,
        "fetch_linked_official_pages",
        lambda _result: {"ok": True, "pages": [], "errors": []},
    )
    messages = [{"role": "user", "content": "Fall 2026 ICS 专业限制什么时候解除？"}]
    client = ScriptedLLMClient(
        tool_response(
            tool_call("fetch_page", {"url": supplement_url}, call_id="call_supplement")
        ),
        text_response(
            "WebSoc and the current ICS page should both be shown when their details differ."
        ),
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

    tool_names = [event["name"] for event in events if event["type"] == "tool_call_start"]
    assert tool_names == ["get_department_restrictions"]
    assert client.calls == []
    final = next(event for event in reversed(events) if event["type"] == "final")
    assert final["deterministic_restriction_answer"] is True
    assert final["restriction_evidence"]["evidence_status"] == "unavailable"
    assert "无法" in final["text"] or "没有足够证据" in final["text"]
