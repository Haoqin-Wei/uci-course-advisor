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
        "preferred_time": None,
        "difficulty_preference": None,
        "recommendation_goal": None,
        "pending_schedule": [],
        "major": "Computer Science",
        "selected_courses": ["ICS33"],
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
        "preferred_time": None,
        "difficulty_preference": "easy",
        "recommendation_goal": None,
        "pending_schedule": [],
    }


def test_session_state_requires_existing_session():
    from app.data import sessions

    with pytest.raises(sessions.SessionNotFound):
        sessions.get_session_state("demo_001", "sess_deadbeef")
