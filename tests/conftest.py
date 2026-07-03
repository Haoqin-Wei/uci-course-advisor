from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

import pytest
import requests


# These values must be set before tests import application modules.
# app.llm.adapter loads .env during import, but python-dotenv does not
# override an environment variable that is already present, even when
# its value is empty.
for _key in ("DEEPSEEK_API_KEY", "ANTEATER_API_KEY", "RESEND_API_KEY"):
    os.environ[_key] = ""
os.environ["AUTH_SESSION_SECRET"] = "test-only-session-secret-not-for-production"


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    memory_root: Path
    auth_db: Path
    auth_secret: Path
    grades_cache: Path
    professor_summaries: Path
    logs: Path


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    root = tmp_path / "runtime"
    return RuntimePaths(
        root=root,
        memory_root=root / "memory",
        auth_db=root / "auth.db",
        auth_secret=root / "auth.secret",
        grades_cache=root / "grades_cache",
        professor_summaries=root / "professor_summaries",
        logs=root / "logs",
    )


def _blocked_network(*_args, **_kwargs):
    raise AssertionError(
        "External network access is disabled in default tests. "
        "Use a test double, or mark an explicitly manual test with @pytest.mark.live."
    )


def _blocked_llm_client():
    raise AssertionError(
        "Real LLM access is disabled in default tests. Inject a fake client instead."
    )


@pytest.fixture(autouse=True)
def isolated_test_environment(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
    request: pytest.FixtureRequest,
):
    """Keep default tests offline and isolate every mutable runtime path."""

    for key in ("DEEPSEEK_API_KEY", "ANTEATER_API_KEY", "RESEND_API_KEY"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv(
        "AUTH_SESSION_SECRET",
        "test-only-session-secret-not-for-production",
    )

    if request.node.get_closest_marker("live") is None:
        monkeypatch.setattr(requests.sessions.Session, "request", _blocked_network)
        monkeypatch.setattr(socket.socket, "connect", _blocked_network)
        monkeypatch.setattr(socket, "create_connection", _blocked_network)

    from app.auth import security, store
    from app.data import grades, professor_summary, sessions
    from app.routers import memory as memory_router
    from app.validation import log as validation_log

    monkeypatch.setattr(store, "DB_PATH", runtime_paths.auth_db)
    monkeypatch.setattr(security, "_SECRET_FILE", runtime_paths.auth_secret)
    monkeypatch.setattr(security, "_serializer", None)
    monkeypatch.setattr(sessions, "MEMORY_ROOT", runtime_paths.memory_root)
    monkeypatch.setattr(memory_router, "MEMORY_ROOT", runtime_paths.memory_root)
    monkeypatch.setattr(grades, "CACHE_DIR", runtime_paths.grades_cache)
    monkeypatch.setattr(
        professor_summary,
        "CACHE_DIR",
        runtime_paths.professor_summaries,
    )
    monkeypatch.setattr(validation_log, "LOG_DIR", runtime_paths.logs)
    monkeypatch.setattr(
        validation_log,
        "LOG_FILE",
        runtime_paths.logs / "validation.jsonl",
    )

    from app.llm import adapter

    monkeypatch.setattr(adapter, "DEEPSEEK_API_KEY", "")
    monkeypatch.setattr(adapter, "LLM_ENABLED", False)
    monkeypatch.setattr(adapter, "_client", None)
    monkeypatch.setattr(adapter, "_get_client", _blocked_llm_client)

    from app.memory import manager as memory_manager
    from app.memory.json_provider import JSONFileMemoryProvider

    fresh_memory_manager = memory_manager.MemoryManager()
    fresh_memory_manager.set_provider(
        JSONFileMemoryProvider(base_dir=str(runtime_paths.memory_root))
    )
    monkeypatch.setattr(memory_manager, "_manager", fresh_memory_manager)

    from app.agent import loop as agent_loop
    from app.data import anteater
    from app.data.uci_general import anteater_programs
    from app.modules import state as state_module

    state_module._sessions.clear()
    agent_loop._continuation_store.clear()
    anteater._course_cache.clear()
    anteater._sections_cache.clear()
    anteater._instructor_cache.clear()
    anteater_programs._departments_cache = None
    anteater_programs._majors_cache = None
    anteater_programs._major_detail_cache.clear()
    anteater_programs._ext_req_cache.clear()
    anteater_programs._courses_cache.clear()
    anteater_programs._all_courses_cache = None

    yield runtime_paths

    fresh_memory_manager.shutdown()
    state_module._sessions.clear()
    agent_loop._continuation_store.clear()


@pytest.fixture
def app_client(isolated_test_environment):
    """In-process client without starting the app lifespan/network warmup."""

    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
