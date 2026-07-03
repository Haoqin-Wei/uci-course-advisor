"""
Session storage layer (Phase 3.1).

Stores per-session conversation history and metadata under
  data/memory/{user_id}/sessions/{session_id}/

Each session is a folder with:
  meta.json    — title, term_scope, timestamps, decisions, summary
  state.json   — structured per-session chat/planning state
  turns.jsonl  — append-only conversation log, one Turn per line

Sessions own conversation history. User-level memory
(data/memory/{user_id}/{profile,preferences,facts}.json) is shared
across all sessions for that user.

Public API:
    create_session(user_id, title=None, term_scope=None)  → session_id
    get_session_meta(user_id, session_id)                 → dict
    update_session_meta(user_id, session_id, **fields)    → dict
    get_session_state(user_id, session_id)                → dict
    update_session_state(user_id, session_id, updates)    → dict
    list_sessions(user_id, limit=None)                    → list[dict]
    delete_session(user_id, session_id)                   → bool
    append_turn(user_id, session_id, role, content)       → turn_index
    read_turns(user_id, session_id, since_turn=0)         → list[dict]
    count_turns(user_id, session_id)                      → int
    append_decision(user_id, session_id, text, from_turn) → dict

Raises:
    InvalidId        for malformed user_id or session_id
    SessionNotFound  for missing sessions
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any


MEMORY_ROOT = Path("data/memory")
SESSION_ID_PREFIX = "sess_"
SESSION_ID_BYTES = 3                  # 6 hex chars → 16M possibilities


# ── Exceptions ───────────────────────────────────────────

class InvalidId(ValueError):
    """user_id or session_id is malformed or unsafe."""


class SessionNotFound(KeyError):
    """The requested session doesn't exist on disk."""


# ── ID utilities ─────────────────────────────────────────

_SESSION_ID_RE = re.compile(r"^sess_[a-f0-9]+$")


def _validate_user_id(user_id: str) -> None:
    if not user_id or not isinstance(user_id, str):
        raise InvalidId("user_id must be a non-empty string")
    if "/" in user_id or "\\" in user_id or ".." in user_id:
        raise InvalidId(f"user_id contains forbidden characters: {user_id!r}")


def _validate_session_id(session_id: str) -> None:
    if not session_id or not isinstance(session_id, str):
        raise InvalidId("session_id must be a non-empty string")
    if not _SESSION_ID_RE.match(session_id):
        raise InvalidId(f"session_id must match {_SESSION_ID_RE.pattern}: {session_id!r}")


def _new_session_id() -> str:
    return f"{SESSION_ID_PREFIX}{secrets.token_hex(SESSION_ID_BYTES)}"


# ── Path helpers ─────────────────────────────────────────

def _user_root(user_id: str) -> Path:
    _validate_user_id(user_id)
    return MEMORY_ROOT / user_id


def _sessions_root(user_id: str) -> Path:
    return _user_root(user_id) / "sessions"


def _session_dir(user_id: str, session_id: str) -> Path:
    _validate_session_id(session_id)
    return _sessions_root(user_id) / session_id


def _meta_path(user_id: str, session_id: str) -> Path:
    return _session_dir(user_id, session_id) / "meta.json"


def _turns_path(user_id: str, session_id: str) -> Path:
    return _session_dir(user_id, session_id) / "turns.jsonl"


def _state_path(user_id: str, session_id: str) -> Path:
    return _session_dir(user_id, session_id) / "state.json"


# ── Time ─────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Low-level JSON / JSONL helpers ───────────────────────

def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic-ish write via temp file + rename.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue        # skip corrupted lines, don't crash
    return rows


def _empty_session_state() -> dict:
    return {}


# ── CRUD ─────────────────────────────────────────────────

def create_session(
    user_id: str,
    title: Optional[str] = None,
    term_scope: Optional[str] = None,
) -> str:
    """
    Create a new session and return its session_id.

    title defaults to "New session"; it's expected to be replaced after
    the first user message via update_session_meta(title=...).
    """
    _validate_user_id(user_id)
    session_id = _new_session_id()
    # Astronomically unlikely, but loop until we get a fresh ID.
    while _session_dir(user_id, session_id).exists():
        session_id = _new_session_id()

    now = _now_iso()
    meta = {
        "session_id":            session_id,
        "user_id":               user_id,
        "title":                 title or "New session",
        "term_scope":            term_scope,
        "created_at":            now,
        "last_active_at":        now,
        "turn_count":            0,
        "decisions":             [],
        "summary":               None,
        "summary_through_turn":  None,
    }
    _write_json(_meta_path(user_id, session_id), meta)
    _write_json(_state_path(user_id, session_id), _empty_session_state())
    return session_id


def get_session_meta(user_id: str, session_id: str) -> dict:
    meta_path = _meta_path(user_id, session_id)
    if not meta_path.exists():
        raise SessionNotFound(f"{user_id}/{session_id}")
    meta = _read_json(meta_path)
    if not isinstance(meta, dict):
        raise SessionNotFound(f"{user_id}/{session_id} (corrupt meta)")
    return meta


def update_session_meta(
    user_id: str,
    session_id: str,
    **fields: Any,
) -> dict:
    """
    Patch the session's meta.json with the given fields. Unknown fields
    are accepted (forward-compat). last_active_at is NOT auto-bumped here
    — only append_turn bumps it. Returns the new meta.

    Disallowed fields (silently ignored): session_id, user_id, created_at,
    turn_count (use append_turn to mutate counts).
    """
    DISALLOWED = {"session_id", "user_id", "created_at", "turn_count"}
    meta = get_session_meta(user_id, session_id)
    for k, v in fields.items():
        if k in DISALLOWED:
            continue
        meta[k] = v
    _write_json(_meta_path(user_id, session_id), meta)
    return meta


def get_session_state(user_id: str, session_id: str) -> dict:
    """
    Return the structured per-session state stored in state.json.

    The meta file remains the session existence check, so older session
    folders that predate state.json read as an empty state instead of
    crashing. Missing or corrupt state files also fall back to an empty
    dict; callers can patch them through update_session_state().
    """
    get_session_meta(user_id, session_id)
    state = _read_json(_state_path(user_id, session_id), default=None)
    if not isinstance(state, dict):
        return _empty_session_state()
    return state


def update_session_state(
    user_id: str,
    session_id: str,
    updates: dict,
) -> dict:
    """
    Patch state.json with structured per-session state fields.

    This is intentionally separate from meta.json so planner/chat state
    can migrate out of app.modules.state._sessions without mixing UI
    state into listing metadata.
    """
    if not isinstance(updates, dict):
        raise ValueError("updates must be a dict")

    state = get_session_state(user_id, session_id)
    state.update(updates)
    _write_json(_state_path(user_id, session_id), state)
    return state


def list_sessions(user_id: str, limit: Optional[int] = None) -> list[dict]:
    """
    Return all session metadata for a user, sorted by last_active_at desc.
    Lightweight: reads each meta.json (no turns).
    """
    root = _sessions_root(user_id)
    if not root.exists():
        return []
    metas: list[dict] = []
    for sd in root.iterdir():
        if not sd.is_dir():
            continue
        meta = _read_json(sd / "meta.json")
        if isinstance(meta, dict):
            metas.append(meta)
    metas.sort(key=lambda m: m.get("last_active_at", ""), reverse=True)
    if limit is not None and limit > 0:
        metas = metas[:limit]
    return metas


def delete_session(user_id: str, session_id: str) -> bool:
    """Remove the session folder. Returns True if it existed, False otherwise."""
    sd = _session_dir(user_id, session_id)
    if not sd.exists():
        return False
    shutil.rmtree(sd)
    return True


def append_turn(
    user_id: str,
    session_id: str,
    role: str,
    content: str,
    *,
    cards: Optional[list] = None,
    followups: Optional[list] = None,
    validation: Optional[dict] = None,
) -> int:
    """
    Append a turn to turns.jsonl, return its turn_index.
    Auto-bumps turn_count and last_active_at in meta.

    Optional extras (assistant turns only, in practice): cards/followups/
    validation are persisted so history replay can reconstruct course
    cards, followup chips, and the validation footer. Falsy values are
    omitted from the JSONL so legacy turns stay byte-identical when
    re-read.
    """
    if role not in ("user", "assistant"):
        raise ValueError(f"role must be 'user' or 'assistant', got {role!r}")

    meta = get_session_meta(user_id, session_id)
    turn_index = meta.get("turn_count", 0) + 1
    now = _now_iso()
    turn = {
        "turn_index": turn_index,
        "role":       role,
        "content":    content,
        "timestamp":  now,
    }
    if cards:
        turn["cards"] = cards
    if followups:
        turn["followups"] = followups
    if validation:
        turn["validation"] = validation
    _append_jsonl(_turns_path(user_id, session_id), turn)

    meta["turn_count"]     = turn_index
    meta["last_active_at"] = now
    _write_json(_meta_path(user_id, session_id), meta)
    return turn_index


def read_turns(
    user_id: str,
    session_id: str,
    since_turn: int = 0,
) -> list[dict]:
    """
    Return turns with turn_index > since_turn. Use since_turn=0 for all.
    Used to skip already-summarized turns once Phase 3.9 lands.
    """
    if not _meta_path(user_id, session_id).exists():
        raise SessionNotFound(f"{user_id}/{session_id}")
    turns = _read_jsonl(_turns_path(user_id, session_id))
    if since_turn > 0:
        turns = [t for t in turns if t.get("turn_index", 0) > since_turn]
    return turns


def count_turns(user_id: str, session_id: str) -> int:
    """Fast turn count from meta — no need to read turns.jsonl."""
    meta = get_session_meta(user_id, session_id)
    return int(meta.get("turn_count", 0))


def append_decision(
    user_id: str,
    session_id: str,
    text: str,
    from_turn: int,
) -> dict:
    """
    Append a pinned decision to the session. Idempotent on text
    (same text won't be appended twice). Returns the new decision dict.
    """
    text = text.strip()
    if not text:
        raise ValueError("decision text must not be empty")

    meta = get_session_meta(user_id, session_id)
    decisions = meta.get("decisions") or []

    # Dedupe on lowercase text
    existing_lower = {(d.get("text") or "").strip().lower() for d in decisions}
    if text.lower() in existing_lower:
        return {"text": text, "from_turn": from_turn, "already_existed": True}

    decision = {
        "text":      text,
        "from_turn": from_turn,
        "at":        _now_iso(),
    }
    decisions.append(decision)
    meta["decisions"] = decisions
    _write_json(_meta_path(user_id, session_id), meta)
    return decision
