import json
from datetime import datetime
from pathlib import Path

from app.data import sessions
from app.terms import FixedClock, LOS_ANGELES
from app.terms.conversation import (
    restore_automatic_default,
    set_manual_default,
    sync_automatic_default,
)
from app.terms.service import TermResolutionService
from app.terms.store import InMemoryTermStateStore, TermStateSnapshot


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=LOS_ANGELES)


def term_service() -> TermResolutionService:
    state = TermStateSnapshot(
        automatic_term="2026 Fall",
        source="anteater",
        status="fresh",
        last_success_at=NOW.isoformat(),
        calendar_records=[
            {"year": "2026", "quarter": "Spring", "instructionStart": "2026-03-30"},
            {"year": "2026", "quarter": "Fall", "instructionStart": "2026-09-24"},
            {"year": "2027", "quarter": "Winter", "instructionStart": "2027-01-04"},
        ],
        websoc_terms=["2026 Spring", "2026 Fall", "2027 Winter"],
        availability={
            name: {"available": True, "course_count": 1, "section_count": 1}
            for name in ("2026 Spring", "2026 Fall", "2027 Winter")
        },
    )
    return TermResolutionService(InMemoryTermStateStore(state), clock=FixedClock(NOW))


def test_new_conversation_metadata_is_auto(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sessions, "MEMORY_ROOT", tmp_path)
    sid = sessions.create_session("u1", default_term="2025 Spring")
    meta = sessions.get_session_meta("u1", sid)
    assert meta["term_mode"] == "auto"
    assert meta["term_source"] == "automatic"
    assert meta["default_term"] == "2025 Spring"
    assert meta["term_updated_by"] == "auto_sync"


def test_selector_choice_is_the_only_manual_mutation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sessions, "MEMORY_ROOT", tmp_path)
    sid = sessions.create_session("u1", default_term="2026 Fall")
    service = term_service()
    updated = set_manual_default("u1", sid, "Spring 2026", service)
    assert updated["term_mode"] == "manual"
    assert updated["default_term"] == "2026 Spring"
    assert updated["term_updated_by"] == "user_ui"
    assert service.effective_for_conversation(updated).canonical_name == "2026 Spring"


def test_selector_accepts_websoc_published_term_without_department_probe(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(sessions, "MEMORY_ROOT", tmp_path)
    sid = sessions.create_session("u1", default_term="2026 Fall")
    service = term_service()
    state = service.store.load()
    assert state is not None
    state.availability.pop("2026 Spring")
    service.store.save(state)

    assert "2026 Spring" in service.available_terms()
    updated = set_manual_default("u1", sid, "2026 Spring", service)

    assert updated["term_mode"] == "manual"
    assert updated["default_term"] == "2026 Spring"


def test_query_resolution_never_changes_default_term(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sessions, "MEMORY_ROOT", tmp_path)
    sid = sessions.create_session("u1", default_term="2026 Fall")
    service = term_service()
    before = sessions.get_session_meta("u1", sid)
    assert service.resolve_message("查询 2026 Spring").kind == "single"
    assert service.resolve_message("比较 2026 Spring 和 2026 Fall").kind == "multi"
    assert sessions.get_session_meta("u1", sid) == before


def test_restore_auto_uses_current_automatic_term(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sessions, "MEMORY_ROOT", tmp_path)
    sid = sessions.create_session("u1", default_term="2026 Fall")
    service = term_service()
    set_manual_default("u1", sid, "2026 Spring", service)
    meta = restore_automatic_default("u1", sid, service)
    assert meta["term_mode"] == "auto"
    assert meta["default_term"] == "2026 Fall"


def test_manual_conversation_ignores_automatic_sync(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sessions, "MEMORY_ROOT", tmp_path)
    sid = sessions.create_session("u1", default_term="2026 Fall")
    service = term_service()
    set_manual_default("u1", sid, "2026 Spring", service)
    sync_automatic_default("u1", sid, service.automatic_term())
    meta = sessions.get_session_meta("u1", sid)
    assert meta["term_mode"] == "manual"
    assert meta["default_term"] == "2026 Spring"


def test_legacy_session_migration_is_idempotent_and_preserves_turns(tmp_path) -> None:
    session_dir = tmp_path / "u1" / "sessions" / "sess_abc123"
    session_dir.mkdir(parents=True)
    original_meta = {
        "session_id": "sess_abc123",
        "user_id": "u1",
        "term_scope": "Spring 2025",
    }
    (session_dir / "meta.json").write_text(json.dumps(original_meta), encoding="utf-8")
    turns = '{"turn_index":1,"role":"user","content":"hello"}\n'
    (session_dir / "turns.jsonl").write_text(turns, encoding="utf-8")

    first = sessions.migrate_term_metadata(tmp_path)
    second = sessions.migrate_term_metadata(tmp_path)
    migrated = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    assert first == {"migrated": 1, "skipped": 0, "failed": 0}
    assert second == {"migrated": 0, "skipped": 1, "failed": 0}
    assert migrated["term_mode"] == "auto"
    assert migrated["default_term"] == "2025 Spring"
    assert "term_scope" not in migrated
    assert migrated["term_schema_version"] == sessions.TERM_SCHEMA_VERSION
    assert (session_dir / "turns.jsonl").read_text(encoding="utf-8") == turns
