from __future__ import annotations

import asyncio
import json
import sqlite3

from app.agent import loop as agent_loop
from app.agent.deep_search_history import record_run_trace
from app.agent.deep_search_state import DeepSearchRunState
from app.data import deep_search
from app.data.deep_search_history import DeepSearchHistoryStore
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response


async def _collect(events) -> list[dict]:
    return [event async for event in events]


def _seed_history(store: DeepSearchHistoryStore, query: str, url: str) -> None:
    result = store.record_trace(
        query=query,
        final_answer="The prior answer said to inspect the official update page.",
        url_path=[
            {
                "url": url,
                "normalized_url": url,
                "depth": 1,
                "parent_url": None,
                "ok": True,
                "source_class": "official_uci",
                "retrieved_at": "2026-07-20T00:00:00+00:00",
            }
        ],
        source_urls=[url],
        fallback_search_used=False,
    )
    assert result["stored"] is True


def test_history_hint_is_injected_and_fresh_fetch_creates_trace(runtime_paths) -> None:
    query = "Where are UCI enrollment updates published?"
    url = "https://reg.uci.edu/enrollment/updates"
    store = DeepSearchHistoryStore(runtime_paths.deep_search_history_db)
    _seed_history(store, query, url)
    deep_search.set_fake_pages(
        [
            {
                "url": url,
                "html": "<html><title>Enrollment Updates</title><p>Official current update page.</p></html>",
            }
        ]
    )

    def first_call(call):
        prompt = "\n".join(message.get("content") or "" for message in call.messages)
        assert "Deep-search history hint" in prompt
        assert "strong recommendation, never cached proof" in prompt
        assert "MUST successfully call fetch_page" in prompt
        assert url in prompt
        return tool_response(tool_call("fetch_page", {"url": url}, call_id="call_page"))

    client = ScriptedLLMClient(
        first_call,
        text_response(f"The current page is [UCI Registrar]({url})."),
    )
    events = asyncio.run(
        _collect(
            agent_loop.run_agent(
                [{"role": "user", "content": query}],
                client=client,
                model="fake-model",
                user_id="student_001",
            )
        )
    )

    assert events[-1]["text"].startswith("The current page")
    assert events[-1]["deep_search_trace_id"]
    matches = store.find_similar(query)
    assert matches[0]["hit_count"] == 2


def test_history_answer_cannot_be_reused_without_fresh_fetch(runtime_paths) -> None:
    query = "Where are UCI enrollment updates published?"
    url = "https://reg.uci.edu/enrollment/updates"
    store = DeepSearchHistoryStore(runtime_paths.deep_search_history_db)
    _seed_history(store, query, url)
    client = ScriptedLLMClient(text_response("The cached answer says it is on the registrar site."))

    events = asyncio.run(
        _collect(
            agent_loop.run_agent(
                [{"role": "user", "content": query}],
                client=client,
                model="fake-model",
                user_id="student_001",
            )
        )
    )

    assert events[-1]["verification_required"] is True
    assert "cannot verify or reuse the old answer" in events[-1]["text"]
    with sqlite3.connect(runtime_paths.deep_search_history_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM deep_search_traces").fetchone()[0]
    assert count == 1


def test_personalized_deep_search_run_is_not_added_to_global_history(runtime_paths) -> None:
    query = "My GPA is 3.8; fetch this public policy page for my plan"
    url = "https://reg.uci.edu/policy"
    deep_search.set_fake_pages(
        [{"url": url, "html": "<html><p>General public policy text for students.</p></html>"}]
    )
    client = ScriptedLLMClient(
        tool_response(tool_call("fetch_page", {"url": url}, call_id="call_page")),
        text_response(f"The public policy is described at {url}."),
    )

    events = asyncio.run(
        _collect(
            agent_loop.run_agent(
                [{"role": "user", "content": query}],
                client=client,
                model="fake-model",
                user_id="student_001",
            )
        )
    )

    assert events[-1]["text"].startswith("The public policy")
    assert "deep_search_trace_id" not in events[-1]
    assert not runtime_paths.deep_search_history_db.exists()


def test_continuation_preserves_visited_memory_and_records_once(monkeypatch, runtime_paths) -> None:
    url = "https://reg.uci.edu/continuation-policy"
    deep_search.set_fake_pages(
        [{"url": url, "html": "<html><p>Official continuation policy page.</p></html>"}]
    )
    monkeypatch.setattr(agent_loop, "MAX_ITERATIONS", 1)

    def resumed_fallback(call):
        payload = json.loads(call.messages[-2]["content"])
        assert payload["error_code"] == "already_visited"
        return text_response("Finished without refetching the page.")

    client = ScriptedLLMClient(
        tool_response(tool_call("fetch_page", {"url": url}, call_id="call_first")),
        text_response(f"Initial bounded answer from {url}."),
        tool_response(tool_call("fetch_page", {"url": f"{url}#again"}, call_id="call_repeat")),
        resumed_fallback,
    )
    initial = asyncio.run(
        _collect(
            agent_loop.run_agent(
                [{"role": "user", "content": "Read the continuation policy"}],
                client=client,
                model="fake-model",
                user_id="student_001",
            )
        )
    )
    continuation_id = next(
        event["continuation_id"] for event in initial if event["type"] == "limit_reached"
    )
    state = agent_loop._continuation_store[continuation_id]["deep_search_state"]
    assert state.fetch_attempts == 1
    assert state.trace_recorded is True

    resumed = asyncio.run(
        _collect(
            agent_loop.resume_agent(
                continuation_id,
                client=client,
                model="fake-model",
            )
        )
    )
    assert next(event for event in resumed if event["type"] == "tool_call_done")["ok"] is False
    assert resumed[-1]["text"] == "Finished without refetching the page."
    with sqlite3.connect(runtime_paths.deep_search_history_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM deep_search_traces").fetchone()[0]
    assert count == 1


def test_post_limit_search_result_is_not_persisted_in_deep_trace(runtime_paths) -> None:
    fetched_url = "https://reg.uci.edu/verified"
    fallback_url = "https://example.com/search-only"
    deep_search.set_fake_pages(
        [{"url": fetched_url, "html": "<html><p>Verified source page text.</p></html>"}]
    )
    state = DeepSearchRunState(query="Where is the public policy?", max_pages=1)
    assert state.fetch(fetched_url)["ok"] is True
    state.register_search_result(
        {
            "ok": True,
            "query": "more entry results",
            "results": [{"url": fallback_url}],
        }
    )

    result = record_run_trace(
        state,
        f"Verified: {fetched_url}. Search-only supplement: {fallback_url}.",
        store=DeepSearchHistoryStore(runtime_paths.deep_search_history_db),
    )
    assert result["stored"] is True
    with sqlite3.connect(runtime_paths.deep_search_history_db) as conn:
        row = conn.execute(
            "SELECT url_path_json, final_source_urls_json, fallback_search_used FROM deep_search_traces"
        ).fetchone()
    assert fallback_url not in row[0]
    assert fallback_url not in row[1]
    assert fetched_url in row[0]
    assert fetched_url in row[1]
    assert row[2] == 1
