from __future__ import annotations

import json

import pytest
import requests


def test_legacy_terms_endpoint_is_removed(app_client):
    response = app_client.get("/api/terms")

    assert response.status_code == 404


def test_term_state_endpoint_returns_backend_automatic_context(app_client):
    response = app_client.get("/api/term-state")

    assert response.status_code == 200
    payload = response.json()
    assert payload["automatic_term"] == "2025 Spring"
    assert payload["source"] == "anteater"
    assert payload["status"] == "fresh"
    assert set(payload) == {"automatic_term", "source", "status"}


def test_frontend_and_static_assets_force_cache_revalidation(app_client):
    page = app_client.get("/")
    asset = app_client.get("/static/js/api-client.js?v=20260723-1")

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store, max-age=0"
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "no-cache, must-revalidate"


def test_session_api_ignores_term_body_and_returns_default_context(app_client):
    created = app_client.post(
        "/api/sessions/demo_001",
        json={"title": "Read-only term", "term_scope": "2099 Fall"},
    )

    assert created.status_code == 200
    payload = created.json()
    assert "term_scope" not in payload
    assert payload["default_term"] == "2025 Spring"
    assert payload["term_mode"] == "auto"
    assert payload["term_source"] == "anteater"

    restored = app_client.get(
        f"/api/sessions/demo_001/{payload['session_id']}?include_turns=false"
    )
    assert restored.status_code == 200
    assert restored.json()["default_term"] == "2025 Spring"

    listed = app_client.get("/api/sessions/demo_001")
    assert listed.status_code == 200
    listed_session = next(
        item
        for item in listed.json()["sessions"]
        if item["session_id"] == payload["session_id"]
    )
    assert listed_session["default_term"] == "2025 Spring"
    assert listed_session["term_mode"] == "auto"


def test_default_term_api_sets_manual_and_restores_auto(app_client, runtime_paths):
    from app.data import sessions
    from app.terms.store import JsonFileTermStateStore

    store = JsonFileTermStateStore(runtime_paths.term_state)
    state = store.load()
    state.websoc_terms.append("2026 Fall")
    state.availability["2026 Fall"] = {
        "available": True,
        "course_count": 1,
        "section_count": 1,
    }
    store.save(state)
    session_id = sessions.create_session("demo_001", title="Manual term")

    selected = app_client.put(
        f"/api/sessions/{session_id}/default-term",
        json={"mode": "manual", "term": "Fall 2026"},
    )
    assert selected.status_code == 200
    assert selected.json()["default_term"] == "2026 Fall"
    assert selected.json()["term_mode"] == "manual"
    assert selected.json()["term_source"] == "user_ui"

    restored = app_client.put(
        f"/api/sessions/{session_id}/default-term",
        json={"mode": "auto", "term": "2099 Fall"},
    )
    assert restored.status_code == 200
    assert restored.json()["default_term"] == "2025 Spring"
    assert restored.json()["term_mode"] == "auto"


def test_default_term_api_rejects_unavailable_without_mutation(app_client):
    from app.data import sessions

    session_id = sessions.create_session("demo_001", title="Stable term")
    before = sessions.get_session_meta("demo_001", session_id)
    response = app_client.put(
        f"/api/sessions/{session_id}/default-term",
        json={"mode": "manual", "term": "2099 Fall"},
    )
    assert response.status_code == 422
    after = sessions.get_session_meta("demo_001", session_id)
    assert after == before


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
