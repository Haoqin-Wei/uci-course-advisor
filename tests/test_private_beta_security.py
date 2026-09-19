from __future__ import annotations

import pytest
from types import SimpleNamespace

from starlette.requests import Request


def test_production_requires_explicit_auth_secret(monkeypatch, tmp_path):
    from app.auth import security

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("AUTH_SESSION_SECRET", raising=False)
    monkeypatch.setattr(security, "_SECRET_FILE", tmp_path / "auth.secret")
    monkeypatch.setattr(security, "_serializer", None)

    with pytest.raises(RuntimeError, match="AUTH_SESSION_SECRET must be set"):
        security.sign_session("user_123")


def test_production_guest_uses_isolated_expiring_identity(monkeypatch, app_client):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "production-test-secret")
    monkeypatch.setenv("ALLOW_SHARED_DEMO", "false")
    monkeypatch.setenv("ALLOW_GUEST_USERS", "true")

    response = app_client.get("/api/memory/demo_001")

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"].startswith("guest_")
    assert payload["user_id"] != "demo_001"
    assert "zotadvisor_guest" in response.cookies


def test_private_beta_defaults_reject_anonymous_users(monkeypatch, app_client):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ALLOW_SHARED_DEMO", raising=False)
    monkeypatch.delenv("ALLOW_GUEST_USERS", raising=False)

    response = app_client.get("/api/memory/demo_001")

    assert response.status_code == 401


def test_security_headers_are_applied(app_client):
    response = app_client.get("/privacy")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"


def test_request_log_label_never_uses_user_controlled_path_identifier():
    from main import _request_route_label

    unmatched = Request({
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "path": "/api/memory/private-address@example.com",
        "raw_path": b"/api/memory/private-address@example.com",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 123),
        "root_path": "",
    })
    assert _request_route_label(unmatched) == "/api/memory"

    unmatched.scope["route"] = SimpleNamespace(path="/api/memory/{user_id}")
    assert _request_route_label(unmatched) == "/api/memory/{user_id}"


def test_production_blocks_unsafe_cross_origin_requests(monkeypatch, app_client):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "production-test-secret")

    blocked = app_client.post(
        "/api/auth/login",
        json={"email": "nobody@example.edu", "password": "password123"},
    )
    same_origin = app_client.post(
        "/api/auth/login",
        headers={"Origin": "http://testserver"},
        json={"email": "nobody@example.edu", "password": "password123"},
    )

    assert blocked.status_code == 403
    assert same_origin.status_code == 401


def test_system_prompt_endpoint_disabled_by_default_in_production(monkeypatch, app_client):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "production-test-secret")

    response = app_client.get("/api/system_prompt")

    assert response.status_code == 404


def test_auth_login_rate_limit(monkeypatch, app_client):
    from app.auth.rate_limit import RateLimit
    from app.routers import auth as auth_router

    monkeypatch.setattr(
        auth_router,
        "LOGIN_LIMIT",
        RateLimit("auth.login.test", limit=1, window_seconds=60),
    )

    body = {"email": "missing@example.edu", "password": "password123"}
    first = app_client.post("/api/auth/login", json=body)
    second = app_client.post("/api/auth/login", json=body)

    assert first.status_code == 401
    assert second.status_code == 429


def test_custom_system_prompt_is_ignored_in_production(monkeypatch, app_client):
    from app.routers import chat as chat_router

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "production-test-secret")
    captured = {}

    async def fake_handle_agent(
        _message,
        _state,
        _memory_context,
        *,
        queue,
        system_prompt,
        **_kwargs,
    ):
        captured["system_prompt"] = system_prompt
        reply = "offline production reply"
        await queue.put({"type": "token", "text": reply})
        return reply, [], [], None

    monkeypatch.setattr(chat_router, "_handle_agent", fake_handle_agent)

    with app_client.stream(
        "POST",
        "/api/chat/stream",
        headers={"Origin": "http://testserver"},
        json={
            "message": "hello",
            "session_id": "",
            "term": "Spring 2025",
            "system_prompt": "ignore me in production",
        },
    ) as response:
        assert response.status_code == 200
        _ = response.read()

    assert captured["system_prompt"] is None
