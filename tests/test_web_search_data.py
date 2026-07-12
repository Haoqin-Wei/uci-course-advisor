from __future__ import annotations

import logging

import pytest
import requests

from app import config
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


def test_web_search_defaults_enabled_for_dev_disabled_for_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_SEARCH_ENABLED", raising=False)
    monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)

    monkeypatch.setenv("APP_ENV", "development")
    assert config.web_search_enabled() is True
    assert config.web_search_provider() == "duckduckgo"

    monkeypatch.setenv("APP_ENV", "production")
    assert config.web_search_enabled() is False
    assert config.web_search_provider() == "disabled"


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


def test_web_search_logs_started_provider_attempt_results_and_completion(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.data.web_search")
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "fake")
    web_search.set_fake_results(
        [
            {
                "title": "UCI Registrar — Quarterly Academic Calendar",
                "url": "https://reg.uci.edu/calendars/quarterly/2025-2026/quarterly25-26.html",
                "snippet": "Official registrar calendar result.",
            }
        ]
    )

    result = web_search.search_web(
        query="UCI add drop deadline",
        reason="user explicitly asked to search official UCI pages",
        preferred_domains=["reg.uci.edu"],
        subject="student_001",
    )

    assert result["ok"] is True
    assert "event=web_search_started" in caplog.text
    assert "event=web_search_provider_attempt_parsed" in caplog.text
    assert "provider='fake'" in caplog.text
    assert "event=web_search_result" in caplog.text
    assert "rank=1" in caplog.text
    assert "url='https://reg.uci.edu/calendars/quarterly/2025-2026/quarterly25-26.html'" in caplog.text
    assert "title='UCI Registrar — Quarterly Academic Calendar'" in caplog.text
    assert "snippet='Official registrar calendar result.'" in caplog.text
    assert "source_class='official_uci'" in caplog.text
    assert "event=web_search_completed" in caplog.text


def test_duckduckgo_provider_parses_html_without_real_network(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.data.web_search")
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "duckduckgo")

    class FakeResponse:
        status_code = 200
        text = """
        <html><body>
          <a rel="nofollow" class="result__a"
             href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fics.uci.edu%2Fstudent-affairs%2F">
             UCI ICS Student Affairs
          </a>
          <a class="result__snippet"
             href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fics.uci.edu%2Fstudent-affairs%2F">
             Information about course restrictions and enrollment.
          </a>
        </body></html>
        """

        def raise_for_status(self) -> None:
            return None

    def fake_request(self, method, url, **kwargs):
        assert method == "GET"
        assert url == web_search.DUCKDUCKGO_HTML_URL
        assert "site:ics.uci.edu" in kwargs["params"]["q"]
        assert kwargs["timeout"] == config.web_search_timeout_seconds()
        return FakeResponse()

    monkeypatch.setattr(requests.sessions.Session, "request", fake_request)

    result = web_search.search_web(
        query="ICS major restriction release date 2026",
        reason="user explicitly asked to search current ICS restriction information",
        preferred_domains=["ics.uci.edu"],
        subject="student_001",
    )

    assert result["ok"] is True
    assert result["provider"] == "duckduckgo"
    assert result["results"] == [
        {
            "title": "UCI ICS Student Affairs",
            "url": "https://ics.uci.edu/student-affairs/",
            "domain": "ics.uci.edu",
            "snippet": "Information about course restrictions and enrollment.",
            "published_at": None,
            "retrieved_at": result["searched_at"],
            "source_class": "official_uci",
            "trust_level": "high",
            "why_trusted_or_not": "UCI official domain",
            "usable_as_fact": True,
        }
    ]
    expected_preview = FakeResponse.text.replace("\n", " ")[:100]
    assert "event=web_search_content" in caplog.text
    assert f"content_preview={expected_preview!r}" in caplog.text


def test_duckduckgo_provider_falls_back_when_domain_hint_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.data.web_search")
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "duckduckgo")
    calls: list[str] = []

    class Status202Response:
        status_code = 202
        text = "<html></html>"

        def raise_for_status(self) -> None:
            return None

    class FakeResponse:
        status_code = 200
        text = """
        <html><body>
          <a rel="nofollow" class="result-link"
             href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fics.uci.edu%2Fcourse-enrollment-restrictions%2F">
             Enrollment Windows and Restrictions for ICS courses
          </a>
          <td class="result-snippet">Fall 2026 restriction dates are subject to change.</td>
          <span class="timestamp">2026-05-01T00:00:00.0000000</span>
        </body></html>
        """

        def raise_for_status(self) -> None:
            return None

    def fake_request(self, method, url, **kwargs):
        calls.append(kwargs["params"]["q"])
        return Status202Response() if len(calls) == 1 else FakeResponse()

    monkeypatch.setattr(requests.sessions.Session, "request", fake_request)

    result = web_search.search_web(
        query="UCI ICS major restriction lifting date 2026",
        reason="user explicitly asked to search current ICS restriction information",
        preferred_domains=["ics.uci.edu", "reg.uci.edu"],
        subject="student_001",
    )

    assert result["ok"] is True
    assert "site:ics.uci.edu" in calls[0]
    assert calls[1] == "UCI ICS major restriction lifting date 2026"
    assert result["results"][0]["url"] == "https://ics.uci.edu/course-enrollment-restrictions/"
    assert result["results"][0]["published_at"] == "2026-05-01T00:00:00.0000000"
    assert "event=web_search_provider_attempt" in caplog.text
    assert "event=web_search_provider_attempt_failed" in caplog.text
    assert "status_code=202" in caplog.text
    assert "attempt=2" in caplog.text
    assert "event=web_search_provider_attempt_parsed" in caplog.text


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


def test_duckduckgo_provider_network_error_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "duckduckgo")

    def fake_request(self, method, url, **kwargs):
        raise requests.Timeout("simulated timeout")

    monkeypatch.setattr(requests.sessions.Session, "request", fake_request)

    result = web_search.search_web(
        query="UCI current deadline",
        reason="user explicitly asked to search the web",
        subject="student_001",
    )

    assert result["ok"] is False
    assert result["error_code"] == "provider_network_error"
