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


def int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def web_search_enabled() -> bool:
    # Web search is opt-in in every environment. Production must set
    # WEB_SEARCH_ENABLED=true explicitly; development/tests stay offline
    # unless a caller enables a fake provider.
    return bool_env("WEB_SEARCH_ENABLED", default=False)


def web_search_provider() -> str:
    return os.environ.get("WEB_SEARCH_PROVIDER", "disabled").strip().lower() or "disabled"


def web_search_api_key() -> str:
    return os.environ.get("WEB_SEARCH_API_KEY", "").strip()


def web_search_max_results() -> int:
    return max(1, min(int_env("WEB_SEARCH_MAX_RESULTS", 5), 10))


def web_search_timeout_seconds() -> float:
    return max(0.5, min(float_env("WEB_SEARCH_TIMEOUT_SECONDS", 5.0), 30.0))
