from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path


def _fresh_memory_manager(memory_root: Path):
    from app.memory.json_provider import JSONFileMemoryProvider
    from app.memory.manager import MemoryManager

    manager = MemoryManager()
    manager.set_provider(JSONFileMemoryProvider(base_dir=str(memory_root)))
    return manager


def test_memory_router_does_not_access_memory_json_files_directly():
    import app.routers.memory as memory_router

    tree = ast.parse(inspect.getsource(memory_router))

    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module.partition(".")[0])

    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    call_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    call_attrs = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "json" not in imported_modules
    assert "pathlib" not in imported_modules
    assert "MEMORY_ROOT" not in names
    assert "_read_json" not in names
    assert "_write_json" not in names
    assert "open" not in call_names
    assert not {"read_text", "write_text", "open"} & call_attrs
    assert "get_memory_manager" in names


def test_memory_snapshot_returns_facts_as_list(app_client, seeded_user):
    response = app_client.get(f"/api/memory/{seeded_user.user_id}")

    assert response.status_code == 200
    assert response.json()["facts"] == [
        "Currently taking STAT67",
        "Stated difficulty preference: easy",
        "Stated goal: ge_fulfillment",
    ]


def test_legacy_dict_facts_are_migrated_to_list(runtime_paths):
    user_id = "legacy_facts"
    user_dir = runtime_paths.memory_root / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    facts_path = user_dir / "facts.json"
    facts_path.write_text(
        json.dumps(
            {
                "completed": ["ICS33"],
                "difficulty_preference": "easy",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (user_dir / "profile.json").write_text("{}", encoding="utf-8")
    (user_dir / "preferences.json").write_text("[]", encoding="utf-8")

    manager = _fresh_memory_manager(runtime_paths.memory_root)
    try:
        manager.initialize_session("sess_legacy_facts", user_id)
        assert manager.get_facts(user_id) == [
            "completed: ICS33",
            "difficulty_preference: easy",
        ]
        manager.add_fact(user_id, "Currently taking STATS67")
    finally:
        manager.shutdown()

    assert json.loads(facts_path.read_text(encoding="utf-8")) == [
        "completed: ICS33",
        "difficulty_preference: easy",
        "Currently taking STATS67",
    ]


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


def test_profile_update_route_updates_loaded_memory_context_without_restart(
    app_client,
):
    from app.memory.manager import get_memory_manager

    active_manager = get_memory_manager()
    active_manager.initialize_session("sess_profile_cache", "demo_001")

    response = app_client.post(
        "/api/memory/path-value-is-ignored/profile",
        json={
            "major": "Computer Science",
            "completed_courses": ["ics33"],
        },
    )

    assert response.status_code == 200
    prompt_block = active_manager.system_prompt_block("demo_001")
    assert "major: Computer Science" in prompt_block
    assert "completed_courses: ['ICS33']" in prompt_block


def test_added_preference_persists_after_fresh_memory_manager(runtime_paths):
    from app.memory.manager import get_memory_manager

    manager = get_memory_manager()
    manager.add_preference("demo_001", "Prefers compact schedules")
    manager.add_preference("demo_001", "prefers compact schedules")

    preferences_path = runtime_paths.memory_root / "demo_001" / "preferences.json"
    persisted = json.loads(preferences_path.read_text(encoding="utf-8"))
    assert len(persisted) == 1
    assert set(persisted[0]) == {
        "id",
        "text",
        "learned_at",
        "last_confirmed_at",
    }
    assert persisted[0]["id"].startswith("pref_")
    assert persisted[0]["text"] == "Prefers compact schedules"
    assert persisted[0]["learned_at"]
    assert persisted[0]["last_confirmed_at"] >= persisted[0]["learned_at"]

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

    assert preferences == persisted
    assert "Prefers compact schedules" in prompt_block
    assert "Prefers compact schedules" in prefetched_context


def test_legacy_preferences_are_migrated_to_full_schema(runtime_paths):
    user_id = "legacy_preferences"
    user_dir = runtime_paths.memory_root / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    preferences_path = user_dir / "preferences.json"
    preferences_path.write_text(
        json.dumps(
            [
                "Prefers compact schedules",
                {
                    "id": "pref_existing",
                    "text": "Prefers morning classes",
                    "learned_at": "2025-01-15T12:00:00+00:00",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (user_dir / "profile.json").write_text("{}", encoding="utf-8")
    (user_dir / "facts.json").write_text("[]", encoding="utf-8")

    manager = _fresh_memory_manager(runtime_paths.memory_root)
    try:
        manager.initialize_session("sess_legacy_preferences", user_id)
        preferences = manager.get_preferences(user_id)
    finally:
        manager.shutdown()

    assert len(preferences) == 2
    assert all(
        set(pref) == {"id", "text", "learned_at", "last_confirmed_at"}
        for pref in preferences
    )
    assert preferences[0]["id"].startswith("pref_")
    assert preferences[0]["text"] == "Prefers compact schedules"
    assert preferences[0]["last_confirmed_at"] == preferences[0]["learned_at"]
    assert preferences[1] == {
        "id": "pref_existing",
        "text": "Prefers morning classes",
        "learned_at": "2025-01-15T12:00:00+00:00",
        "last_confirmed_at": "2025-01-15T12:00:00+00:00",
    }
    assert json.loads(preferences_path.read_text(encoding="utf-8")) == preferences


def test_add_preference_never_persists_bare_strings(runtime_paths):
    user_id = "new_preference_schema"
    manager = _fresh_memory_manager(runtime_paths.memory_root)
    try:
        manager.add_preference(user_id, "Prefers project-based classes")
    finally:
        manager.shutdown()

    preferences_path = runtime_paths.memory_root / user_id / "preferences.json"
    persisted = json.loads(preferences_path.read_text(encoding="utf-8"))

    assert persisted
    assert not any(isinstance(pref, str) for pref in persisted)
    assert all(
        set(pref) == {"id", "text", "learned_at", "last_confirmed_at"}
        for pref in persisted
    )


def test_legacy_string_preference_can_be_deleted_by_migrated_id(runtime_paths):
    from app.memory.json_provider import _preference_id

    user_id = "legacy_preference_delete"
    user_dir = runtime_paths.memory_root / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    preferences_path = user_dir / "preferences.json"
    preferences_path.write_text(
        json.dumps(["Prefers asynchronous classes"], ensure_ascii=False),
        encoding="utf-8",
    )
    (user_dir / "profile.json").write_text("{}", encoding="utf-8")
    (user_dir / "facts.json").write_text("[]", encoding="utf-8")

    manager = _fresh_memory_manager(runtime_paths.memory_root)
    pref_id = _preference_id("Prefers asynchronous classes")
    try:
        manager.initialize_session("sess_legacy_delete", user_id)
        result = manager.forget_preference(user_id, pref_id)
    finally:
        manager.shutdown()

    assert result == {"removed": pref_id, "remaining": 0}
    assert json.loads(preferences_path.read_text(encoding="utf-8")) == []


def test_preference_size_limit_counts_text_not_dict_fields(runtime_paths, monkeypatch):
    from app.memory import json_provider

    monkeypatch.setattr(json_provider, "USER_PROFILE_MAX_CHARS", 40)
    user_id = "preference_size_limit"

    manager = _fresh_memory_manager(runtime_paths.memory_root)
    try:
        manager.add_preference(user_id, "A" * 30)
        manager.add_preference(user_id, "B" * 30)
        preferences = manager.get_preferences(user_id)
    finally:
        manager.shutdown()

    assert [pref["text"] for pref in preferences] == ["B" * 30]

    preferences_path = runtime_paths.memory_root / user_id / "preferences.json"
    persisted = json.loads(preferences_path.read_text(encoding="utf-8"))
    assert [pref["text"] for pref in persisted] == ["B" * 30]


def test_repeated_preference_updates_last_confirmed_without_duplicate(
    runtime_paths,
    monkeypatch,
):
    from app.memory import json_provider

    times = iter([
        "2025-01-01T00:00:00+00:00",
        "2025-02-01T00:00:00+00:00",
    ])
    monkeypatch.setattr(json_provider, "_now_iso", lambda: next(times))

    user_id = "preference_reconfirmed"
    manager = _fresh_memory_manager(runtime_paths.memory_root)
    try:
        manager.add_preference(user_id, "Prefers morning classes")
        manager.add_preference(user_id, "prefers morning classes")
        preferences = manager.get_preferences(user_id)
    finally:
        manager.shutdown()

    assert len(preferences) == 1
    assert preferences[0]["text"] == "Prefers morning classes"
    assert preferences[0]["learned_at"] == "2025-01-01T00:00:00+00:00"
    assert preferences[0]["last_confirmed_at"] == "2025-02-01T00:00:00+00:00"


def test_conflicting_new_time_preference_replaces_old_preference(
    runtime_paths,
    monkeypatch,
):
    from app.memory import json_provider

    times = iter([
        "2025-01-01T00:00:00+00:00",
        "2025-02-01T00:00:00+00:00",
    ])
    monkeypatch.setattr(json_provider, "_now_iso", lambda: next(times))

    user_id = "preference_conflict"
    manager = _fresh_memory_manager(runtime_paths.memory_root)
    try:
        manager.add_preference(user_id, "Prefers morning classes")
        manager.add_preference(user_id, "Prefers afternoon classes")
        preferences = manager.get_preferences(user_id)
        prompt_block = manager.system_prompt_block(user_id)
    finally:
        manager.shutdown()

    assert [pref["text"] for pref in preferences] == ["Prefers afternoon classes"]
    assert preferences[0]["learned_at"] == "2025-02-01T00:00:00+00:00"
    assert preferences[0]["last_confirmed_at"] == "2025-02-01T00:00:00+00:00"
    assert "Prefers morning classes" not in prompt_block
    assert "Prefers afternoon classes" in prompt_block

    preferences_path = runtime_paths.memory_root / user_id / "preferences.json"
    assert json.loads(preferences_path.read_text(encoding="utf-8")) == preferences


def test_completed_turn_has_only_one_full_transcript_copy(runtime_paths):
    from app.data import sessions as sessions_data
    from app.memory.manager import get_memory_manager

    manager = get_memory_manager()
    user_id = "demo_001"
    session_id = sessions_data.create_session(
        user_id,
        title="Transcript dedupe fixture",
        term_scope="Spring 2025",
    )
    user_message = "Unique acceptance user message about COMPSCI161."
    assistant_message = "Unique acceptance assistant reply about COMPSCI161."

    manager.initialize_session(session_id, user_id)
    manager.on_turn_start(session_id, user_id)
    manager.sync_turn(
        user_id,
        user_message,
        assistant_message,
        session_id,
    )
    sessions_data.append_turn(user_id, session_id, "user", user_message)
    sessions_data.append_turn(user_id, session_id, "assistant", assistant_message)
    manager.on_session_end(user_id, session_id)

    user_dir = runtime_paths.memory_root / user_id
    transcript_paths = []
    for path in user_dir.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if user_message in text and assistant_message in text:
            transcript_paths.append(path.relative_to(user_dir))

    assert transcript_paths == [Path("sessions") / session_id / "turns.jsonl"]
    assert not (user_dir / "turn_log.jsonl").exists()


def test_deleted_preference_updates_loaded_memory_context_and_persists(
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

    assert "Prefers morning classes" not in active_manager.system_prompt_block(
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
