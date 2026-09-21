from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.auth import security, store
from app.routers import auth


def registration_body(**updates):
    return {
        "email": "student@uci.edu",
        "password": "study-plan-123",
        "password_confirmation": "study-plan-123",
        "age_18_confirmed": True,
        "terms_accepted": True,
        "terms_version": auth.CURRENT_TERMS_VERSION,
        "privacy_version": auth.CURRENT_PRIVACY_VERSION,
        **updates,
    }


def test_registration_signs_in_without_email_or_business_records(app_client, runtime_paths, monkeypatch):
    from app.auth import email_sender

    def no_email(*args):
        pytest.fail("Registration must not send email")

    monkeypatch.setattr(email_sender, "send_verification_code", no_email)
    response = app_client.post("/api/auth/register", json=registration_body(email=" Student@UCI.EDU "))

    assert response.status_code == 200
    account = store.find_user_by_email("student@uci.edu")
    assert response.json()["user_id"] == account["id"]
    assert account["verified_at"] is None
    assert account["password_hash"] != "study-plan-123"
    assert security.verify_password("study-plan-123", account["password_hash"])
    assert account["terms_version"] == auth.CURRENT_TERMS_VERSION
    assert account["privacy_version"] == auth.CURRENT_PRIVACY_VERSION
    assert account["age_18_attested_at"]
    assert account["terms_accepted_at"]
    assert "password" not in response.text
    assert security.SESSION_COOKIE_NAME in response.cookies
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=lax" in response.headers["set-cookie"].lower()
    me = app_client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["id"] == account["id"]
    assert me.json()["user"]["verified_at"] is None
    assert "password_hash" not in me.json()["user"]
    assert not (runtime_paths.memory_root / account["id"]).exists()
    assert not runtime_paths.academic_db.exists()
    with sqlite3.connect(runtime_paths.auth_db) as conn:
        assert conn.execute("SELECT count(*) FROM verification_codes").fetchone()[0] == 0

    app_client.post("/api/auth/logout")
    assert app_client.get("/api/auth/me").status_code == 401
    login = app_client.post("/api/auth/login", json={"email": "STUDENT@uci.edu", "password": "study-plan-123"})
    assert login.status_code == 200
    assert login.json()["user_id"] == account["id"]


@pytest.mark.parametrize("updates,status", [
    ({"email": "student@gmail.com"}, 400),
    ({"email": "student@uci.edu.attacker.com"}, 400),
    ({"email": "student@sub.uci.edu"}, 400),
    ({"email": "student@@uci.edu"}, 400),
    ({"password_confirmation": "different-password"}, 400),
    ({"password": "short", "password_confirmation": "short"}, 422),
    ({"password": "a" * 73, "password_confirmation": "a" * 73}, 400),
    ({"password": "密" * 25, "password_confirmation": "密" * 25}, 400),
    ({"age_18_confirmed": False}, 400),
    ({"terms_accepted": False}, 400),
    ({"terms_version": "2026-09-03"}, 409),
    ({"privacy_version": "2026-09-03"}, 409),
])
def test_invalid_registration_creates_no_account_or_cookie(app_client, updates, status):
    body = registration_body(**updates)
    response = app_client.post("/api/auth/register", json=body)
    assert response.status_code == status
    assert security.SESSION_COOKIE_NAME not in response.cookies
    assert store.find_user_by_email(body["email"]) is None


def test_confirmation_is_required_by_the_server(app_client):
    body = registration_body()
    del body["password_confirmation"]
    response = app_client.post("/api/auth/register", json=body)
    assert response.status_code == 422
    assert store.find_user_by_email(body["email"]) is None


def test_duplicate_registration_keeps_original_password(app_client):
    assert app_client.post("/api/auth/register", json=registration_body()).status_code == 200
    response = app_client.post("/api/auth/register", json=registration_body(
        email="STUDENT@UCI.EDU", password="another-password", password_confirmation="another-password",
    ))
    assert response.status_code == 409
    account = store.find_user_by_email("student@uci.edu")
    assert security.verify_password("study-plan-123", account["password_hash"])
    assert not security.verify_password("another-password", account["password_hash"])


def test_registration_handles_insert_race_as_conflict(app_client, monkeypatch):
    def concurrent_insert(*args, **kwargs):
        raise store.EmailAlreadyRegistered("student@uci.edu")

    monkeypatch.setattr(store, "create_user", concurrent_insert)
    response = app_client.post("/api/auth/register", json=registration_body())
    assert response.status_code == 409
    assert security.SESSION_COOKIE_NAME not in response.cookies


def test_registration_rate_limit_covers_rotated_emails_and_forged_ip(app_client, monkeypatch):
    from app.auth.rate_limit import RateLimit

    monkeypatch.setattr(auth, "REGISTER_LIMIT", RateLimit("test.register", limit=1, window_seconds=60))
    first = app_client.post("/api/auth/register", json=registration_body(), headers={"X-Forwarded-For": "192.0.2.1"})
    second = app_client.post("/api/auth/register", json=registration_body(email="other@uci.edu"), headers={"X-Forwarded-For": "192.0.2.2"})
    assert first.status_code == 200
    assert second.status_code == 429
    assert store.find_user_by_email("other@uci.edu") is None


@pytest.mark.parametrize("path", ["/api/auth/request_code", "/api/auth/verify"])
def test_code_registration_endpoints_are_removed(app_client, path):
    assert app_client.post(path, json={}).status_code == 404


def test_production_registration_requires_same_origin_and_sets_secure_cookie(app_client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    monkeypatch.delenv("CSRF_PROTECTION", raising=False)
    blocked = app_client.post("/api/auth/register", json=registration_body())
    assert blocked.status_code == 403
    allowed = app_client.post("/api/auth/register", json=registration_body(), headers={"Origin": "http://testserver"})
    assert allowed.status_code == 200
    assert "secure" in allowed.headers["set-cookie"].lower()


@pytest.mark.parametrize("has_consent_columns", [False, True])
def test_old_database_migration_preserves_account_and_login(app_client, runtime_paths, has_consent_columns):
    password_hash = security.hash_password("old-password-123")
    verified_at = "2026-09-03T01:02:03+00:00"
    runtime_paths.auth_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(runtime_paths.auth_db) as conn:
        conn.execute("""CREATE TABLE users (
            id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, verified_at TEXT NOT NULL, created_at TEXT NOT NULL
        )""")
        conn.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?)", (
            "original-user-id", "existing@uci.edu", password_hash, verified_at, verified_at,
        ))
        if has_consent_columns:
            for name in ("age_18_attested_at", "terms_accepted_at", "terms_version", "privacy_version"):
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} TEXT")
            conn.execute("UPDATE users SET terms_version = '2026-09-03', privacy_version = '2026-09-03'")
        conn.execute("CREATE INDEX custom_users_created ON users(created_at)")

    original = store.find_user_by_id("original-user-id")
    assert original["verified_at"] == verified_at
    assert original["created_at"] == verified_at
    assert original["password_hash"] == password_hash
    assert original["terms_version"] == ("2026-09-03" if has_consent_columns else None)
    assert store.find_user_by_id("original-user-id") == original  # Reopening is idempotent.
    login = app_client.post("/api/auth/login", json={"email": "existing@uci.edu", "password": "old-password-123"})
    assert login.status_code == 200
    assert login.json()["user_id"] == "original-user-id"
    assert app_client.post("/api/auth/register", json=registration_body()).status_code == 200
    assert store.find_user_by_email("student@uci.edu")["verified_at"] is None
    with sqlite3.connect(runtime_paths.auth_db) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 2
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'custom_users_created'").fetchone()


def test_concurrent_first_registration_keeps_one_account():
    def create():
        try:
            return store.create_user("same@uci.edu", "test-hash")["id"]
        except store.EmailAlreadyRegistered:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: create(), range(4)))
    assert len([value for value in results if value]) == 1
    assert store.find_user_by_email("same@uci.edu")["id"] in results
