"""
MemoryProvider Abstract Base Class

Adapted from Hermes Agent's memory provider abstraction. Defines the
lifecycle hooks an external memory backend must implement.

Lifecycle (called by MemoryManager from chat.py):
    is_available()        — pre-flight readiness check
    initialize()          — load user state at session start
    system_prompt_block() — static context injected into Claude's system prompt
    prefetch(query)       — dynamic recall before each LLM call
    on_turn_start(turn)   — per-turn nudge / counter
    sync_turn(u, a)       — completed-turn lifecycle hook
    on_session_end()      — session cleanup hook
    shutdown()            — flush queues, close files

Design intent:
    Different providers (JSON file, SQLite + FTS5, vector store) all
    expose this same shape. Swapping backends never touches chat.py.
"""

from abc import ABC, abstractmethod
from typing import Optional


class MemoryProvider(ABC):
    """Long-term memory for the UCI Course Advisor."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier (e.g. 'json-file', 'sqlite-fts5')."""

    # ── Core lifecycle (must implement) ─────────────────────

    @abstractmethod
    def is_available(self) -> bool:
        """Quick check: is this provider ready to use? No network calls."""

    @abstractmethod
    def initialize(self, session_id: str, user_id: str) -> None:
        """Load this user's persistent state into provider memory."""

    # ── Recall channels (override or use defaults) ──────────

    def system_prompt_block(self, user_id: str) -> str:
        """
        Static student profile + learned preferences.
        Injected ONCE into Claude's system prompt per request.
        """
        return ""

    def prefetch(self, query: str, user_id: str) -> str:
        """
        Dynamic recall — only items relevant to THIS query.
        Inject as additional context for the upcoming LLM call.
        """
        return ""

    # ── Per-turn lifecycle ──────────────────────────────────

    def on_turn_start(self, turn_number: int, user_id: str) -> Optional[str]:
        """
        Called at the start of every chat turn. May return a string
        (e.g. periodic-reflection nudge) for the caller to inject as
        a system reminder. Return None to do nothing.
        """
        return None

    def sync_turn(
        self,
        user_id: str,
        user_message: str,
        assistant_message: str,
        session_id: str,
    ) -> None:
        """
        Completed-turn lifecycle hook. Chat transcripts are persisted
        by app.data.sessions, not by MemoryProvider implementations.
        """

    # ── Session boundaries ──────────────────────────────────

    def on_session_end(
        self,
        user_id: str,
        session_id: str,
    ) -> None:
        """
        Called when the session ends explicitly (browser closes,
        /session/end called, gateway timeout). Do not persist chat
        history here; sessions/{session_id}/turns.jsonl is the unique
        transcript source.
        """

    def shutdown(self) -> None:
        """Final flush. Called once when the app is shutting down."""

    # ── Optional write API ──────────────────────────────────

    def add_preference(self, user_id: str, text: str) -> None:
        """Append a learned preference (e.g. 'prefers morning classes')."""

    def add_fact(self, user_id: str, text: str) -> None:
        """Append a hard fact (e.g. 'completed ICS33 in Fall 2024')."""

    def update_profile(self, user_id: str, updates: dict) -> None:
        """Update structured profile fields (major, year, etc.)."""

    def get_profile(self, user_id: str) -> dict:
        """Return structured profile fields for a user."""
        return {}

    def get_facts(self, user_id: str) -> list[str]:
        """Return stored hard facts for a user as list[str]."""
        return []

    def get_preferences(self, user_id: str) -> list:
        """Return stored learned preferences for a user."""
        return []

    def get_memory_snapshot(self, user_id: str) -> dict:
        """Return profile, facts, and preferences through the provider."""
        return {
            "profile": self.get_profile(user_id),
            "facts": self.get_facts(user_id),
            "preferences": self.get_preferences(user_id),
        }

    def forget_preference(self, user_id: str, pref_id: str) -> dict | None:
        """Remove one learned preference by id. Return removal metadata or None."""
        return None

    def forget_all_preferences(self, user_id: str) -> int:
        """Remove all learned preferences. Return the number removed."""
        return 0
