from datetime import datetime, timedelta

from app.data.anteater import AnteaterResult
from app.terms import FixedClock, LOS_ANGELES, TermKey
from app.terms.store import (
    InMemoryTermStateStore,
    JsonFileTermStateStore,
    TermStateSnapshot,
)
from app.terms.sync import TermStateSynchronizer


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=LOS_ANGELES)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def api_result(data, url="https://anteater.test/resource"):
    return AnteaterResult("ok", url, iso(NOW), data=data, http_status=200)


def calendar_data():
    return [
        {"year": "2026", "quarter": "Fall", "instructionStart": "2026-09-24"},
        {"year": "2027", "quarter": "Winter", "instructionStart": "2027-01-04"},
    ]


def terms_data():
    return [
        {"shortName": "2026 Fall"},
        {"shortName": "2027 Winter"},
    ]


def test_json_store_round_trip_and_corrupt_cache(tmp_path) -> None:
    path = tmp_path / "runtime" / "term_state.json"
    store = JsonFileTermStateStore(path)
    state = TermStateSnapshot(
        automatic_term="2026 Fall",
        calendar_records=calendar_data(),
        last_success_at=iso(NOW),
    )
    store.save(state)
    assert store.load() == state
    path.write_text("{broken", encoding="utf-8")
    assert store.load() is None


def fetchers(calls):
    def calendar():
        calls.append("calendar")
        return api_result(calendar_data(), "https://anteater.test/calendar/all")

    def terms():
        calls.append("terms")
        return api_result(terms_data(), "https://anteater.test/websoc/terms")
    return {"calendar_fetcher": calendar, "terms_fetcher": terms}


def state_synced(days_ago: float, **extra) -> TermStateSnapshot:
    return TermStateSnapshot(calendar_records=calendar_data(),
                             last_success_at=iso(NOW - timedelta(days=days_ago)), **extra)


def test_same_term_cache_skips_all_network_fetchers() -> None:
    # NOW is 22 Jul 2026: no regular term in the calendar has started yet.
    store = InMemoryTermStateStore(state_synced(100))

    def should_not_call():
        raise AssertionError("no term has started since the last sync")

    result = TermStateSynchronizer(
        store,
        clock=FixedClock(NOW),
        calendar_fetcher=should_not_call,
        terms_fetcher=should_not_call,
    ).sync_if_due()
    assert result.action == "skipped_fresh"


def test_calendar_is_fetched_once_per_term_when_a_term_begins() -> None:
    fall_start = datetime(2026, 9, 24, 8, 0, tzinfo=LOS_ANGELES)
    last = fall_start - timedelta(days=40)
    state = TermStateSnapshot(calendar_records=calendar_data(), last_success_at=iso(last),
                              availability={"2026 Fall": {"available": True}})
    store = InMemoryTermStateStore(state)
    calls: list[str] = []

    def sync_at(instant):
        return TermStateSynchronizer(store, clock=FixedClock(instant), **fetchers(calls)).sync_if_due()

    assert sync_at(fall_start - timedelta(days=1)).action == "skipped_fresh"    # 39 days old: not monthly
    assert calls == []
    first = sync_at(fall_start)                                                  # Fall began
    assert first.action == "synced" and calls == ["calendar", "terms"]
    assert first.state.last_success_at == iso(fall_start) and first.state.source == "anteater"
    assert first.state.websoc_terms == ["2026 Fall", "2027 Winter"]
    assert first.state.availability == {}                  # publication is no longer probed here
    assert sync_at(fall_start + timedelta(days=60)).action == "skipped_fresh"   # same term
    assert sync_at(datetime(2027, 1, 4, 0, 30, tzinfo=LOS_ANGELES)).action == "synced"   # Winter began
    assert calls == ["calendar", "terms"] * 2


def test_never_synced_or_very_old_cache_is_due() -> None:
    calls: list[str] = []
    fresh = InMemoryTermStateStore(TermStateSnapshot())
    assert TermStateSynchronizer(fresh, clock=FixedClock(NOW), **fetchers(calls)).sync_if_due().action == "synced"
    recent = InMemoryTermStateStore(TermStateSnapshot(last_success_at=iso(NOW - timedelta(days=120))))
    assert TermStateSynchronizer(recent, clock=FixedClock(NOW), **fetchers(calls)).sync_if_due().action == "skipped_fresh"
    old = InMemoryTermStateStore(TermStateSnapshot(last_success_at=iso(NOW - timedelta(days=120, seconds=1))))
    assert TermStateSynchronizer(old, clock=FixedClock(NOW), **fetchers(calls)).sync_if_due().action == "synced"
    assert calls == ["calendar", "terms"] * 2


def test_failed_sync_keeps_last_known_good_within_180_days() -> None:
    old_records = calendar_data()
    state = TermStateSnapshot(
        calendar_records=old_records,
        automatic_term="2026 Fall",
        source="anteater",
        status="fresh",
        last_success_at=iso(NOW - timedelta(days=179)),
    )
    store = InMemoryTermStateStore(state)
    failed = AnteaterResult("timeout", "https://anteater.test/calendar", iso(NOW), error="slow")
    result = TermStateSynchronizer(
        store,
        clock=FixedClock(NOW),
        calendar_fetcher=lambda: failed,
    ).sync_if_due()
    assert result.action == "using_last_known_good"
    assert result.state.calendar_records == old_records
    assert result.state.source == "last_known_good"
    assert result.state.status == "stale"


def test_failed_sync_after_180_days_activates_explicit_code_fallback() -> None:
    state = TermStateSnapshot(
        automatic_term="2025 Spring",
        source="anteater",
        status="fresh",
        last_success_at=iso(NOW - timedelta(days=180, seconds=1)),
    )
    store = InMemoryTermStateStore(state)
    failed = AnteaterResult("non_200", "https://anteater.test/calendar", iso(NOW), error="HTTP 503")
    result = TermStateSynchronizer(
        store,
        clock=FixedClock(NOW),
        calendar_fetcher=lambda: failed,
    ).sync_if_due()
    assert result.action == "fallback"
    assert result.state.automatic_term == "2026 Fall"
    assert result.state.source == "code_fallback"
    assert result.state.status == "fallback"


def test_missing_cache_is_immediately_usable_as_fallback() -> None:
    store = InMemoryTermStateStore()
    state = TermStateSynchronizer(store, clock=FixedClock(NOW)).state_for_use()
    assert state.automatic_term == "2026 Fall"
    assert state.source == "code_fallback"


def test_nonblocking_process_lock_prevents_duplicate_sync() -> None:
    store = InMemoryTermStateStore(TermStateSnapshot())
    synchronizer = TermStateSynchronizer(store, clock=FixedClock(NOW))
    with store.sync_lock() as acquired:
        assert acquired is True
        result = synchronizer.sync_if_due(force=True)
    assert result.action == "skipped_locked"
