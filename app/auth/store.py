"""
SQLite-backed account store with legacy verification-code storage.

Tables:
    users                — accounts (one row per email); new emails are unverified
    verification_codes   — retained for compatibility with old database files

Concurrency: a single uvicorn worker is the demo target. SQLite's
default check_same_thread=True is fine — we open a fresh connection
per call via the @contextmanager. If we scale horizontally later,
swap for Postgres without changing the call signatures.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path("data/auth.db")
CODE_TTL_MINUTES = 10


class EmailAlreadyRegistered(ValueError):
    """The normalized email already belongs to an account."""


_USER_COLUMNS = """
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    verified_at TEXT,
    created_at TEXT NOT NULL,
    age_18_attested_at TEXT,
    terms_accepted_at TEXT,
    terms_version TEXT,
    privacy_version TEXT
"""


# ── Connection + schema ──────────────────────────────────

def _init_schema(conn: sqlite3.Connection) -> None:
    columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(users)")}
    consent_columns = ("age_18_attested_at", "terms_accepted_at", "terms_version", "privacy_version")
    if all(name in columns for name in consent_columns) and not columns["verified_at"]["notnull"]:
        return

    # Serialize first-open upgrades, and roll the entire table rebuild back if
    # anything fails. Preserve IDs, password hashes and historical verification.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(f"CREATE TABLE IF NOT EXISTS users ({_USER_COLUMNS})")
        conn.execute("""CREATE TABLE IF NOT EXISTS verification_codes (
            email TEXT NOT NULL, code_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE INDEX IF NOT EXISTS ix_codes_email_created
                        ON verification_codes (email, created_at DESC)""")
        columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(users)")}
        for name in consent_columns:
            if name not in columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} TEXT")
        if columns["verified_at"]["notnull"]:
            schema_objects = conn.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name = 'users' AND sql IS NOT NULL "
                "AND type IN ('index', 'trigger')"
            ).fetchall()
            conn.execute(f"CREATE TABLE users_nullable_verification ({_USER_COLUMNS})")
            fields = "id, email, password_hash, verified_at, created_at, " + ", ".join(consent_columns)
            conn.execute(f"INSERT INTO users_nullable_verification ({fields}) SELECT {fields} FROM users")
            conn.execute("DROP TABLE users")
            conn.execute("ALTER TABLE users_nullable_verification RENAME TO users")
            for row in schema_objects:
                conn.execute(row["sql"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        _init_schema(conn)
        yield conn
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Users ────────────────────────────────────────────────

def find_user_by_email(email: str) -> Optional[dict]:
    email = (email or "").strip().lower()
    if not email:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email,),
        ).fetchone()
        return dict(row) if row else None


def find_user_by_id(user_id: str) -> Optional[dict]:
    if not user_id:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,),
        ).fetchone()
        return dict(row) if row else None


def create_user(
    email: str,
    password_hash: str,
    *,
    age_18_attested: bool = False,
    terms_version: Optional[str] = None,
    privacy_version: Optional[str] = None,
) -> dict:
    """Insert a new account without claiming that the email was verified."""
    email = email.strip().lower()
    uid = uuid.uuid4().hex
    now = _now_iso()
    attested_at = now if age_18_attested else None
    accepted_at = now if terms_version and privacy_version else None
    with _conn() as conn:
        try:
            conn.execute(
                """INSERT INTO users (
                       id, email, password_hash, verified_at, created_at,
                       age_18_attested_at, terms_accepted_at,
                       terms_version, privacy_version
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    uid, email, password_hash, None, now,
                    attested_at, accepted_at, terms_version, privacy_version,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            if exc.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE:
                raise EmailAlreadyRegistered(email) from exc
            raise
    return {
        "id": uid, "email": email, "password_hash": password_hash,
        "verified_at": None, "created_at": now,
        "age_18_attested_at": attested_at,
        "terms_accepted_at": accepted_at,
        "terms_version": terms_version,
        "privacy_version": privacy_version,
    }


def delete_user(user_id: str) -> bool:
    if not user_id:
        return False
    with _conn() as conn:
        conn.execute("DELETE FROM verification_codes WHERE email IN (SELECT email FROM users WHERE id = ?)", (user_id,))
        cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0


# ── Verification codes ───────────────────────────────────

def stash_verification_code(email: str, code_hash: str) -> datetime:
    """
    Store a freshly issued code hash. Returns the expires_at datetime
    so the caller can include it in the response if helpful for UX.

    Side effect: marks any prior un-consumed codes for this email as
    consumed, so only the latest issued code is valid. Without this,
    a user who re-requests a code would have BOTH codes work — that's
    an unexpected security and UX wart.
    """
    email = email.strip().lower()
    now_dt = datetime.now(timezone.utc)
    expires_dt = now_dt + timedelta(minutes=CODE_TTL_MINUTES)
    with _conn() as conn:
        conn.execute(
            "UPDATE verification_codes SET consumed = 1 WHERE email = ? AND consumed = 0",
            (email,),
        )
        conn.execute(
            """INSERT INTO verification_codes (email, code_hash, expires_at, consumed, created_at)
               VALUES (?, ?, ?, 0, ?)""",
            (email, code_hash, expires_dt.isoformat(), now_dt.isoformat()),
        )
        conn.commit()
    return expires_dt


def pop_latest_active_code(email: str) -> Optional[dict]:
    """
    Return the most recently issued un-consumed un-expired code row
    for this email, AND mark it consumed atomically so a successful
    verify can't be replayed. Returns None if no usable code exists.
    """
    email = email.strip().lower()
    now_iso = _now_iso()
    with _conn() as conn:
        row = conn.execute(
            """SELECT rowid, * FROM verification_codes
               WHERE email = ? AND consumed = 0 AND expires_at > ?
               ORDER BY created_at DESC LIMIT 1""",
            (email, now_iso),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE verification_codes SET consumed = 1 WHERE rowid = ?",
            (row["rowid"],),
        )
        conn.commit()
        return dict(row)
