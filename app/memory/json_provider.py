"""
JSONFileMemoryProvider — file-system backed memory for demo / single-machine use.

File layout per user (under data/memory/):
    {user_id}/
        ├── profile.json         Channel A: structured identity (major, year, target_gpa, ...)
        ├── facts.json           Channel A: event-style hard facts (currently_taking, completed)
        └── preferences.json     Channel B: soft preferences from periodic reflection

Chat transcripts are owned by app.data.sessions:
    sessions/{session_id}/turns.jsonl

Architecture (post-refactor):
    Channel A — every turn, immediate writes — handled by chat.py via
        update_profile() and add_fact()
    Channel B — every N turns, background LLM reflection — handled by
        chat.py via add_preference() (after reflect_on_history_llm returns)
    The old inline `[memo]:` mechanism has been removed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.memory.base import MemoryProvider

logger = logging.getLogger(__name__)

# ── Bounded memory limits ─────────────────────────────────
USER_PROFILE_MAX_CHARS = 1500
FACTS_MAX_CHARS = 2200
PREFETCH_MAX_ITEMS = 5


def _pref_text(item) -> str:
    """
    Extract searchable text from a memory item.

    facts.json is list[str].
    preferences.json is list[dict] with
    {id, text, learned_at, last_confirmed_at}.

    Returns empty string for anything we can't normalize, so callers can
    safely .lower() / regex without isinstance checks.
    """
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        v = item.get("text")
        return v if isinstance(v, str) else ""
    return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _preference_id(text: str) -> str:
    digest = hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()[:12]
    return f"pref_{digest}"


def _fact_text(item) -> str:
    """Normalize one fact-like item to a non-empty string or ""."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        text = item.get("text")
        if isinstance(text, str):
            return text.strip()
    if item in (None, "", []):
        return ""
    return str(item).strip()


def _memory_item_chars(item) -> int:
    """Count stored memory item payload text, not container metadata."""
    text = _pref_text(item)
    if text:
        return len(text)
    return len(str(item)) if item not in (None, "", []) else 0


class JSONFileMemoryProvider(MemoryProvider):
    """File-based memory store. Simple, durable, no extra dependencies."""

    def __init__(self, base_dir: str = "data/memory"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._loaded: dict[str, dict] = {}

    @property
    def name(self) -> str:
        return "json-file"

    # ── Core lifecycle ──────────────────────────────────────

    def is_available(self) -> bool:
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            return os.access(self.base_dir, os.W_OK)
        except OSError:
            return False

    def initialize(self, session_id: str, user_id: str) -> None:
        if user_id in self._loaded:
            return
        user_dir = self.base_dir / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        raw_facts = self._read_json(user_dir / "facts.json", [])
        raw_preferences = self._read_json(user_dir / "preferences.json", [])
        preferences = (
            self._normalize_preferences(raw_preferences)
            if isinstance(raw_preferences, list)
            else raw_preferences
        )
        self._loaded[user_id] = {
            "profile": self._read_json(user_dir / "profile.json", {}),
            "preferences": preferences,
            "facts": self._normalize_facts(raw_facts),
        }
        if (
            self._loaded[user_id]["facts"] != raw_facts
            or self._loaded[user_id]["preferences"] != raw_preferences
        ):
            self._save_user(user_id, self._loaded[user_id])
        logger.info("Memory loaded for user=%s", user_id)

    # ── Recall channels ─────────────────────────────────────

    def system_prompt_block(self, user_id: str) -> str:
        self._ensure_loaded(user_id)
        data = self._loaded.get(user_id)
        if not data:
            return ""
        lines = []
        profile = data["profile"] if isinstance(data.get("profile"), dict) else {}
        if profile:
            lines.append("PERSISTENT STUDENT PROFILE:")
            for k, v in profile.items():
                if v not in (None, "", []):
                    lines.append(f"  {k}: {v}")
        preferences = data["preferences"] if isinstance(data.get("preferences"), list) else []
        if preferences:
            lines.append("\nLEARNED PREFERENCES (from past sessions):")
            for p in preferences[-10:]:
                text = _pref_text(p)
                if text:
                    lines.append(f"  - {text}")
        return "\n".join(lines)

    def prefetch(self, query: str, user_id: str) -> str:
        self._ensure_loaded(user_id)
        data = self._loaded.get(user_id)
        if not data:
            return ""
        facts = data["facts"]
        preferences = data["preferences"] if isinstance(data.get("preferences"), list) else []
        items = facts + preferences
        if not items:
            return ""
        keywords = [w.lower() for w in query.split() if len(w) > 3]
        if not keywords:
            return ""
        # facts is list[str]; preferences are dicts. Normalize both through
        # _pref_text before string matching.
        matched_texts: list[str] = []
        for item in items:
            text = _pref_text(item)
            if text and any(kw in text.lower() for kw in keywords):
                matched_texts.append(text)
        if not matched_texts:
            return ""
        head = "RELEVANT PRIOR CONTEXT (recalled for this query):"
        bullets = "\n".join(f"  - {t}" for t in matched_texts[:PREFETCH_MAX_ITEMS])
        return f"{head}\n{bullets}"

    # ── Per-turn ────────────────────────────────────────────

    def on_turn_start(self, turn_number: int, user_id: str) -> Optional[str]:
        """
        No-op now. Turn counting lives in MemoryManager.
        Kept only to satisfy the MemoryProvider interface.
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
        No-op by design.

        Full per-turn transcripts are persisted only in
        sessions/{session_id}/turns.jsonl through app.data.sessions.
        Keeping a Memory-level turn_log.jsonl created a second complete
        copy that could drift from the session repository.
        """
        return None

    # ── Session boundaries ──────────────────────────────────

    def on_session_end(
        self,
        user_id: str,
        session_id: str,
    ) -> None:
        """
        No-op by design.

        Session-end snapshots used to duplicate complete chat history
        under data/memory/{user_id}/sessions. The session repository is
        now the only transcript store.
        """
        return None

    def shutdown(self) -> None:
        for user_id, data in self._loaded.items():
            try:
                self._save_user(user_id, data)
            except Exception as e:
                logger.warning("Shutdown flush failed for %s: %s", user_id, e)

    # ── Read API (used by Channel B reflection task) ────────

    def get_preferences(self, user_id: str) -> list[dict]:
        """Return a copy of the user's current preference list."""
        prefs = self._preferences(user_id)
        return [dict(p) for p in prefs]

    def get_profile(self, user_id: str) -> dict:
        """Return a shallow copy of the user's persistent profile dict
        (major, year, target_gpa, completed_courses, selected_courses,
        ...). Empty dict if nothing recorded — never None so callers
        don't have to handle both branches."""
        self._ensure_loaded(user_id)
        profile = self._loaded[user_id]["profile"]
        return dict(profile) if isinstance(profile, dict) else {}

    def get_facts(self, user_id: str) -> list[str]:
        """Return a copy of the user's hard-fact store as list[str]."""
        self._ensure_loaded(user_id)
        return list(self._loaded[user_id]["facts"])

    def get_memory_snapshot(self, user_id: str) -> dict:
        """Return profile, facts, and preferences from the same loaded cache."""
        try:
            preferences = self.get_preferences(user_id)
        except ValueError:
            preferences = []
        return {
            "profile": self.get_profile(user_id),
            "facts": self.get_facts(user_id),
            "preferences": preferences,
        }

    # ── Write API ───────────────────────────────────────────

    def add_preference(self, user_id: str, text: str) -> None:
        prefs = self._preferences(user_id)
        text = text.strip()
        if not text:
            return
        # Dedup case-insensitively against normalized preference dicts.
        existing_lower = {_pref_text(p).lower() for p in prefs}
        if text.lower() in existing_lower:
            return
        now = _now_iso()
        prefs.append(
            {
                "id": _preference_id(text),
                "text": text,
                "learned_at": now,
                "last_confirmed_at": now,
            }
        )
        self._enforce_size_limit(user_id, "preferences", USER_PROFILE_MAX_CHARS)
        self._save_user(user_id, self._loaded[user_id])

    def add_fact(self, user_id: str, text: str) -> None:
        self._ensure_loaded(user_id)
        text = text.strip()
        if not text:
            return
        facts = self._loaded[user_id]["facts"]
        existing_lower = {str(f).lower() for f in facts}
        if text.lower() in existing_lower:
            return
        facts.append(text)
        self._enforce_size_limit(user_id, "facts", FACTS_MAX_CHARS)
        self._save_user(user_id, self._loaded[user_id])

    def update_profile(self, user_id: str, updates: dict) -> dict:
        self._ensure_loaded(user_id)
        cleaned = {k: v for k, v in updates.items() if v not in (None, "", [])}
        if not cleaned:
            return self.get_profile(user_id)
        # Skip writing if nothing actually changes
        current = self._loaded[user_id]["profile"]
        if not isinstance(current, dict):
            current = {}
            self._loaded[user_id]["profile"] = current
        if all(current.get(k) == v for k, v in cleaned.items()):
            return dict(current)
        current.update(cleaned)
        self._save_user(user_id, self._loaded[user_id])
        return dict(current)

    def forget_preference(self, user_id: str, pref_id: str) -> dict | None:
        prefs = self._preferences(user_id)
        before = len(prefs)
        kept = [p for p in prefs if p["id"] != pref_id]
        if len(kept) == before:
            return None
        self._loaded[user_id]["preferences"] = kept
        self._save_user(user_id, self._loaded[user_id])
        return {"removed": pref_id, "remaining": len(kept)}

    def forget_all_preferences(self, user_id: str) -> int:
        self._ensure_loaded(user_id)
        prefs = self._loaded[user_id]["preferences"]
        removed = len(prefs) if isinstance(prefs, list) else 0
        self._loaded[user_id]["preferences"] = []
        self._save_user(user_id, self._loaded[user_id])
        return removed

    # ── Internals ───────────────────────────────────────────

    def _ensure_loaded(self, user_id: str) -> None:
        if user_id not in self._loaded:
            self.initialize(session_id="", user_id=user_id)

    def _preferences(self, user_id: str) -> list:
        self._ensure_loaded(user_id)
        prefs = self._loaded[user_id]["preferences"]
        if not isinstance(prefs, list):
            raise ValueError("preferences.json is malformed")
        normalized = self._normalize_preferences(prefs)
        if normalized != prefs:
            self._loaded[user_id]["preferences"] = normalized
            self._save_user(user_id, self._loaded[user_id])
            prefs = normalized
        return prefs

    def _normalize_preference(self, item) -> dict | None:
        text = _pref_text(item).strip()
        if not text:
            return None

        if isinstance(item, dict):
            learned_at = item.get("learned_at") or _now_iso()
            last_confirmed_at = item.get("last_confirmed_at") or learned_at
            pref_id = item.get("id") or _preference_id(text)
        else:
            learned_at = _now_iso()
            last_confirmed_at = learned_at
            pref_id = _preference_id(text)

        return {
            "id": str(pref_id),
            "text": text,
            "learned_at": str(learned_at),
            "last_confirmed_at": str(last_confirmed_at),
        }

    def _normalize_preferences(self, raw: list) -> list[dict]:
        normalized: list[dict] = []
        seen: set[str] = set()
        for item in raw:
            pref = self._normalize_preference(item)
            if not pref:
                continue
            key = pref["text"].lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(pref)
        return normalized

    def _normalize_facts(self, raw) -> list[str]:
        """
        The canonical facts.json schema is list[str].

        Older or hand-edited dict payloads are flattened once at the provider
        boundary so the rest of the app never has to branch on list-vs-dict.
        """
        if isinstance(raw, list):
            return [text for item in raw if (text := _fact_text(item))]
        if not isinstance(raw, dict):
            return []

        normalized: list[str] = []
        for key, value in raw.items():
            if value in (None, "", []):
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                text = _fact_text(item)
                if text:
                    normalized.append(f"{key}: {text}")
        return normalized

    def _read_json(self, path: Path, default):
        if not path.exists():
            return default
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read %s, using default", path)
            return default

    def _save_user(self, user_id: str, data: dict) -> None:
        user_dir = self.base_dir / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        for key in ("profile", "preferences", "facts"):
            with (user_dir / f"{key}.json").open("w", encoding="utf-8") as f:
                json.dump(data[key], f, ensure_ascii=False, indent=2)

    def _enforce_size_limit(self, user_id: str, key: str, max_chars: int) -> None:
        items = self._loaded[user_id][key]
        total = sum(_memory_item_chars(item) for item in items)
        while total > max_chars and items:
            removed = items.pop(0)
            total -= _memory_item_chars(removed)
            logger.info("Memory full — dropped oldest %s entry for %s", key, user_id)
