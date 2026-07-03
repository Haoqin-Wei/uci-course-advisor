from __future__ import annotations

import json
from pathlib import Path

import pytest


def _fresh_memory_manager(memory_root: Path):
    from app.memory.json_provider import JSONFileMemoryProvider
    from app.memory.manager import MemoryManager

    manager = MemoryManager()
    manager.set_provider(JSONFileMemoryProvider(base_dir=str(memory_root)))
    return manager


def test_profile_update_route_persists_after_fresh_memory_manager(
    app_client,
    runtime_paths,
):
    response = app_client.post(
        "/api/memory/path-value-is-ignored/profile",
        json={
            "major": "Computer Science",
            "year": "Junior",
            "completed_courses": ["ics33", "ICS33"],
            "selected_courses": ["stats67"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["profile"]["major"] == "Computer Science"
    assert payload["profile"]["year"] == "Junior"
    assert payload["profile"]["completed_courses"] == ["ICS33"]
    assert payload["profile"]["selected_courses"] == ["STATS67"]

    fresh_manager = _fresh_memory_manager(runtime_paths.memory_root)
    try:
        fresh_manager.initialize_session("sess_after_restart", "demo_001")
        profile = fresh_manager.provider.get_profile("demo_001")
        prompt_block = fresh_manager.system_prompt_block("demo_001")
    finally:
        fresh_manager.shutdown()

    assert profile["major"] == "Computer Science"
    assert profile["year"] == "Junior"
    assert profile["completed_courses"] == ["ICS33"]
    assert profile["selected_courses"] == ["STATS67"]
    assert "major: Computer Science" in prompt_block
    assert "completed_courses: ['ICS33']" in prompt_block


def test_added_preference_persists_after_fresh_memory_manager(runtime_paths):
    from app.memory.manager import get_memory_manager

    manager = get_memory_manager()
    manager.add_preference("demo_001", "Prefers compact schedules")
    manager.add_preference("demo_001", "prefers compact schedules")

    preferences_path = runtime_paths.memory_root / "demo_001" / "preferences.json"
    assert json.loads(preferences_path.read_text(encoding="utf-8")) == [
        "Prefers compact schedules"
    ]

    fresh_manager = _fresh_memory_manager(runtime_paths.memory_root)
    try:
        fresh_manager.initialize_session("sess_after_restart", "demo_001")
        preferences = fresh_manager.get_preferences("demo_001")
        prompt_block = fresh_manager.system_prompt_block("demo_001")
        prefetched_context = fresh_manager.prefetch(
            "compact schedule recommendation",
            "demo_001",
        )
    finally:
        fresh_manager.shutdown()

    assert preferences == ["Prefers compact schedules"]
    assert "Prefers compact schedules" in prompt_block
    assert "Prefers compact schedules" in prefetched_context


def test_sync_turn_does_not_write_duplicate_turn_log(runtime_paths):
    from app.memory.manager import get_memory_manager

    manager = get_memory_manager()
    session_id = "sess_memory_dedup"

    manager.initialize_session(session_id, "demo_001")
    manager.on_turn_start(session_id, "demo_001")
    manager.sync_turn(
        "demo_001",
        "Which CS course should I take?",
        "Consider ICS 45C.",
        session_id,
    )

    assert not (runtime_paths.memory_root / "demo_001" / "turn_log.jsonl").exists()


def test_deleted_preference_is_gone_after_fresh_memory_manager_but_loaded_cache_is_stale(
    app_client,
    runtime_paths,
    seeded_user,
):
    from app.memory.manager import get_memory_manager

    active_manager = get_memory_manager()
    active_manager.initialize_session(seeded_user.session_id, seeded_user.user_id)
    assert "Prefers morning classes" in active_manager.system_prompt_block(
        seeded_user.user_id
    )

    response = app_client.delete(
        f"/api/memory/{seeded_user.user_id}/preferences/pref_morning"
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "removed": "pref_morning",
        "remaining": 0,
    }
    assert json.loads(
        (seeded_user.user_dir / "preferences.json").read_text(encoding="utf-8")
    ) == []

    snapshot = app_client.get(f"/api/memory/{seeded_user.user_id}")
    assert snapshot.status_code == 200
    assert snapshot.json()["preferences"] == []

    # Characterization of the current M1 behavior: the router rewrites
    # preferences.json, but a provider that already loaded this user keeps
    # serving its in-memory copy until the process/provider is restarted.
    assert "Prefers morning classes" in active_manager.system_prompt_block(
        seeded_user.user_id
    )

    fresh_manager = _fresh_memory_manager(runtime_paths.memory_root)
    try:
        fresh_manager.initialize_session("sess_after_restart", seeded_user.user_id)
        preferences_after_restart = fresh_manager.get_preferences(seeded_user.user_id)
        prompt_after_restart = fresh_manager.system_prompt_block(seeded_user.user_id)
    finally:
        fresh_manager.shutdown()

    assert preferences_after_restart == []
    assert "Prefers morning classes" not in prompt_after_restart
    assert "major: Computer Science" in prompt_after_restart


@pytest.mark.xfail(
    strict=True,
    reason=(
        "M2.3 should route preference deletion through MemoryRepository "
        "or invalidate the loaded provider cache."
    ),
)
def test_forget_preference_should_update_loaded_memory_context_without_restart(
    app_client,
    seeded_user,
):
    from app.memory.manager import get_memory_manager

    active_manager = get_memory_manager()
    active_manager.initialize_session(seeded_user.session_id, seeded_user.user_id)

    response = app_client.delete(
        f"/api/memory/{seeded_user.user_id}/preferences/pref_morning"
    )

    assert response.status_code == 200
    assert "Prefers morning classes" not in active_manager.system_prompt_block(
        seeded_user.user_id
    )
