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
