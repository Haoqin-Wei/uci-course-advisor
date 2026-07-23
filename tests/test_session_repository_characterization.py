from __future__ import annotations

import pytest


def test_session_repository_persists_state_next_to_meta_and_turns(runtime_paths):
    from app.data import sessions

    session_id = sessions.create_session(
        "demo_001",
        title="State repository fixture",
        term_scope="Spring 2025",
    )

    assert sessions.get_session_state("demo_001", session_id) == {
        "term": "Spring 2025",
        "major": None,
        "year": None,
        "selected_courses": [],
        "completed_courses": [],
        "preferred_time": None,
        "difficulty_preference": None,
        "recommendation_goal": None,
        "pending_schedule": [],
    }

    updated = sessions.update_session_state(
        "demo_001",
        session_id,
        {
            "major": "Computer Science",
            "selected_courses": ["ICS33"],
        },
    )

    assert updated == {
        "term": "Spring 2025",
        "major": "Computer Science",
        "year": None,
        "selected_courses": ["ICS33"],
        "completed_courses": [],
        "preferred_time": None,
        "difficulty_preference": None,
        "recommendation_goal": None,
        "pending_schedule": [],
    }
    assert sessions.get_session_state("demo_001", session_id) == updated
    assert (
        runtime_paths.memory_root
        / "demo_001"
        / "sessions"
        / session_id
        / "state.json"
    ).exists()


def test_session_state_backfills_defaults_for_legacy_partial_state(runtime_paths):
    from app.data import sessions

    session_id = sessions.create_session(
        "demo_001",
        title="Legacy state fixture",
        term_scope="Spring 2025",
    )
    state_path = (
        runtime_paths.memory_root
        / "demo_001"
        / "sessions"
        / session_id
        / "state.json"
    )
    state_path.write_text(
        '{"difficulty_preference": "easy"}\n',
        encoding="utf-8",
    )

    assert sessions.get_session_state("demo_001", session_id) == {
        "term": "Spring 2025",
        "major": None,
        "year": None,
        "selected_courses": [],
        "completed_courses": [],
        "preferred_time": None,
        "difficulty_preference": "easy",
        "recommendation_goal": None,
        "pending_schedule": [],
    }


def test_session_state_requires_existing_session():
    from app.data import sessions

    with pytest.raises(sessions.SessionNotFound):
        sessions.get_session_state("demo_001", "sess_deadbeef")


def test_state_module_uses_session_repository_without_in_memory_store(runtime_paths):
    from app.data import sessions
    from app.modules import state

    session_id = sessions.create_session(
        "demo_001",
        title="State module fixture",
        term_scope="Spring 2025",
    )

    assert not hasattr(state, "_sessions")

    updated = state.update_session(
        session_id,
        {
            "major": "Computer Science",
            "selected_courses": ["ICS33"],
            "difficulty_preference": "easy",
        },
        user_id="demo_001",
    )

    assert updated["major"] == "Computer Science"
    assert state.get_known_fields(session_id, user_id="demo_001") == {
        "term": "Spring 2025",
        "major": "Computer Science",
        "year": None,
        "selected_courses": ["ICS33"],
        "completed_courses": [],
        "preferred_time": None,
        "difficulty_preference": "easy",
        "recommendation_goal": None,
    }
    assert sessions.get_session_state("demo_001", session_id)["selected_courses"] == [
        "ICS33"
    ]


def test_service_restart_keeps_term_profile_history_and_pending_schedule(runtime_paths):
    from app.data import sessions
    from app.memory.json_provider import JSONFileMemoryProvider
    from app.memory.manager import MemoryManager

    user_id = "restart_acceptance"
    session_id = sessions.create_session(
        user_id,
        title="Restart acceptance fixture",
        term_scope="Spring 2025",
    )

    manager = MemoryManager()
    manager.set_provider(JSONFileMemoryProvider(base_dir=str(runtime_paths.memory_root)))
    try:
        manager.initialize_session(session_id, user_id)
        manager.update_profile(
            user_id,
            {
                "major": "Computer Science",
                "year": "Junior",
            },
        )
    finally:
        manager.shutdown()

    sessions.update_session_state(
        user_id,
        session_id,
        {
            "pending_schedule": [
                {"course_id": "COMPSCI161", "section": "A", "status": "pending"}
            ],
        },
    )
    sessions.append_turn(user_id, session_id, "user", "Remember this schedule.")
    sessions.append_turn(
        user_id,
        session_id,
        "assistant",
        "Schedule saved.",
        web_fetches=[
            {
                "method": "GET",
                "url": "https://ics.uci.edu/course-enrollment-restrictions/",
                "status_code": 200,
                "ok": True,
                "provides_evidence": True,
            }
        ],
    )

    fresh_manager = MemoryManager()
    fresh_manager.set_provider(
        JSONFileMemoryProvider(base_dir=str(runtime_paths.memory_root))
    )
    try:
        fresh_manager.initialize_session("sess_after_restart", user_id)
        profile_after_restart = fresh_manager.get_profile(user_id)
    finally:
        fresh_manager.shutdown()

    state_after_restart = sessions.get_session_state(user_id, session_id)
    turns_after_restart = sessions.read_turns(user_id, session_id)

    assert state_after_restart["term"] == "Spring 2025"
    assert state_after_restart["pending_schedule"] == [
        {"course_id": "COMPSCI161", "section": "A", "status": "pending"}
    ]
    assert profile_after_restart == {
        "major": "Computer Science",
        "year": "Junior",
    }
    assert [(turn["role"], turn["content"]) for turn in turns_after_restart] == [
        ("user", "Remember this schedule."),
        ("assistant", "Schedule saved."),
    ]
    assert turns_after_restart[1]["web_fetches"] == [
        {
            "method": "GET",
            "url": "https://ics.uci.edu/course-enrollment-restrictions/",
            "status_code": 200,
            "ok": True,
            "provides_evidence": True,
        }
    ]
