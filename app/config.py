"""Runtime configuration helpers.

Values are read from environment variables at call time so tests can
monkeypatch production/dev behavior without re-importing the app.
"""

from __future__ import annotations

import os


TRUE_VALUES = {"1", "true", "yes", "on"}


def env_name() -> str:
    return os.environ.get("APP_ENV", "development").strip().lower()


def is_production() -> bool:
    return env_name() in {"prod", "production"}


def bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUE_VALUES


def allow_shared_demo() -> bool:
    # Development keeps the old demo_001 flow; public/prod does not.
    return bool_env("ALLOW_SHARED_DEMO", default=not is_production())


def allow_guest_users() -> bool:
    return bool_env("ALLOW_GUEST_USERS", default=True)


def cookie_secure() -> bool:
    return bool_env("COOKIE_SECURE", default=is_production())


def csrf_protection_enabled() -> bool:
    return bool_env("CSRF_PROTECTION", default=is_production())


def allow_custom_system_prompt() -> bool:
    return bool_env("ALLOW_CUSTOM_SYSTEM_PROMPT", default=not is_production())


def allowed_origins() -> set[str]:
    raw = os.environ.get("ALLOWED_ORIGINS", "")
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}
