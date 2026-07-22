from __future__ import annotations

import json

import pytest
import requests


def test_terms_endpoint_uses_local_catalog(app_client):
    response = app_client.get("/api/terms")

    assert response.status_code == 200
    payload = response.json()
    assert payload["terms"]
    term_names = [term["name"] for term in payload["terms"]]
    assert payload["default"] in term_names
    assert payload["default"] == "Spring 2026"

    fall_2026 = next(term for term in payload["terms"] if term["name"] == "Fall 2026")
    spring_2026 = next(term for term in payload["terms"] if term["name"] == "Spring 2026")
    assert fall_2026["coverage_status"] == "partial"
    assert fall_2026["section_count"] == 452
    assert fall_2026["department_count"] > 0
    assert spring_2026["coverage_status"] == "complete"
    assert payload["manifest"]["schema_version"] == "uci-relational-v1"


def test_term_state_endpoint_returns_backend_automatic_context(app_client):
    response = app_client.get("/api/term-state")

    assert response.status_code == 200
    payload = response.json()
    assert payload["automatic_term"] == "2025 Spring"
    assert payload["source"] == "anteater"
    assert payload["status"] == "fresh"
    assert payload["last_success_at"]
    assert payload["next_cutoff"].startswith("2025-04-11T17:00:00")
    assert payload["fallback"] is False


def test_session_api_ignores_term_body_and_returns_resolved_context(app_client):
    created = app_client.post(
        "/api/sessions/demo_001",
        json={"title": "Read-only term", "term_scope": "2099 Fall"},
    )

    assert created.status_code == 200
    payload = created.json()
    assert payload["term_scope"] is None
    assert payload["effective_term"] == "2025 Spring"
    assert payload["term_mode"] == "auto"
    assert payload["term_source"] == "anteater"

    restored = app_client.get(
        f"/api/sessions/demo_001/{payload['session_id']}?include_turns=false"
    )
    assert restored.status_code == 200
    assert restored.json()["effective_term"] == "2025 Spring"

    listed = app_client.get("/api/sessions/demo_001")
    assert listed.status_code == 200
    listed_session = next(
        item
        for item in listed.json()["sessions"]
        if item["session_id"] == payload["session_id"]
    )
    assert listed_session["effective_term"] == "2025 Spring"
    assert listed_session["term_mode"] == "auto"


def test_session_api_resolves_pinned_conversation_term(app_client):
    from app.data import sessions

    session_id = sessions.create_session("demo_001", title="Pinned term")
    sessions.update_session_meta(
        "demo_001",
        session_id,
        term_scope="2026 Fall",
        term_mode="pinned",
        term_source="explicit",
    )

    response = app_client.get(
        f"/api/sessions/demo_001/{session_id}?include_turns=false"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["effective_term"] == "2026 Fall"
    assert payload["term_mode"] == "pinned"
    assert payload["term_source"] == "conversation_pinned"


def test_non_streaming_chat_endpoint_is_removed(app_client):
    response = app_client.post(
        "/api/chat",
        json={"message": "hello", "session_id": "", "term": "Spring 2026"},
    )

    assert response.status_code == 404


def test_profile_write_uses_temporary_runtime(app_client, runtime_paths):
    response = app_client.post(
        "/api/memory/path-value-is-ignored/profile",
        json={"major": "Computer Science"},
    )

    assert response.status_code == 200
    profile_path = runtime_paths.memory_root / "demo_001" / "profile.json"
    assert profile_path.exists()
    assert json.loads(profile_path.read_text(encoding="utf-8")) == {
        "major": "Computer Science"
    }


def test_external_network_is_blocked_by_default():
    with pytest.raises(AssertionError, match="External network access is disabled"):
        requests.get("https://example.com")


def test_real_llm_client_is_blocked_by_default():
    from app.llm import adapter

    assert adapter.LLM_ENABLED is False
    with pytest.raises(AssertionError, match="Real LLM access is disabled"):
        adapter._get_client()
