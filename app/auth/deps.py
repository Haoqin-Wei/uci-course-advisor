"""
FastAPI dependencies for auth.

Two flavors, both read the session cookie:

  current_user_optional() → user_dict
      Real sessions take priority. In development, anonymous callers
      can still use demo_001. In public/prod, anonymous callers use
      a signed, expiring guest_* identity instead of a shared writable
      demo account.

  current_user_required() → user_dict (or HTTPException 401)
      Used by routes that should reject anonymous access — newly
      added profile-edit endpoints, the onboarding flow, anything
      that mutates per-user state intentionally.

Both dependencies stay lightweight (one signed-cookie decode + one
SQLite SELECT). Per-request overhead is negligible for the demo.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Cookie, HTTPException, Request, Response, status

from app import config
from app.auth import security, store

# Stand-in user record returned when a route allows anonymous access.
# Keeps existing data/memory/demo_001/ folder usable so the legacy
# demo flow still works while we migrate routes to real auth.
_DEMO_USER = {
    "id": "demo_001",
    "email": "demo@local",
    "verified_at": None,
    "created_at": None,
    "is_demo": True,
}


def current_user_optional(
    request: Request,
    response: Response,
    zotadvisor_session: Optional[str] = Cookie(default=None),
    zotadvisor_guest: Optional[str] = Cookie(default=None),
) -> dict:
    """Resolve the request's user, falling back to demo or guest identity."""
    if zotadvisor_session:
        user_id = security.verify_session(zotadvisor_session)
        if user_id:
            row = store.find_user_by_id(user_id)
            if row:
                # Drop the password hash — never want this in route handlers.
                row.pop("password_hash", None)
                row["is_demo"] = False
                row["is_guest"] = False
                return row
    if config.allow_shared_demo():
        return _DEMO_USER
    if config.allow_guest_users() and zotadvisor_guest:
        guest_id = security.verify_guest(zotadvisor_guest)
        if guest_id:
            return {
                "id": guest_id,
                "email": f"{guest_id}@guest.local",
                "verified_at": None,
                "created_at": None,
                "is_demo": False,
                "is_guest": True,
            }
    if config.allow_guest_users():
        guest_id = security.new_guest_id()
        response.set_cookie(
            key=security.GUEST_COOKIE_NAME,
            value=security.sign_guest(guest_id),
            max_age=security.GUEST_MAX_AGE_S,
            httponly=True,
            samesite="lax",
            secure=config.cookie_secure(),
            path="/",
        )
        return {
            "id": guest_id,
            "email": f"{guest_id}@guest.local",
            "verified_at": None,
            "created_at": None,
            "is_demo": False,
            "is_guest": True,
        }
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


def current_user_required(
    zotadvisor_session: Optional[str] = Cookie(default=None),
) -> dict:
    """Resolve the request's user; 401 if no valid session."""
    if not zotadvisor_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Not authenticated")
    user_id = security.verify_session(zotadvisor_session)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or expired session")
    row = store.find_user_by_id(user_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="User no longer exists")
    row.pop("password_hash", None)
    row["is_demo"] = False
    row["is_guest"] = False
    return row
