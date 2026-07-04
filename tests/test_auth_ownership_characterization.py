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

    captured = {}

    async def fake_handle_agent(
        _message,
        state,
        _memory_context,
        *,
        queue,
        **_kwargs,
    ):
        captured["state"] = state
        reply = "offline reply"
        await queue.put({"type": "token", "text": reply})
        return reply, [], [], None

    monkeypatch.setattr(chat_router, "_handle_agent", fake_handle_agent)

    _login_as(app_client, alice["id"])
    with app_client.stream(
        "POST",
        "/api/chat/stream",
        json={
            "message": "What should I take?",
            "session_id": "",
            "student_id": bob["id"],
            "term": "Spring 2025",
        },
    ) as response:
        assert response.status_code == 200
        _ = response.read()

    state = captured["state"]
    assert state["major"] == "Computer Science"
    assert state["selected_courses"] == ["ICS33"]
    assert state["major"] != "Data Science"


def test_end_session_uses_cookie_user_without_archiving_duplicate_history(
    app_client,
    monkeypatch,
    runtime_paths,
):
    from app.routers import chat as chat_router

    captured = []

    class FakeMemoryManager:
        def on_session_end(self, user_id: str, session_id: str) -> None:
            captured.append((user_id, session_id))

    alice = _create_user("alice-end-session@example.edu")
    bob = _create_user("bob-end-session@example.edu")
    from app.data import sessions

    session_id = sessions.create_session(
        alice["id"],
        title="End session fixture",
        term_scope="Spring 2025",
    )

    monkeypatch.setattr(
        chat_router,
        "get_memory_manager",
        lambda: FakeMemoryManager(),
    )

    _login_as(app_client, alice["id"])
    response = app_client.post(
        "/api/session/end",
        json={
            "session_id": session_id,
            "student_id": bob["id"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "messages_archived": 0}
    assert captured == [(alice["id"], session_id)]
    assert not (
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


def test_schedule_mutations_enforce_cookie_session_owner(app_client):
    from app.data import sessions

    alice = _create_user("alice-schedule@example.edu")
    bob = _create_user("bob-schedule@example.edu")

    alice_session = sessions.create_session(
        alice["id"],
        title="Alice schedule",
        term_scope="Spring 2025",
    )
    bob_session = sessions.create_session(
        bob["id"],
        title="Bob schedule",
        term_scope="Spring 2025",
    )
    bob_pending = [
        {"course_id": "COMPSCI161", "section": "A", "status": "pending"}
    ]
    sessions.update_session_state(
        bob["id"],
        bob_session,
        {"pending_schedule": bob_pending},
    )

    _login_as(app_client, alice["id"])

    add_response = app_client.post(
        "/api/schedule/add",
        json={
            "session_id": bob_session,
            "course_id": "IN4MATX43",
            "section": "A",
            "term": "Spring 2025",
        },
    )
    remove_response = app_client.post(
        "/api/schedule/remove",
        json={
            "session_id": bob_session,
            "course_id": "COMPSCI161",
            "section": "A",
            "term": "Spring 2025",
        },
    )
    clear_response = app_client.post(
        "/api/schedule/clear",
        json={
            "session_id": bob_session,
            "term": "Spring 2025",
        },
    )

    assert add_response.status_code == 404
    assert remove_response.status_code == 404
    assert clear_response.status_code == 404
    assert "Session not found" in add_response.json()["detail"]
    assert "Session not found" in remove_response.json()["detail"]
    assert "Session not found" in clear_response.json()["detail"]
    assert sessions.get_session_state(bob["id"], bob_session)["pending_schedule"] == bob_pending
    assert sessions.get_session_state(alice["id"], alice_session)["pending_schedule"] == []
