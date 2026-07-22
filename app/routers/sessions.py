"""
Session CRUD API (Phase 3.2).

Five endpoints under /api/sessions/. Thin shells over app/data/sessions.py.
Exceptions from the data layer are translated to HTTP status codes here.

    GET    /api/sessions/{user_id}                    list (optional ?limit=N)
    POST   /api/sessions/{user_id}                    create
    GET    /api/sessions/{user_id}/{session_id}       meta + turns
    PATCH  /api/sessions/{user_id}/{session_id}       update title
    DELETE /api/sessions/{user_id}/{session_id}       delete

The path `{user_id}` is retained for URL compatibility with the existing
frontend but its value is IGNORED. Authoritative user id comes from the
session cookie via current_user_optional — anonymous callers get demo_001
(legacy demo flow), authenticated callers get their own.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.deps import current_user_optional
from app.data import sessions as S
from app.terms.service import get_term_resolution_service


router = APIRouter()


# ── Request body models ─────────────────────────────────

class CreateSessionBody(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)


class UpdateSessionBody(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)


# ── Exception translation helpers ───────────────────────

def _translate(exc: Exception) -> HTTPException:
    """Map data-layer exceptions to HTTP errors."""
    if isinstance(exc, S.InvalidId):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, S.SessionNotFound):
        return HTTPException(status_code=404, detail=f"Session not found: {exc}")
    return HTTPException(status_code=500, detail=f"Internal error: {exc}")


def _with_resolved_term(meta: dict) -> dict:
    out = dict(meta)
    out.update(get_term_resolution_service().conversation_state(meta))
    return out


# ── Endpoints ───────────────────────────────────────────

@router.get("/api/sessions/{user_id}")
def list_sessions(
    user_id: str,
    limit: Optional[int] = Query(default=None, ge=1, le=200),
    user: dict = Depends(current_user_optional),
):
    """
    List sessions for the authenticated caller (path user_id ignored).
    Sorted by last_active_at descending. Returns lightweight metadata
    only — use the per-session endpoint for conversation history.
    """
    real_user_id = user["id"]
    try:
        metas = S.list_sessions(real_user_id, limit=limit)
    except Exception as e:
        raise _translate(e)

    resolved = [_with_resolved_term(meta) for meta in metas]
    return {"user_id": real_user_id, "count": len(resolved), "sessions": resolved}


@router.post("/api/sessions/{user_id}")
def create_session(
    user_id: str, body: CreateSessionBody,
    user: dict = Depends(current_user_optional),
):
    """Create a new session for the authenticated caller (path user_id ignored)."""
    real_user_id = user["id"]
    try:
        session_id = S.create_session(
            real_user_id,
            title=body.title,
        )
        meta = S.get_session_meta(real_user_id, session_id)
    except Exception as e:
        raise _translate(e)

    return _with_resolved_term(meta)


@router.get("/api/sessions/{user_id}/{session_id}")
def get_session(
    user_id: str,
    session_id: str,
    since_turn: int = Query(default=0, ge=0),
    include_turns: bool = Query(default=True),
    user: dict = Depends(current_user_optional),
):
    """
    Get a session's metadata, optionally with its conversation history.
    Path user_id is ignored; the caller's session-derived id is used —
    that's how a real user can't read another user's session by guessing.

    - since_turn: return only turns with turn_index > since_turn
    - include_turns: set false for meta-only fetch (lighter payload)
    """
    real_user_id = user["id"]
    try:
        meta = S.get_session_meta(real_user_id, session_id)
        if include_turns:
            turns = S.read_turns(real_user_id, session_id, since_turn=since_turn)
        else:
            turns = None
    except Exception as e:
        raise _translate(e)

    out = dict(meta)
    out.update(get_term_resolution_service().conversation_state(meta))
    if include_turns:
        out["turns"] = turns
    return out


@router.patch("/api/sessions/{user_id}/{session_id}")
def update_session(
    user_id: str, session_id: str, body: UpdateSessionBody,
    user: dict = Depends(current_user_optional),
):
    """Partial update of user-editable session metadata (title only)."""
    real_user_id = user["id"]
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        meta = S.update_session_meta(real_user_id, session_id, **fields)
    except Exception as e:
        raise _translate(e)

    return _with_resolved_term(meta)


@router.delete("/api/sessions/{user_id}/{session_id}")
def delete_session(
    user_id: str, session_id: str,
    user: dict = Depends(current_user_optional),
):
    """
    Delete a session and its turns. Idempotent: deleting a session that
    doesn't exist returns ok=False with HTTP 200 so a refresh loop on
    the frontend doesn't need special handling.
    """
    real_user_id = user["id"]
    try:
        ok = S.delete_session(real_user_id, session_id)
    except Exception as e:
        raise _translate(e)

    return {"ok": ok, "session_id": session_id}
