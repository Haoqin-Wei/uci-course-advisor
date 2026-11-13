"""Default-term switching, as confirmed by the user on 2026-11-11.

The planning default moves to the next regular term as soon as that term's
complete timetable is stored, and at Week 7 Monday at the latest.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.catalog import store
from app.terms import FixedClock, LOS_ANGELES, TermKey
from app.terms.service import TermResolutionService
from app.terms.store import InMemoryTermStateStore, TermStateSnapshot

ROW = [{"section_code": "1", "department": "MATH", "course_number": "2A"}]
WEEK_4 = datetime(2026, 10, 20, 12, tzinfo=LOS_ANGELES)          # Fall 2026 began 24 Sep
WEEK_7 = datetime(2026, 11, 9, 0, tzinfo=LOS_ANGELES)


def make(when, *, automatic="2026 Fall", state_store=None):
    state_store = state_store or InMemoryTermStateStore(TermStateSnapshot(
        automatic_term=automatic, source="anteater", status="fresh",
        last_success_at=WEEK_4.isoformat(timespec="seconds"),
        calendar_records=[
            {"year": "2026", "quarter": "Fall", "instructionStart": "2026-09-24"},
            {"year": "2027", "quarter": "Winter", "instructionStart": "2027-01-04"},
            {"year": "2027", "quarter": "Spring", "instructionStart": "2027-03-29"},
        ],
    ))
    return TermResolutionService(state_store, clock=FixedClock(when)), state_store


def test_default_switches_as_soon_as_the_next_timetable_is_stored():
    service, state_store = make(WEEK_4)
    assert service.automatic_term().canonical_name == "2026 Fall"

    store.replace_term_sections("2027_Winter", ROW, source="anteater_websoc")      # the sync finished

    automatic = service.automatic_term()
    assert automatic.canonical_name == "2027 Winter" and automatic.data_available is True
    assert service.current_term().canonical_name == "2026 Fall"                    # the real term is unchanged
    transition = state_store.load().transition
    assert (transition["previous"], transition["current"]) == ("2026 Fall", "2027 Winter")
    assert transition["source"] == "timetable_published"
    assert transition["cutoff"] == "2026-11-09T00:00:00-08:00"                     # the Week 7 backstop it beat
    # "This quarter" still means the quarter in progress.
    assert [t.canonical_name for t in service.resolve_message("本学期 ICS 33 谁教").terms] == ["2026 Fall"]
    assert service.automatic_state()["automatic_term"] == "2027 Winter"


def test_week7_switches_even_when_nothing_is_published():
    before, _ = make(WEEK_7 - timedelta(seconds=1))
    assert before.automatic_term().canonical_name == "2026 Fall"

    service, state_store = make(WEEK_7)
    automatic = service.automatic_term()
    assert automatic.canonical_name == "2027 Winter"
    assert automatic.data_available is False and automatic.status == "unknown"     # says so; does not pretend
    assert state_store.load().transition["source"] == "calendar_week7"

    store.mark_term_unpublished("2027_Winter", source="anteater_websoc")
    assert service.automatic_term().status == "unavailable"                        # verified unpublished


@pytest.mark.parametrize("prepare", [
    lambda: store.replace_term_sections("2027_Winter", ROW, source="t", status="partial"),   # only partly posted
    lambda: store.mark_term_unpublished("2027_Winter", source="probe"),
    lambda: store.record_sync_failure("2027_Winter", "timeout"),
    lambda: store.replace_term_sections("2027_Spring", ROW, source="t"),                     # not the next term
    lambda: store.replace_term_sections("2026_Winter", ROW, source="t"),                     # last year's reference
])
def test_only_a_complete_timetable_of_the_immediately_next_term_switches(prepare):
    prepare()
    service, _ = make(WEEK_4)
    assert service.automatic_term().canonical_name == "2026 Fall"


def test_an_early_switch_is_never_rolled_back(tmp_path, monkeypatch):
    service, state_store = make(WEEK_4)
    store.replace_term_sections("2027_Winter", ROW, source="anteater_websoc")
    assert service.automatic_term().canonical_name == "2027 Winter"

    # The catalog file is lost; publication can no longer be confirmed.
    monkeypatch.setenv("CATALOG_DB_PATH", str(tmp_path / "empty.db"))
    from app.terms import availability
    availability.clear_cache()
    later, _ = make(WEEK_4 + timedelta(days=2), state_store=state_store)
    assert later.automatic_term().canonical_name == "2027 Winter"
    assert state_store.load().transition["source"] == "timetable_published"        # history is not rewritten


def test_next_switch_time_reported_to_the_frontend_is_week7_of_the_default_term():
    store.replace_term_sections("2027_Winter", ROW, source="anteater_websoc")
    service, _ = make(WEEK_4)
    assert service.automatic_state()["next_cutoff"] == "2027-02-15T00:00:00-08:00"  # Winter's own Week 7
    assert make(WEEK_4, automatic="2026 Fall")[0].automatic_term().switch_cutoff.isoformat() == \
        "2027-02-15T00:00:00-08:00"
