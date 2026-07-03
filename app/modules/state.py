"""
Session State Management

Maps to SKILL.md: Step 1 — Gather Key Information.

Tracks the following per session:
  - term, major, year
  - selected_courses, completed_courses
  - preferred_time, difficulty_preference
  - recommendation_goal
  - conversation history

TODO: Persist sessions to Redis or database for multi-request durability.
      Currently stored in-memory (resets on server restart).
"""

from typing import Optional
from app.data.db import get_student_profile


# ── In-Memory Session Store ──────────────────────────────
_sessions: dict[str, dict] = {}


def get_or_create_session(session_id: str) -> dict:
    """Get existing session or create a new one."""
    if session_id not in _sessions:
        _sessions[session_id] = _make_empty_session(session_id)
    return _sessions[session_id]


def update_session(session_id: str, updates: dict) -> dict:
    """Merge updates into the session state."""
    session = get_or_create_session(session_id)
    for key, value in updates.items():
        if key in session and value is not None:
            session[key] = value
    return session


def add_message(session_id: str, role: str, content: str):
    """Append a message to conversation history."""
    session = get_or_create_session(session_id)
    session["history"].append({"role": role, "content": content})


def load_student_into_session(session_id: str, student_id: str) -> bool:
    """
    Pre-fill session state from student profile if available.
    Maps to SKILL.md: 'Prioritize obtaining info via system tools or existing context.'

    TODO: Call real student_profile_tool here.
    """
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
    update_session(session_id, {
        "major": profile.get("major"),
        "year": profile.get("year"),
        "term": profile.get("term"),
        "completed_courses": profile.get("completed_courses", []),
        "selected_courses": profile.get("selected_courses", []),
    })
    return True


def get_known_fields(session_id: str) -> dict:
    """Return a summary of which key fields are populated."""
    s = get_or_create_session(session_id)
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


# ── Internal ─────────────────────────────────────────────

def _make_empty_session(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "term": None,
        "major": None,
        "year": None,
        "selected_courses": [],
        "completed_courses": [],
        "preferred_time": None,
        "difficulty_preference": None,
        "recommendation_goal": None,
        "history": [],
        "pending_schedule": [],  # courses user tentatively added
    }
