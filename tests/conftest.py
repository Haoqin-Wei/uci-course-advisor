from __future__ import annotations

import csv
import os
import shutil
import socket
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import requests

if TYPE_CHECKING:
    from app.catalog.term import Term
    from app.catalog.types import CourseRecord, SectionRecord


FIXTURES_ROOT = Path(__file__).parent / "fixtures"
EXTERNAL_ENV_KEYS = (
    "DEEPSEEK_API_KEY",
    "ANTEATER_API_KEY",
    "RESEND_API_KEY",
    "WEB_SEARCH_API_KEY",
)
_ORIGINAL_EXTERNAL_ENV = {key: os.environ.get(key) for key in EXTERNAL_ENV_KEYS}


# These values must be set before tests import application modules.
# app.llm.adapter loads .env during import, but python-dotenv does not
# override an environment variable that is already present, even when
# its value is empty.
for _key in EXTERNAL_ENV_KEYS:
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
    deep_search_history_db: Path
    term_state: Path


@dataclass(frozen=True)
class CatalogFixture:
    data_dir: Path
    term: Term
    course_rows: tuple[dict[str, str], ...]
    courses: tuple[CourseRecord, ...]
    sections: tuple[SectionRecord, ...]


@dataclass(frozen=True)
class SeededUserFixture:
    source_dir: Path
    memory_root: Path
    user_id: str
    session_id: str

    @property
    def user_dir(self) -> Path:
        return self.memory_root / self.user_id


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
        deep_search_history_db=root / "deep_search_history.db",
        term_state=root / "term_state.json",
    )


@pytest.fixture
def catalog_fixture_dir() -> Path:
    return FIXTURES_ROOT / "catalog"


@pytest.fixture
def minimal_catalog(catalog_fixture_dir: Path) -> CatalogFixture:
    """Load the synthetic Spring 2025 catalog through the production loader."""

    from app.catalog.loaders.uci_relational import UCIRelationalLoader
    from app.catalog.term import Term

    with (catalog_fixture_dir / "courses.csv").open(
        "r",
        encoding="utf-8",
        newline="",
    ) as file:
        course_rows = tuple(csv.DictReader(file))

    term = Term(year=2025, quarter="Spring")
    courses, sections = UCIRelationalLoader(catalog_fixture_dir).load(term)
    return CatalogFixture(
        data_dir=catalog_fixture_dir,
        term=term,
        course_rows=course_rows,
        courses=tuple(courses),
        sections=tuple(sections),
    )


@pytest.fixture
def seeded_user(
    isolated_test_environment: RuntimePaths,
) -> SeededUserFixture:
    """Copy the versioned user fixture into this test's isolated memory root."""

    user_id = "demo_001"
    session_id = "sess_abc123"
    source_dir = FIXTURES_ROOT / "memory" / user_id
    destination = isolated_test_environment.memory_root / user_id
    shutil.copytree(source_dir, destination)
    return SeededUserFixture(
        source_dir=source_dir,
        memory_root=isolated_test_environment.memory_root,
        user_id=user_id,
        session_id=session_id,
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

    live_test = request.node.get_closest_marker("live") is not None

    if live_test:
        for key, value in _ORIGINAL_EXTERNAL_ENV.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
    else:
        for key in EXTERNAL_ENV_KEYS:
            monkeypatch.setenv(key, "")
        monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "disabled")
    monkeypatch.setenv(
        "AUTH_SESSION_SECRET",
        "test-only-session-secret-not-for-production",
    )
    monkeypatch.setenv("TERM_STATE_PATH", str(runtime_paths.term_state))

    if not live_test:
        monkeypatch.setattr(requests.sessions.Session, "request", _blocked_network)
        monkeypatch.setattr(socket.socket, "connect", _blocked_network)
        monkeypatch.setattr(socket, "create_connection", _blocked_network)

    from app import observability
    from app.auth import rate_limit, security, store
    from app.data import deep_search_history, grades, professor_summary, sessions
    from app.terms.store import JsonFileTermStateStore, TermStateSnapshot
    from app.validation import log as validation_log

    monkeypatch.setattr(store, "DB_PATH", runtime_paths.auth_db)
    monkeypatch.setattr(security, "_SECRET_FILE", runtime_paths.auth_secret)
    monkeypatch.setattr(security, "_serializer", None)
    rate_limit.clear_rate_limits()
    observability.clear_metrics()
    monkeypatch.setattr(sessions, "MEMORY_ROOT", runtime_paths.memory_root)
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
    monkeypatch.setattr(
        deep_search_history,
        "DB_PATH",
        runtime_paths.deep_search_history_db,
    )
    JsonFileTermStateStore(runtime_paths.term_state).save(
        TermStateSnapshot(
            automatic_term="2025 Spring",
            source="anteater",
            status="fresh",
            last_success_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            calendar_records=[
                {
                    "year": "2025",
                    "quarter": "Spring",
                    "instructionStart": "2025-03-31",
                }
            ],
            websoc_terms=["2025 Spring"],
            availability={
                "2025 Spring": {
                    "available": True,
                    "course_count": 1,
                    "section_count": 1,
                }
            },
        )
    )

    from app.llm import adapter

    monkeypatch.setattr(adapter, "_client", None)
    if live_test:
        deepseek_api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        monkeypatch.setattr(adapter, "DEEPSEEK_API_KEY", deepseek_api_key)
        monkeypatch.setattr(adapter, "LLM_ENABLED", bool(deepseek_api_key))
    else:
        monkeypatch.setattr(adapter, "DEEPSEEK_API_KEY", "")
        monkeypatch.setattr(adapter, "LLM_ENABLED", False)
        monkeypatch.setattr(adapter, "_get_client", _blocked_llm_client)

    from app.memory import manager as memory_manager
    from app.memory.json_provider import JSONFileMemoryProvider

    fresh_memory_manager = memory_manager.MemoryManager()
    fresh_memory_manager.set_provider(
        JSONFileMemoryProvider(base_dir=str(runtime_paths.memory_root))
    )
    monkeypatch.setattr(memory_manager, "_manager", fresh_memory_manager)

    from app.agent import loop as agent_loop
    from app.data import anteater, db, deep_search, web_search
    from app.data.uci_general import anteater_programs
    agent_loop._continuation_store.clear()
    db._course_info_cache.clear()
    anteater._course_cache.clear()
    anteater._sections_cache.clear()
    anteater._live_sections_cache.clear()
    anteater._instructor_cache.clear()
    anteater_programs._departments_cache = None
    anteater_programs._majors_cache = None
    anteater_programs._major_detail_cache.clear()
    anteater_programs._ext_req_cache.clear()
    anteater_programs._courses_cache.clear()
    anteater_programs._all_courses_cache = None
    web_search.clear_web_search_state()
    deep_search.clear_deep_search_state()

    yield runtime_paths

    fresh_memory_manager.shutdown()
    agent_loop._continuation_store.clear()
    web_search.clear_web_search_state()
    deep_search.clear_deep_search_state()
    rate_limit.clear_rate_limits()
    observability.clear_metrics()


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
