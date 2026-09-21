"""
Auth router — UCI email/password registration + password login.

Flow (frontend perspective):

    1. POST /api/auth/register       { email, password, password_confirmation, consent }
         → unverified account created + session cookie set

    2. POST /api/auth/login          { email, password }
         → on success: session cookie set

    3. POST /api/auth/logout
         → cookie cleared

    4. GET  /api/auth/me
         → current user dict, or 401

Private-beta controls include endpoint rate limiting, generic login failures,
SameSite cookies, and a production origin/CSRF guard in the app middleware.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app import config
from app.auth.rate_limit import RateLimit, check_rate_limit
from app.auth import security, store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

REGISTER_LIMIT = RateLimit("auth.register", limit=5, window_seconds=10 * 60)
LOGIN_LIMIT = RateLimit("auth.login", limit=10, window_seconds=10 * 60)
CURRENT_TERMS_VERSION = "2026-09-21"
CURRENT_PRIVACY_VERSION = "2026-09-21"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ── Request models ───────────────────────────────────────

class RegisterBody(BaseModel):
    email:    str
    password: str = Field(min_length=8, max_length=128, repr=False)
    password_confirmation: str = Field(min_length=1, max_length=128, repr=False)
    age_18_confirmed: bool
    terms_accepted: bool
    terms_version: str = Field(min_length=1, max_length=32)
    privacy_version: str = Field(min_length=1, max_length=32)


class LoginBody(BaseModel):
    email:    str
    password: str


class DeleteAccountBody(BaseModel):
    password: str = Field(min_length=1, max_length=128)


# ── Helpers ──────────────────────────────────────────────

def _norm_email(raw: str) -> str:
    e = (raw or "").strip().lower()
    if not _EMAIL_RE.match(e):
        raise HTTPException(status_code=400, detail="Invalid email format")
    return e


def _require_uci_email(email: str) -> str:
    local, sep, domain = email.rpartition("@")
    if not sep or not local or domain != "uci.edu":
        raise HTTPException(
            status_code=400,
            detail="Private testing is limited to @uci.edu email addresses.",
        )
    return email


def _set_session_cookie(response: Response, user_id: str) -> None:
    token = security.sign_session(user_id)
    response.set_cookie(
        key=security.SESSION_COOKIE_NAME,
        value=token,
        max_age=security.SESSION_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=config.cookie_secure(),
        path="/",
    )


# ── Endpoints ────────────────────────────────────────────

@router.post("/register")
def register(body: RegisterBody, response: Response, request: Request):
    """Create an unverified account and sign in without sending email."""
    # One IP budget across all submitted emails, before costly password hashing.
    check_rate_limit(request, REGISTER_LIMIT)
    email = _require_uci_email(_norm_email(body.email))
    if body.password != body.password_confirmation:
        raise HTTPException(status_code=400, detail="Passwords do not match")
    if len(body.password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Password must be at most 72 UTF-8 bytes")

    if not body.age_18_confirmed:
        raise HTTPException(status_code=400, detail="You must confirm that you are 18 or older.")
    if not body.terms_accepted:
        raise HTTPException(status_code=400, detail="Terms and Privacy Notice acceptance is required.")
    if (
        body.terms_version != CURRENT_TERMS_VERSION
        or body.privacy_version != CURRENT_PRIVACY_VERSION
    ):
        raise HTTPException(status_code=409, detail="Terms or Privacy Notice version is out of date.")

    if store.find_user_by_email(email):
        raise HTTPException(
            status_code=409,
            detail="Email is already registered. Use /login instead.",
        )

    password_hash = security.hash_password(body.password)
    try:
        user = store.create_user(
            email,
            password_hash,
            age_18_attested=True,
            terms_version=body.terms_version,
            privacy_version=body.privacy_version,
        )
    except store.EmailAlreadyRegistered as exc:
        # Another request may have registered this email since the lookup above.
        raise HTTPException(
            status_code=409, detail="Email is already registered. Use /login instead.",
        ) from exc

    _set_session_cookie(response, user["id"])
    logger.info("[auth] registered + logged in")
    return {"ok": True, "user_id": user["id"], "email": email}


@router.post("/login")
def login(body: LoginBody, response: Response, request: Request):
    email = _norm_email(body.email)
    check_rate_limit(request, LOGIN_LIMIT, email)
    user = store.find_user_by_email(email)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid email or password")
    if not security.verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid email or password")

    _set_session_cookie(response, user["id"])
    logger.info("[auth] login succeeded")
    return {"ok": True, "user_id": user["id"], "email": email}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(security.SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


# Importing here keeps the deps module from being a hard dep at module
# load time (helps tests that don't exercise auth at all).
from app.auth.deps import current_user_required  # noqa: E402
from fastapi import Depends  # noqa: E402


@router.get("/me")
def me(user: dict = Depends(current_user_required)):
    """Return the current user (sans password). 401 if no session."""
    return {"ok": True, "user": user}


@router.delete("/account")
def delete_account(
    body: DeleteAccountBody,
    response: Response,
    user: dict = Depends(current_user_required),
):
    full_user = store.find_user_by_id(user["id"])
    if not full_user or not security.verify_password(body.password, full_user["password_hash"]):
        raise HTTPException(status_code=401, detail="Password is incorrect")

    from app.privacy.deletion import delete_account_data

    delete_account_data(user["id"])
    response.delete_cookie(security.SESSION_COOKIE_NAME, path="/")
    logger.info("[auth] account_deleted")
    return {"ok": True}
