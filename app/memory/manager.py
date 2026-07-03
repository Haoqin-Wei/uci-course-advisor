"""
MemoryManager — singleton wrapper around the active provider.

Mirrors Hermes Agent's enforce-one-provider rule.

Channel B knobs:
    REFLECTION_INTERVAL — every N turns, the chat router fires a background
    LLM call to extract soft preferences from recent history. Set to a small
    number for the demo; raise it in production to reduce token cost.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.memory.base import MemoryProvider
from app.memory.json_provider import JSONFileMemoryProvider

logger = logging.getLogger(__name__)


class MemoryManager:
    REFLECTION_INTERVAL = 3   # Channel B firing rate

    def __init__(self):
        self._provider: Optional[MemoryProvider] = None
        self._turn_counts: dict[str, int] = {}
        self._initialized_sessions: set[str] = set()

    def set_provider(self, provider: MemoryProvider) -> None:
        if not provider.is_available():
            logger.warning("Provider %s is unavailable; memory disabled", provider.name)
            self._provider = None
            return
        self._provider = provider
        logger.info("Memory provider active: %s", provider.name)

    @property
    def provider(self) -> Optional[MemoryProvider]:
        return self._provider

    @property
    def enabled(self) -> bool:
        return self._provider is not None

    # ── Lifecycle pass-throughs (safe no-op when disabled) ──

    def initialize_session(self, session_id: str, user_id: str) -> None:
        if not self._provider:
            return
        if session_id in self._initialized_sessions:
            return
        try:
            self._provider.initialize(session_id, user_id)
            self._initialized_sessions.add(session_id)
            self._turn_counts[session_id] = 0
        except Exception as e:
            logger.warning("initialize_session failed: %s", e)

    def system_prompt_block(self, user_id: str) -> str:
        if not self._provider:
            return ""
        try:
            return self._provider.system_prompt_block(user_id)
        except Exception as e:
            logger.warning("system_prompt_block failed: %s", e)
            return ""

    def prefetch(self, query: str, user_id: str) -> str:
        if not self._provider:
            return ""
        try:
            return self._provider.prefetch(query, user_id)
        except Exception as e:
            logger.warning("prefetch failed: %s", e)
            return ""

    def on_turn_start(self, session_id: str, user_id: str) -> None:
        """Increment turn count for this session. No nudge text anymore."""
        if not self._provider:
            return
        self._turn_counts[session_id] = self._turn_counts.get(session_id, 0) + 1

    def turn_count(self, session_id: str) -> int:
        return self._turn_counts.get(session_id, 0)

    def should_reflect(self, session_id: str) -> bool:
        """
        Channel B trigger: True when this turn should kick off a background
        reflection task. Fires every REFLECTION_INTERVAL turns starting at turn N.
        """
        if not self._provider:
            return False
        n = self._turn_counts.get(session_id, 0)
        return n > 0 and n % self.REFLECTION_INTERVAL == 0

    def sync_turn(
        self,
        user_id: str,
        user_message: str,
        assistant_message: str,
        session_id: str,
    ) -> None:
        if not self._provider:
            return
        try:
            self._provider.sync_turn(user_id, user_message, assistant_message, session_id)
        except Exception as e:
            logger.warning("sync_turn failed: %s", e)

    def on_session_end(self, user_id: str, session_id: str) -> None:
        if not self._provider:
            return
        try:
            self._provider.on_session_end(user_id, session_id)
        finally:
            self._turn_counts.pop(session_id, None)
            self._initialized_sessions.discard(session_id)

    def shutdown(self) -> None:
        if self._provider:
            try:
                self._provider.shutdown()
            except Exception as e:
                logger.warning("shutdown failed: %s", e)

    # ── Channel-B helpers ──────────────────────────────────

    def get_preferences(self, user_id: str) -> list[str]:
        """Read current preferences (used by the reflection task to dedup)."""
        if not self._provider:
            return []
        try:
            return self._provider.get_preferences(user_id) if hasattr(
                self._provider, "get_preferences"
            ) else []
        except Exception as e:
            logger.warning("get_preferences failed: %s", e)
            return []

    def add_preference(self, user_id: str, text: str) -> None:
        if not self._provider:
            return
        try:
            self._provider.add_preference(user_id, text)
        except Exception as e:
            logger.warning("add_preference failed: %s", e)


# ── Module-level singleton ───────────────────────────────────

_manager = MemoryManager()
_manager.set_provider(JSONFileMemoryProvider())


def get_memory_manager() -> MemoryManager:
    return _manager
