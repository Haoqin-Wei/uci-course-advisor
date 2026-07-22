import json
from datetime import datetime
from pathlib import Path

from app.data import sessions
from app.terms import FixedClock, LOS_ANGELES
from app.terms.conversation import commit_conversation_resolution
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
    sid = sessions.create_session("u1", term_scope="Spring 2025")
    meta = sessions.get_session_meta("u1", sid)
    assert meta["term_mode"] == "auto"
    assert meta["term_source"] == "automatic"


def test_successful_single_term_query_pins_and_history_resolves_it(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sessions, "MEMORY_ROOT", tmp_path)
    sid = sessions.create_session("u1")
    service = term_service()
    resolution = service.resolve_message("查询 2026 Spring")
    updated = commit_conversation_resolution(
        "u1", sid, resolution, answer_succeeded=True
    )
    assert updated["term_mode"] == "pinned"
    assert updated["term_scope"] == "2026 Spring"
    assert service.effective_for_conversation(updated).canonical_name == "2026 Spring"


def test_failed_or_blocked_or_multi_query_never_changes_term(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sessions, "MEMORY_ROOT", tmp_path)
    sid = sessions.create_session("u1")
    service = term_service()
    single = service.resolve_message("查询 2026 Spring")
    commit_conversation_resolution("u1", sid, single, answer_succeeded=False)
    assert sessions.get_session_meta("u1", sid)["term_mode"] == "auto"
    commit_conversation_resolution(
        "u1", sid, single, answer_succeeded=True, validation_blocked=True
    )
    assert sessions.get_session_meta("u1", sid)["term_mode"] == "auto"
    multi = service.resolve_message("比较 2026 Spring 和 2026 Fall")
    commit_conversation_resolution("u1", sid, multi, answer_succeeded=True)
    assert sessions.get_session_meta("u1", sid)["term_mode"] == "auto"


def test_current_term_resets_pinned_conversation_to_auto(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sessions, "MEMORY_ROOT", tmp_path)
    sid = sessions.create_session("u1")
    service = term_service()
    commit_conversation_resolution(
        "u1", sid, service.resolve_message("2026 Spring"), answer_succeeded=True
    )
    reset = service.resolve_message("回到当前学期")
    meta = commit_conversation_resolution("u1", sid, reset, answer_succeeded=True)
    assert meta["term_mode"] == "auto"
    assert meta["term_scope"] == "2026 Fall"


def test_unavailable_term_does_not_pin(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sessions, "MEMORY_ROOT", tmp_path)
    sid = sessions.create_session("u1")
    resolution = term_service().resolve_message("2028 Winter")
    assert resolution.all_available is False
    commit_conversation_resolution("u1", sid, resolution, answer_succeeded=True)
    assert sessions.get_session_meta("u1", sid)["term_mode"] == "auto"


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
    assert migrated["term_scope"] == "Spring 2025"
    assert (session_dir / "turns.jsonl").read_text(encoding="utf-8") == turns
