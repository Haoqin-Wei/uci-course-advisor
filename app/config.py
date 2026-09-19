"""Runtime configuration helpers.

Values are read from environment variables at call time so tests can
monkeypatch production/dev behavior without re-importing the app.
"""

from __future__ import annotations

import os
from pathlib import Path


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
    # Explicit opt-in only. The private beta requires a verified account.
    return bool_env("ALLOW_SHARED_DEMO", default=False)


def allow_guest_users() -> bool:
    return bool_env("ALLOW_GUEST_USERS", default=False)


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
    # Production must set WEB_SEARCH_ENABLED=true explicitly. Development
    # defaults to enabled so "上网查一下" works out of the box; tests
    # override this to false in conftest to keep CI offline.
    return bool_env("WEB_SEARCH_ENABLED", default=not is_production())


def web_search_provider() -> str:
    default = "disabled" if is_production() else "duckduckgo"
    return os.environ.get("WEB_SEARCH_PROVIDER", default).strip().lower() or default


def web_search_api_key() -> str:
    return os.environ.get("WEB_SEARCH_API_KEY", "").strip()


def web_search_max_results() -> int:
    return max(1, min(int_env("WEB_SEARCH_MAX_RESULTS", 5), 10))


def web_search_timeout_seconds() -> float:
    return max(0.5, min(float_env("WEB_SEARCH_TIMEOUT_SECONDS", 5.0), 30.0))


def term_state_path() -> Path:
    return Path(os.environ.get("TERM_STATE_PATH", "data/runtime/term_state.json"))


def memory_provider() -> str:
    """Select the long-term memory backend.

    SQLite is the deployment default because it supplies indexed retrieval,
    provenance, temporal versioning, and soft deletion without adding an
    external service. ``json`` remains available as a rollback/demo backend.
    """
    value = os.environ.get("MEMORY_PROVIDER", "sqlite").strip().lower()
    return value if value in {"sqlite", "json"} else "sqlite"


def memory_root_path() -> Path:
    return Path(os.environ.get("MEMORY_ROOT", "data/memory"))


def memory_db_path() -> Path:
    return Path(
        os.environ.get(
            "MEMORY_DB_PATH",
            str(memory_root_path() / "long_term_memory.db"),
        )
    )


def memory_max_active_items() -> int:
    return max(50, min(int_env("MEMORY_MAX_ACTIVE_ITEMS", 1000), 100_000))


def academic_db_path() -> Path:
    """Server-side structured academic record store for the private beta."""
    return Path(os.environ.get("ACADEMIC_DB_PATH", "data/academic.db"))
