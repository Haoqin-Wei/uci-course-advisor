from datetime import datetime, timedelta

import pytest

from app.terms import FixedClock, LOS_ANGELES, TermKey
from app.terms.service import TermResolutionService
from app.terms.store import InMemoryTermStateStore, TermStateSnapshot


def calendar(term: TermKey, start: str) -> dict:
    return {"year": str(term.year), "quarter": term.quarter, "instructionStart": start}


def availability(*terms: TermKey) -> dict:
    return {
        term.canonical_name: {
            "available": True,
            "course_count": 1,
            "section_count": 1,
            "checked_at": "2026-01-01T00:00:00-08:00",
        }
        for term in terms
    }


def state(**updates) -> TermStateSnapshot:
    base = TermStateSnapshot(
        automatic_term="2026 Fall",
        source="anteater",
        status="fresh",
        last_success_at="2026-11-01T00:00:00-08:00",
        calendar_records=[
            calendar(TermKey(2026, "Fall"), "2026-09-24"),
            calendar(TermKey(2027, "Winter"), "2027-01-04"),
            calendar(TermKey(2027, "Spring"), "2027-03-29"),
        ],
        websoc_terms=["2026 Fall", "2027 Winter", "2027 Spring", "2026 Summer2"],
        availability=availability(
            TermKey(2026, "Fall"),
            TermKey(2027, "Winter"),
            TermKey(2027, "Spring"),
            TermKey(2026, "Summer2"),
        ),
    )
    for key, value in updates.items():
        setattr(base, key, value)
    return base


def service(snapshot: TermStateSnapshot, now: datetime) -> TermResolutionService:
    store = InMemoryTermStateStore(snapshot)
    return TermResolutionService(store, clock=FixedClock(now))


def test_cutoff_second_before_does_not_switch_and_instant_does() -> None:
    cutoff = datetime(2026, 11, 16, 0, 0, tzinfo=LOS_ANGELES)
    assert service(state(), cutoff - timedelta(seconds=1)).automatic_term().canonical_name == "2026 Fall"
    assert service(state(), cutoff).automatic_term().canonical_name == "2027 Winter"


@pytest.mark.parametrize("missing", ["calendar", "term_list", "course", "section"])
def test_publication_and_next_calendar_do_not_gate_week8_transition(missing: str) -> None:
    snapshot = state()
    if missing == "calendar":
        snapshot.calendar_records = snapshot.calendar_records[:1]
    elif missing == "term_list":
        snapshot.websoc_terms.remove("2027 Winter")
    elif missing == "course":
        snapshot.availability["2027 Winter"]["course_count"] = 0
    else:
        snapshot.availability["2027 Winter"]["section_count"] = 0
    now = datetime(2026, 11, 17, 12, 0, tzinfo=LOS_ANGELES)
    assert service(snapshot, now).automatic_term().canonical_name == "2027 Winter"


def test_recovery_can_advance_multiple_regular_terms_at_once() -> None:
    now = datetime(2027, 4, 12, 12, 0, tzinfo=LOS_ANGELES)
    resolved = service(state(last_success_at=now.isoformat()), now).automatic_term()
    assert resolved.canonical_name == "2027 Spring"


def test_summer_never_enters_automatic_sequence() -> None:
    snapshot = state(
        websoc_terms=["2026 Fall", "2026 Summer2"],
        calendar_records=[
            calendar(TermKey(2026, "Fall"), "2026-09-24"),
            calendar(TermKey(2026, "Summer2"), "2026-08-03"),
        ],
    )
    now = datetime(2026, 11, 17, 12, 0, tzinfo=LOS_ANGELES)
    assert service(snapshot, now).automatic_term().canonical_name == "2027 Winter"


def test_duplicate_or_invalid_calendar_record_refuses_inference() -> None:
    snapshot = state()
    snapshot.calendar_records.append(calendar(TermKey(2026, "Fall"), "2026-09-25"))
    now = datetime(2026, 11, 17, 12, 0, tzinfo=LOS_ANGELES)
    assert service(snapshot, now).automatic_term().canonical_name == "2026 Fall"


def test_transition_evidence_is_persisted() -> None:
    store = InMemoryTermStateStore(state())
    now = datetime(2026, 11, 17, 12, 0, tzinfo=LOS_ANGELES)
    TermResolutionService(store, clock=FixedClock(now)).automatic_term()
    transition = store.load().transition
    assert transition["previous"] == "2026 Fall"
    assert transition["current"] == "2027 Winter"
    assert transition["source"] == "calendar_week8"


def test_relative_message_uses_actual_term_after_default_advances() -> None:
    now = datetime(2026, 11, 17, 12, 0, tzinfo=LOS_ANGELES)
    resolution = service(state(), now).resolve_message("下学期")
    assert resolution.automatic.canonical_name == "2027 Winter"
    assert resolution.terms[0].canonical_name == "2027 Winter"
