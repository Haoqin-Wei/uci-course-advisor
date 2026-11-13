from __future__ import annotations

from datetime import datetime, timezone

from app.catalog import store
from app.terms import TermKey
from app.terms.availability import catalog_availability, merged_availability
from app.terms.service import get_term_resolution_service
from app.terms.store import JsonFileTermStateStore, TermStateSnapshot

ROW = [{"section_code": "1", "department": "MATH", "course_number": "2A"}]


def save_state(paths, **fields):
    defaults = dict(
        automatic_term="2026 Fall", source="anteater", status="fresh",
        last_success_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        calendar_records=[
            {"year": "2026", "quarter": "Fall", "instructionStart": "2026-09-24"},
            {"year": "2027", "quarter": "Winter", "instructionStart": "2027-01-04"},
            {"year": "2027", "quarter": "Spring", "instructionStart": "2027-03-29"},
        ],
        websoc_terms=["2026 Fall"],
    )
    JsonFileTermStateStore(paths.term_state).save(TermStateSnapshot(**{**defaults, **fields}))


def test_availability_records_mirror_the_catalog_store():
    assert catalog_availability() == {}                       # nothing synced: no claims
    store.replace_term_sections("2027_Winter", ROW, source="t", synced_at="2026-11-11T23:43:29+00:00")
    store.mark_term_unpublished("2027_Spring", source="probe")
    store.record_sync_failure("2027_Fall", "timeout")

    records = catalog_availability()
    assert records["2027 Winter"] == {
        "available": True, "course_count": 1, "section_count": 1, "status": "complete",
        "checked_at": "2026-11-11T23:43:29+00:00", "source": "catalog_store"}
    assert records["2027 Spring"]["available"] is False       # verified unpublished
    assert records["2027 Fall"]["available"] is None          # only a failed attempt: unknown
    assert records["2027 Fall"]["checked_at"]


def test_stored_evidence_wins_and_state_records_only_fill_gaps():
    store.mark_term_unpublished("2027_Spring", source="probe")
    merged = merged_availability({
        "2027 Spring": {"available": True},                   # stale claim carried in a state file
        "2025 Spring": {"available": True, "section_count": 9},
    })
    assert merged["2027 Spring"]["available"] is False
    assert merged["2025 Spring"] == {"available": True, "section_count": 9}
    assert merged_availability(None) == catalog_availability()


def fixed_service(when):
    """Calendar-backed service at a fixed instant, independent of the wall clock."""
    from app.terms import FixedClock
    from app.terms.service import TermResolutionService
    from app.terms.store import InMemoryTermStateStore

    state = TermStateSnapshot(
        automatic_term="2026 Fall", source="anteater", status="fresh",
        last_success_at=when.isoformat(timespec="seconds"),
        calendar_records=[
            {"year": "2026", "quarter": "Fall", "instructionStart": "2026-09-24"},
            {"year": "2027", "quarter": "Winter", "instructionStart": "2027-01-04"},
            {"year": "2027", "quarter": "Spring", "instructionStart": "2027-03-29"},
        ],
        websoc_terms=["2026 Fall"],
    )
    return TermResolutionService(InMemoryTermStateStore(state), clock=FixedClock(when))


def test_term_service_reads_publication_from_synced_snapshots():
    from app.terms.clock import LOS_ANGELES

    service = fixed_service(datetime(2026, 10, 20, 12, tzinfo=LOS_ANGELES))    # Week 4 of Fall
    winter, spring = TermKey(2027, "Winter"), TermKey(2027, "Spring")

    before = service.resolve_explicit("2027 Winter").terms[0]
    assert before.data_available is False and before.status == "unknown"
    assert service.is_selectable_term(winter) is False
    assert "2027 Winter" not in service.available_terms()

    store.replace_term_sections("2027_Winter", ROW, source="t")
    store.mark_term_unpublished("2027_Spring", source="probe")

    after = service.resolve_explicit("2027 Winter").terms[0]
    assert after.data_available is True and after.status == "fresh"
    assert service.is_selectable_term(winter) is True          # without waiting for the term list
    assert "2027 Winter" in service.available_terms()
    unpublished = service.resolve_explicit("2027 Spring").terms[0]
    assert unpublished.data_available is False and unpublished.status == "unavailable"
    assert service.is_selectable_term(spring) is False


def test_operational_view_reports_the_automatic_terms_snapshot(isolated_test_environment):
    save_state(isolated_test_environment, availability={"2026 Fall": {"available": True, "section_count": 3}})
    service = get_term_resolution_service()
    automatic = service.automatic_state()["automatic_term"]
    year, quarter = automatic.split()
    assert service.automatic_state()["availability"] in (None, {"available": True, "section_count": 3})

    store.replace_term_sections(f"{year}_{quarter}", ROW, source="t")
    view = service.automatic_state()["availability"]
    assert view["available"] is True and view["source"] == "catalog_store" and view["section_count"] == 1
