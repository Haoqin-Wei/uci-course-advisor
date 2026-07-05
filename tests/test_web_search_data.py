from __future__ import annotations

import pytest

from app import observability
from app.data import web_search


@pytest.mark.parametrize(
    ("url", "source_class", "trust_level"),
    [
        ("https://uci.edu", "official_uci", "high"),
        ("https://catalogue.uci.edu/allcourses/compsci/", "official_uci", "high"),
        ("https://reg.uci.edu/calendars/quarterly/2025-2026/quarterly25-26.html", "official_uci", "high"),
        ("https://www.ratemyprofessors.com/professor/123", "rmp", "medium"),
        ("https://www.reddit.com/r/UCI/comments/example", "reddit", "low"),
        ("https://unknown.invalid/page", "unknown", "low"),
    ],
)
def test_source_classifier_expected_domains(url: str, source_class: str, trust_level: str) -> None:
    result = web_search.classify_url(url)

    assert result["source_class"] == source_class
    assert result["trust_level"] == trust_level


def test_source_classifier_rejects_missing_url_as_fact_source() -> None:
    result = web_search.classify_url(None)

    assert result["source_class"] == "unknown"
    assert result["usable_as_fact"] is False
    assert "missing" in result["why_trusted_or_not"]


def test_search_disabled_returns_structured_unavailable() -> None:
    result = web_search.search_web(
        query="UCI add drop deadline",
        reason="user explicitly asked to look up the latest deadline",
        subject="student_001",
    )

    assert result["ok"] is False
    assert result["error_code"] == "web_search_disabled"
    assert result["query"] == "UCI add drop deadline"
    assert result["reason"] == "user explicitly asked to look up the latest deadline"
    assert result["results"] == []


def test_fake_provider_returns_normalized_limited_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "fake")
    long_snippet = "x" * 700
    web_search.set_fake_results(
        [
            {
                "title": "UCI Registrar — Quarterly Academic Calendar",
                "url": "https://reg.uci.edu/calendars/quarterly/2025-2026/quarterly25-26.html",
                "snippet": long_snippet,
            },
            {
                "title": "Reddit anecdote",
                "url": "https://reddit.com/r/UCI/comments/example",
                "snippet": "student anecdote",
            },
            {
                "title": "Missing URL row",
                "snippet": "no URL cannot be factual evidence",
            },
        ]
    )

    result = web_search.search_web(
        query="UCI add drop deadline",
        reason="local policy data may be stale for a recent deadline question",
        preferred_domains=["reg.uci.edu"],
        max_results=5,
        subject="student_001",
    )

    assert result["ok"] is True
    assert result["provider"] == "fake"
    assert result["max_results"] == 5
    assert result["preferred_domains"] == ["reg.uci.edu"]
    assert len(result["results"]) == 1

    first = result["results"][0]
    assert first["source_class"] == "official_uci"
    assert first["trust_level"] == "high"
    assert first["domain"] == "reg.uci.edu"
    assert first["retrieved_at"] == result["searched_at"]
    assert first["usable_as_fact"] is True
    assert len(first["snippet"]) <= web_search.SNIPPET_MAX_CHARS

    metrics = observability.snapshot_metrics()
    assert metrics["counters"]["web_search.source_class{source_class=official_uci}"] == 1


def test_fake_provider_marks_no_url_result_unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "fake")
    web_search.set_fake_results(
        [{"title": "No URL", "snippet": "claim without URL"}]
    )

    result = web_search.search_web(
        query="professor announcement",
        reason="local DB missing an external professor page",
        subject="student_001",
    )

    assert result["ok"] is True
    assert result["results"][0]["url"] is None
    assert result["results"][0]["source_class"] == "unknown"
    assert result["results"][0]["usable_as_fact"] is False


def test_search_rate_limit_returns_structured_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "fake")

    for i in range(20):
        result = web_search.search_web(
            query=f"UCI query {i}",
            reason="user explicitly requested repeated web search",
            subject="rate_limited_student",
        )
        assert result["ok"] is True

    limited = web_search.search_web(
        query="UCI query 21",
        reason="user explicitly requested repeated web search",
        subject="rate_limited_student",
    )

    assert limited["ok"] is False
    assert limited["error_code"] == "rate_limited"
    assert limited["retry_after_seconds"] >= 1
