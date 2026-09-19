"""
Session state compatibility layer.

Structured per-session state is persisted by app.data.sessions in
sessions/{session_id}/state.json. This module keeps the older helper API
used by chat.py and schedule routes, but it no longer owns an in-memory
session store.
"""

from __future__ import annotations

from typing import Optional

from app.data import sessions as sessions_data
from app.data.db import get_student_profile


DEFAULT_USER_ID = "demo_001"

_STATE_FIELDS = {
    "term",
    "major",
    "year",
    "selected_courses",
    "completed_courses",
    "preferred_time",
    "difficulty_preference",
    "recommendation_goal",
    "pending_schedule",
}
_LIST_FIELDS = {"selected_courses", "completed_courses", "pending_schedule"}


def _normalize_state(session_id: str, state: dict) -> dict:
    normalized = {
        "session_id": session_id,
        "term": state.get("term"),
        "major": state.get("major"),
        "year": state.get("year"),
        "selected_courses": state.get("selected_courses") or [],
        "completed_courses": state.get("completed_courses") or [],
        "preferred_time": state.get("preferred_time"),
        "difficulty_preference": state.get("difficulty_preference"),
        "recommendation_goal": state.get("recommendation_goal"),
        "pending_schedule": state.get("pending_schedule") or [],
    }
    for field in _LIST_FIELDS:
        if not isinstance(normalized[field], list):
            normalized[field] = []
    return normalized


def get_or_create_session(
    session_id: str,
    *,
    user_id: str = DEFAULT_USER_ID,
) -> dict:
    """
    Return persisted structured state for an existing session.

    The name is kept for compatibility with older call sites. Session
    creation is handled by app.data.sessions.create_session() and the
    chat router's temporary legacy-id resolver.
    """
    state = sessions_data.get_session_state(user_id, session_id)
    return _normalize_state(session_id, state)


def update_session(
    session_id: str,
    updates: dict,
    *,
    user_id: str = DEFAULT_USER_ID,
) -> dict:
    """Merge allowed structured fields into sessions/{session_id}/state.json."""
    if not isinstance(updates, dict):
        raise ValueError("updates must be a dict")

    cleaned = {}
    for key, value in updates.items():
        if key not in _STATE_FIELDS or value is None:
            continue
        if key in _LIST_FIELDS and not isinstance(value, list):
            continue
        cleaned[key] = value

    if cleaned:
        state = sessions_data.update_session_state(user_id, session_id, cleaned)
    else:
        state = sessions_data.get_session_state(user_id, session_id)
    return _normalize_state(session_id, state)


def add_message(session_id: str, role: str, content: str) -> None:
    """
    Deprecated no-op.

    Chat history is persisted only through app.data.sessions.append_turn().
    This helper remains temporarily so old imports fail softly while the
    rest of M2.2 removes legacy state usage.
    """
    return None


def load_student_into_session(
    session_id: str,
    student_id: str,
    *,
    user_id: Optional[str] = None,
) -> bool:
    """
    Pre-fill persisted session state from the student's profile if available.

    user_id identifies the session owner. When omitted, it defaults to
    student_id for backward-compatible direct calls.
    """
    owner_id = user_id or student_id
    response = get_student_profile(student_id)
    if not response:
        return False

    if isinstance(response, dict) and "profile" in response:
        if response.get("found") is False:
            return False
        profile = response.get("profile") or {}
    else:
        # Backward compatibility for tests or older adapters that return
        # the raw profile dict directly instead of the db.py envelope.
        profile = response

    if not isinstance(profile, dict) or not profile:
        return False

    update_session(
        session_id,
        {
            "major": profile.get("major"),
            "year": profile.get("year"),
            "term": profile.get("term"),
            "completed_courses": profile.get("completed_courses", []),
            "selected_courses": profile.get("selected_courses", []),
        },
        user_id=owner_id,
    )
    return True


def get_known_fields(
    session_id: str,
    *,
    user_id: str = DEFAULT_USER_ID,
) -> dict:
    """Return the key populated fields from persisted session state."""
    s = get_or_create_session(session_id, user_id=user_id)
    return {
        "term": s["term"],
        "major": s["major"],
        "year": s["year"],
        "selected_courses": s["selected_courses"],
        "completed_courses": s["completed_courses"],
        "preferred_time": s["preferred_time"],
        "difficulty_preference": s["difficulty_preference"],
        "recommendation_goal": s["recommendation_goal"],
    }
