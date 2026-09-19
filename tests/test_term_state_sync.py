from datetime import datetime, timedelta

from app.data.anteater import AnteaterResult, TermAvailabilityResult
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


def available(term: TermKey):
    return TermAvailabilityResult(
        term=term,
        available=True,
        course_count=1,
        section_count=2,
        status="ok",
        source_url="https://anteater.test/websoc",
        checked_at=iso(NOW),
        content_length=123,
    )


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


def test_fresh_cache_skips_all_network_fetchers() -> None:
    state = TermStateSnapshot(last_success_at=iso(NOW - timedelta(days=30)))
    store = InMemoryTermStateStore(state)

    def should_not_call():
        raise AssertionError("fresh startup cache must not fetch")

    result = TermStateSynchronizer(
        store,
        clock=FixedClock(NOW),
        calendar_fetcher=should_not_call,
        terms_fetcher=should_not_call,
    ).sync_if_due()
    assert result.action == "skipped_fresh"


def test_cache_over_30_days_syncs_and_saves_availability() -> None:
    state = TermStateSnapshot(last_success_at=iso(NOW - timedelta(days=30, seconds=1)))
    store = InMemoryTermStateStore(state)
    calls = []

    def checker(term):
        calls.append(term.canonical_name)
        return available(term)

    result = TermStateSynchronizer(
        store,
        clock=FixedClock(NOW),
        calendar_fetcher=lambda: api_result(calendar_data(), "https://anteater.test/calendar/all"),
        terms_fetcher=lambda: api_result(terms_data(), "https://anteater.test/websoc/terms"),
        availability_checker=checker,
    ).sync_if_due()
    assert result.action == "synced"
    assert calls == ["2026 Fall", "2027 Winter"]
    assert result.state.last_success_at == iso(NOW)
    assert result.state.availability["2027 Winter"]["available"] is True
    assert result.state.source == "anteater"


def test_failed_sync_keeps_last_known_good_within_45_days() -> None:
    old_records = calendar_data()
    state = TermStateSnapshot(
        calendar_records=old_records,
        automatic_term="2026 Fall",
        source="anteater",
        status="fresh",
        last_success_at=iso(NOW - timedelta(days=44)),
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


def test_failed_sync_after_45_days_activates_explicit_code_fallback() -> None:
    state = TermStateSnapshot(
        automatic_term="2025 Spring",
        source="anteater",
        status="fresh",
        last_success_at=iso(NOW - timedelta(days=45, seconds=1)),
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


def test_failed_course_probe_does_not_discard_successful_calendar_refresh():
    store = InMemoryTermStateStore()

    def unavailable(term):
        return TermAvailabilityResult(term=term, available=False, course_count=0,
            section_count=0, status="timeout", source_url="https://anteater.test/websoc",
            checked_at=iso(NOW))

    result = TermStateSynchronizer(store, clock=FixedClock(NOW),
        calendar_fetcher=lambda: api_result(calendar_data()),
        terms_fetcher=lambda: api_result(terms_data()),
        availability_checker=unavailable).sync_if_due()
    assert result.action == "synced"
    assert result.state.calendar_records == calendar_data()
    assert result.state.availability["2027 Winter"]["available"] is None
