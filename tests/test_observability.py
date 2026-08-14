from __future__ import annotations

import logging

from app import observability


def test_log_event_keeps_full_search_urls_but_bounds_regular_text(caplog):
    logger = logging.getLogger("tests.search_logging")
    caplog.set_level(logging.INFO, logger=logger.name)
    url = "https://www.reg.uci.edu/perl/WebSoc?" + "department=ART&" * 20

    observability.log_event(
        logger,
        logging.INFO,
        "search_test",
        web_search_url=url,
        summary="x" * 300,
    )

    assert f"web_search_url={url!r}" in caplog.text
    assert "summary='" + "x" * 157 + "...'" in caplog.text


def test_log_event_bounds_nested_collections(caplog):
    logger = logging.getLogger("tests.collection_logging")
    caplog.set_level(logging.INFO, logger=logger.name)

    observability.log_event(
        logger,
        logging.INFO,
        "collection_test",
        result_urls=[f"https://example.com/{index}" for index in range(25)],
        details={f"key_{index}": "x" * 300 for index in range(15)},
    )

    assert "... (15 more)" in caplog.text
    assert "_truncated_items" in caplog.text
    assert "x" * 200 not in caplog.text


def test_health_live_returns_trace_id(app_client):
    response = app_client.get("/health/live", headers={"X-Trace-Id": "trace-test"})

    assert response.status_code == 200
    assert response.headers["x-trace-id"] == "trace-test"
    assert response.json() == {
        "status": "live",
        "trace_id": "trace-test",
    }


def test_health_ready_checks_catalog_and_memory(app_client):
    response = app_client.get("/health/ready", headers={"X-Trace-Id": "ready-test"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["trace_id"] == "ready-test"
    assert body["checks"]["catalog"]["ok"] is True
    assert body["checks"]["catalog"]["terms"] >= 1
    assert body["checks"]["memory"]["ok"] is True
    assert body["checks"]["term_state"]["ok"] is True
    assert body["checks"]["term_state"]["automatic_term"] == "2025 Spring"
    assert body["checks"]["term_state"]["fallback"] is False


def test_health_metrics_exposes_in_process_counters(app_client):
    app_client.get("/health/live", headers={"X-Trace-Id": "metrics-seed"})

    response = app_client.get("/health/metrics", headers={"X-Trace-Id": "metrics-test"})

    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"] == "metrics-test"
    assert "http.requests{method=GET,route=/health/live,status=200}" in body[
        "metrics"
    ]["counters"]
    assert body["term_state"]["automatic_term"] == "2025 Spring"
    assert body["term_state"]["availability"]["section_count"] == 1


def test_timing_metrics_include_percentiles():
    observability.clear_metrics()
    for value in (10, 20, 30, 40, 100):
        observability.observe_ms("agent.total_ms", value)

    timing = observability.snapshot_metrics()["timings"]["agent.total_ms"]

    assert timing["p50_ms"] == 30
    assert timing["p95_ms"] == 100
    assert timing["p99_ms"] == 100
