from __future__ import annotations

import json
from pathlib import Path


def _create_user(email: str) -> dict:
    from app.auth import store

    return store.create_user(email, password_hash="not-used-in-ownership-tests")


def _login_as(client, user_id: str) -> None:
    from app.auth import security

    client.cookies.set(
        security.SESSION_COOKIE_NAME,
        security.sign_session(user_id),
    )


def _write_memory_profile(memory_root: Path, user_id: str, profile: dict) -> None:
    user_dir = memory_root / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "profile.json").write_text(
        json.dumps(profile, ensure_ascii=False),
        encoding="utf-8",
    )
    (user_dir / "facts.json").write_text("[]", encoding="utf-8")
    (user_dir / "preferences.json").write_text("[]", encoding="utf-8")


def test_authenticated_memory_routes_ignore_path_user_id(
    app_client,
    runtime_paths,
):
    alice = _create_user("alice@example.edu")
    bob = _create_user("bob@example.edu")
    _write_memory_profile(
        runtime_paths.memory_root,
        alice["id"],
        {"major": "Computer Science", "selected_courses": ["ICS33"]},
    )
    _write_memory_profile(
        runtime_paths.memory_root,
        bob["id"],
        {"major": "Data Science", "selected_courses": ["STATS67"]},
    )

    _login_as(app_client, alice["id"])
    response = app_client.get(f"/api/memory/{bob['id']}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == alice["id"]
    assert payload["profile"]["major"] == "Computer Science"
    assert payload["profile"]["selected_courses"] == ["ICS33"]
    assert payload["profile"]["major"] != "Data Science"


def test_authenticated_session_routes_ignore_path_user_id_and_enforce_owner(
    app_client,
):
    from app.data import sessions

    alice = _create_user("alice-session@example.edu")
    bob = _create_user("bob-session@example.edu")

    alice_session = sessions.create_session(
        alice["id"],
        title="Alice plan",
        term_scope="Spring 2025",
    )
    sessions.append_turn(alice["id"], alice_session, "user", "Alice private turn")

    bob_session = sessions.create_session(
        bob["id"],
        title="Bob plan",
        term_scope="Fall 2025",
    )
    sessions.append_turn(bob["id"], bob_session, "user", "Bob private turn")

    _login_as(app_client, alice["id"])

    list_response = app_client.get(f"/api/sessions/{bob['id']}")
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["user_id"] == alice["id"]
    assert [item["session_id"] for item in list_payload["sessions"]] == [
        alice_session
    ]

    own_response = app_client.get(
        f"/api/sessions/{bob['id']}/{alice_session}",
    )
    assert own_response.status_code == 200
    own_payload = own_response.json()
    assert own_payload["user_id"] == alice["id"]
    assert own_payload["session_id"] == alice_session
    assert own_payload["turns"][0]["content"] == "Alice private turn"

    cross_owner_response = app_client.get(
        f"/api/sessions/{bob['id']}/{bob_session}",
    )
    assert cross_owner_response.status_code == 404
    assert "Session not found" in cross_owner_response.json()["detail"]


def test_chat_request_model_has_no_student_id_identity_field():
    from app.routers.chat import ChatRequest

    fields = (
        ChatRequest.model_fields
        if hasattr(ChatRequest, "model_fields")
        else ChatRequest.__fields__
    )

    assert "student_id" not in fields


def test_authenticated_chat_ignores_body_student_id_and_uses_cookie_user(
    app_client,
    runtime_paths,
    monkeypatch,
):
    from app.routers import chat as chat_router

    alice = _create_user("alice-chat@example.edu")
    bob = _create_user("bob-chat@example.edu")
    _write_memory_profile(
        runtime_paths.memory_root,
        alice["id"],
        {"major": "Computer Science", "selected_courses": ["ICS33"]},
    )
    _write_memory_profile(
        runtime_paths.memory_root,
        bob["id"],
        {"major": "Data Science", "selected_courses": ["STATS67"]},
    )

    async def fake_extract_info(_message: str) -> dict:
        return {}

    async def fake_classify_intent(_message: str) -> dict:
        return {
            "intent": "single_query",
            "confidence": 1.0,
            "entities": {},
            "source": "test",
        }

    async def fake_handle_single_query(*_args, **_kwargs):
        return "offline reply", [], []

    monkeypatch.setattr(chat_router, "extract_info_from_message", fake_extract_info)
    monkeypatch.setattr(chat_router, "classify_intent", fake_classify_intent)
    monkeypatch.setattr(chat_router, "_handle_single_query", fake_handle_single_query)

    _login_as(app_client, alice["id"])
    response = app_client.post(
        "/api/chat",
        json={
            "message": "What should I take?",
            "session_id": "body_spoof_chat",
            "student_id": bob["id"],
            "term": "Spring 2025",
        },
    )

    assert response.status_code == 200
    state = response.json()["session_state"]
    assert state["major"] == "Computer Science"
    assert state["selected_courses"] == ["ICS33"]
    assert state["major"] != "Data Science"


def test_end_session_uses_cookie_user_not_body_student_id(
    app_client,
    runtime_paths,
):
    from app.modules import state as state_module

    alice = _create_user("alice-end-session@example.edu")
    bob = _create_user("bob-end-session@example.edu")
    session_id = "end_body_spoof"
    state_module.add_message(session_id, "user", "Alice private question")
    state_module.add_message(session_id, "assistant", "Alice private answer")

    _login_as(app_client, alice["id"])
    response = app_client.post(
        "/api/session/end",
        json={
            "session_id": session_id,
            "student_id": bob["id"],
        },
    )

    assert response.status_code == 200
    assert response.json()["messages_archived"] == 2
    assert (
        runtime_paths.memory_root
        / alice["id"]
        / "sessions"
        / f"{session_id}.json"
    ).exists()
    assert not (
        runtime_paths.memory_root
        / bob["id"]
        / "sessions"
        / f"{session_id}.json"
    ).exists()
