"""
Auth router — email-code registration + password login.

Flow (frontend perspective):

    1. POST /api/auth/request_code   { email }
         → 6-digit code is "sent" (console-print in dev, Resend in prod)

    2. POST /api/auth/verify         { email, code, password }
         → on success: user created + session cookie set

    3. POST /api/auth/login          { email, password }
         → on success: session cookie set

    4. POST /api/auth/logout
         → cookie cleared

    5. GET  /api/auth/me
         → current user dict, or 401

Private-beta controls include endpoint rate limiting, generic login failures,
SameSite cookies, and a production origin/CSRF guard in the app middleware.
"""

from __future__ import annotations

import logging
import re
import secrets

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app import config
from app.auth.rate_limit import RateLimit, check_rate_limit
from app.auth import email_sender, security, store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

REQUEST_CODE_LIMIT = RateLimit("auth.request_code", limit=5, window_seconds=10 * 60)
VERIFY_LIMIT = RateLimit("auth.verify", limit=8, window_seconds=10 * 60)
LOGIN_LIMIT = RateLimit("auth.login", limit=10, window_seconds=10 * 60)
CURRENT_TERMS_VERSION = "2026-09-03"
CURRENT_PRIVACY_VERSION = "2026-09-03"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ── Request models ───────────────────────────────────────

class RequestCodeBody(BaseModel):
    email: str


class VerifyBody(BaseModel):
    email:    str
    code:     str
    password: str = Field(min_length=8, max_length=128)
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
            detail="Private testing is limited to verified @uci.edu email addresses.",
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

@router.post("/request_code")
def request_code(body: RequestCodeBody, request: Request):
    """
    Issue a fresh 6-digit verification code for `email` and dispatch
    it. The code is hashed before storage; only the latest issued
    code is valid (prior un-consumed codes for the same email are
    marked consumed inside stash_verification_code).
    """
    email = _require_uci_email(_norm_email(body.email))
    check_rate_limit(request, REQUEST_CODE_LIMIT, email)
    code = f"{secrets.randbelow(1_000_000):06d}"

    code_hash = security.hash_code(code)
    expires = store.stash_verification_code(email, code_hash)

    sent = email_sender.send_verification_code(email, code)
    if not sent:
        logger.error("[auth] email send failed")
        # We still stashed the code; if the user retries, a new one
        # is issued. Failing closed is fine.
        raise HTTPException(status_code=502, detail="Email send failed; try again")

    return {
        "ok": True,
        "email": email,
        "expires_at": expires.isoformat(),
        # NEVER include the code itself in the response.
    }


@router.post("/verify")
def verify(body: VerifyBody, response: Response, request: Request):
    """
    Consume the latest active verification code for `email` and
    create the account with the given password. On success the
    session cookie is set, so the client is logged in immediately
    — saves a round-trip vs forcing a separate /login after verify.
    """
    email = _require_uci_email(_norm_email(body.email))
    check_rate_limit(request, VERIFY_LIMIT, email)

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

    code_row = store.pop_latest_active_code(email)
    if not code_row:
        raise HTTPException(
            status_code=400,
            detail="No active verification code. Request a new one.",
        )
    if not security.verify_code(body.code, code_row["code_hash"]):
        # NOTE: pop_latest_active_code already marked the row
        # consumed, so a wrong submission also invalidates the code.
        # That makes brute-force harder without rate-limit infra.
        raise HTTPException(status_code=400, detail="Incorrect code")

    password_hash = security.hash_password(body.password)
    user = store.create_user(
        email,
        password_hash,
        age_18_attested=True,
        terms_version=body.terms_version,
        privacy_version=body.privacy_version,
    )

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
