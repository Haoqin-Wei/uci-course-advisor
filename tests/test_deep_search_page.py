from __future__ import annotations

import logging

from app.data import deep_search


def test_normalize_url_removes_tracking_fragment_and_sorts_query() -> None:
    assert deep_search.normalize_url(
        "HTTPS://Example.COM:443/path/?b=2&utm_source=test&a=1#section"
    ) == "https://example.com/path?a=1&b=2"
    assert deep_search.normalize_url("https://example.com") == "https://example.com/"


def test_validate_public_url_rejects_local_and_private_targets() -> None:
    cases = [
        ("file:///etc/passwd", "unsupported_scheme"),
        ("http://localhost/admin", "blocked_host"),
        ("http://127.0.0.1/", "non_public_ip"),
        ("http://169.254.169.254/latest/meta-data", "non_public_ip"),
        ("http://10.0.0.5/internal", "non_public_ip"),
        ("http://[::1]/", "non_public_ip"),
    ]
    for url, error_code in cases:
        result = deep_search.validate_public_url(url)
        assert result["ok"] is False
        assert result["error_code"] == error_code


def test_fetch_fake_page_returns_bounded_structured_content(monkeypatch, caplog) -> None:
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
    caplog.set_level(logging.INFO, logger="app.data.deep_search")
    deep_search.set_fake_pages(
        [
            {
                "url": "https://ics.uci.edu/course-enrollment-restrictions/",
                "html": """
                    <html>
                      <head>
                        <title>ICS Enrollment Restrictions</title>
                        <style>.hidden { display:none }</style>
                        <script>doNotReturnThis()</script>
                      </head>
                      <body>
                        <h1>Undergraduate enrollment updates</h1>
                        <p>Major restrictions for Fall 2026 are removed on August 24 at noon.</p>
                        <p>Students should verify each course in WebSoc before attempting enrollment.</p>
                        <a href="/academics/policies/?utm_source=nav#rules">Student policies</a>
                        <a href="https://www.reddit.com/r/UCI/comments/example">Student discussion</a>
                        <a href="mailto:advisor@uci.edu">Email advising</a>
                      </body>
                    </html>
                """,
            }
        ]
    )

    result = deep_search.fetch_page(
        "https://ics.uci.edu/course-enrollment-restrictions/?utm_campaign=test"
    )

    assert result["ok"] is True
    assert result["title"] == "ICS Enrollment Restrictions"
    assert result["domain"] == "ics.uci.edu"
    assert result["source_class"] == "official_uci"
    assert result["trust_level"] == "high"
    assert "Major restrictions for Fall 2026" in result["summary"]
    assert "doNotReturnThis" not in result["summary"]
    assert result["key_passages"]
    assert [link["domain"] for link in result["links"]] == [
        "ics.uci.edu",
        "reddit.com",
    ]
    assert result["links"][0]["normalized_url"] == "https://ics.uci.edu/academics/policies"
    assert result["links"][0]["source_position"]["html_line"] > 0
    assert result["links"][1]["source_class"] == "reddit"
    assert result["links"][1]["trust_level"] == "low"
    assert "event=deep_search_page_started" in caplog.text
    assert "web_search_url='https://ics.uci.edu/course-enrollment-restrictions/" in caplog.text
    assert "event=deep_search_page_completed" in caplog.text
    assert "title='ICS Enrollment Restrictions'" not in caplog.text
    assert "Major restrictions for Fall 2026 are removed on August 24 at noon" not in caplog.text
    assert "result_urls=" not in caplog.text


def test_live_page_logs_each_request_redirect_response_and_result(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.data.deep_search")
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    start_url = "https://example.com/start"
    final_url = "https://example.com/final"

    class FakeResponse:
        def __init__(self, *, url, status_code, body=b"", location=None):
            self.url = url
            self.status_code = status_code
            self.headers = {"content-type": "text/html"}
            if location:
                self.headers["location"] = location
            self.is_redirect = 300 <= status_code < 400
            self.is_permanent_redirect = status_code in {301, 308}
            self.encoding = "utf-8"
            self._body = body

        def iter_content(self, chunk_size):
            del chunk_size
            yield self._body

    responses = [
        FakeResponse(url=start_url, status_code=302, location="/final"),
        FakeResponse(
            url=final_url,
            status_code=200,
            body=b"<html><title>Final page</title><p>Useful final evidence for the answer.</p></html>",
        ),
    ]

    class FakeSession:
        def get(self, url, **kwargs):
            del url, kwargs
            return responses.pop(0)

    monkeypatch.setattr(deep_search.requests, "Session", FakeSession)
    monkeypatch.setattr(
        deep_search,
        "validate_public_url",
        lambda url, resolve_dns=False: {
            "ok": True,
            "normalized_url": url,
            "domain": "example.com",
        },
    )

    result = deep_search.fetch_page(start_url)

    assert result["ok"] is True
    assert result["final_url"] == final_url
    assert caplog.text.count("event=agent_web_fetch_started") == 2
    assert caplog.text.count("event=agent_web_fetch_completed") == 1
    assert "event=deep_search_page_redirect" in caplog.text
    assert f"final_url={final_url!r}" in caplog.text
    assert "cache_hit=False" in caplog.text
    assert "trigger='agent'" in caplog.text
    assert "content_length=82" in caplog.text
    assert "Useful final evidence for the answer" not in caplog.text


def test_fetch_fake_page_rejects_non_text_and_oversized_content() -> None:
    deep_search.set_fake_pages(
        [
            {
                "url": "https://example.com/document.pdf",
                "content_type": "application/pdf",
                "text": "binary",
            }
        ]
    )
    unsupported = deep_search.fetch_page("https://example.com/document.pdf")
    assert unsupported["ok"] is False
    assert unsupported["error_code"] == "unsupported_content_type"

    deep_search.set_fake_pages(
        [
            {
                "url": "https://example.com/huge",
                "html": "x" * (deep_search.MAX_PAGE_BYTES + 1),
            }
        ]
    )
    oversized = deep_search.fetch_page("https://example.com/huge")
    assert oversized["ok"] is False
    assert oversized["error_code"] == "page_too_large"


def test_fetch_page_missing_fake_stays_offline(monkeypatch, caplog) -> None:
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
    caplog.set_level(logging.WARNING, logger="app.data.deep_search")
    result = deep_search.fetch_page("https://example.com/not-installed")
    assert result["ok"] is False
    assert result["error_code"] == "page_fetch_disabled"
    assert "event=deep_search_page_failed" in caplog.text
    assert "error_code='page_fetch_disabled'" in caplog.text
