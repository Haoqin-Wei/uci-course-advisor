from __future__ import annotations

import asyncio
import os

import pytest
import requests


pytestmark = [pytest.mark.live, pytest.mark.manual]


def test_live_anteater_api_ping():
    response = requests.get(
        "https://anteaterapi.com/v2/rest/ping",
        timeout=15,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload.get("ok") is True


def test_live_deepseek_llm_smoke():
    if not os.environ.get("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY is required for the manual live LLM smoke test")

    from app.llm import adapter

    async def collect_chunks() -> list[str]:
        chunks: list[str] = []
        async for chunk in adapter.stream_answer_llm(
            user_message="Reply with exactly: live-ok",
            retrieved_data={},
            session_state={},
            memory_context=None,
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(collect_chunks())

    assert chunks
    assert any("live" in chunk.lower() for chunk in chunks)
