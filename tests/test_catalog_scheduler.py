from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import config
from app.catalog import course_store, scheduler, store
from app.catalog.course_sync import CourseSyncResult
from app.catalog.scheduler import TrackedTerm, is_course_sync_due, is_due, plan_due, run_due, tracked_terms
from app.catalog.sync import SyncResult
from app.terms.models import TermKey

NOW = datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc)


def ago(**delta):
    return (NOW - timedelta(**delta)).isoformat(timespec="seconds")


def snap(status="complete", count=100, synced=None, attempted=None, error=None):
    return {"status": status, "section_count": count, "last_synced_at": synced,
            "last_attempt_at": attempted or synced, "last_error": error}


NEXT = TrackedTerm("2027_Winter", "next", active_until=NOW + timedelta(days=65))
NOT_ANNOUNCED = TrackedTerm("2027_Spring", "next", probe_from=NOW + timedelta(days=3))
ANNOUNCED = TrackedTerm("2027_Spring", "next", probe_from=NOW - timedelta(hours=1))
CURRENT_SETTLED = TrackedTerm("2026_Fall", "current", None, NOW - timedelta(days=20))
CURRENT_ACTIVE = TrackedTerm("2026_Fall", "current", None, NOW + timedelta(days=3))
REFERENCE = TrackedTerm("2026_Winter", "reference")


@pytest.mark.parametrize("term, snapshot, expected", [
    # 1. before publication
    (NOT_ANNOUNCED, None, False),                                               # no request before the date
    (NOT_ANNOUNCED, snap("unpublished", 0, attempted=ago(days=2)), False),
    (NOT_ANNOUNCED, snap("unknown", 0, attempted=ago(days=2), error="timeout"), False),
    (NOT_ANNOUNCED, snap("partial", 40, synced=ago(hours=7)), True),            # it is already being posted
    (ANNOUNCED, None, True),
    (NEXT, None, True),                                                         # no date known: just ask
    (ANNOUNCED, snap("unpublished", 0, attempted=ago(hours=5)), False),         # probed recently
    (ANNOUNCED, snap("unpublished", 0, attempted=ago(hours=6)), True),          # probe again
    (ANNOUNCED, snap("unknown", 0, attempted=ago(minutes=30), error="timeout"), False),
    (ANNOUNCED, snap("unknown", 0, attempted=ago(hours=7), error="timeout"), True),
    (NEXT, snap("partial", 40, synced=ago(hours=7)), True),                     # still being posted
    (NEXT, snap("partial", 40, synced=ago(hours=2)), False),
    # 2. published, until the term's Week 2 Friday: daily
    (NEXT, snap(synced=ago(hours=23)), False),
    (NEXT, snap(synced=ago(hours=24)), True),
    (CURRENT_ACTIVE, snap(synced=ago(hours=25)), True),
    (NEXT, snap(synced=ago(days=3), attempted=ago(hours=2), error="rejected_shrink"), False),
    (NEXT, snap(synced=ago(days=3), attempted=ago(hours=7), error="rejected_shrink"), True),
    (NEXT, snap(synced="2026-05-13T04:51:36.251Z", attempted=ago(days=1)), True),   # CSV-era stamp
    # 3. after that Friday: one final fetch, then never
    (CURRENT_SETTLED, snap(synced=ago(days=21)), True),                         # last synced before the deadline
    (CURRENT_SETTLED, snap(synced=ago(days=19)), False),                        # already has the final timetable
    (CURRENT_SETTLED, snap(synced=ago(days=1)), False),
    (CURRENT_SETTLED, None, True),                                              # fresh install mid-term
    (CURRENT_SETTLED, snap("partial", 40, synced=ago(days=1)), True),
    # reference
    (REFERENCE, None, True),
    (REFERENCE, snap(synced=ago(days=400)), False),                             # the past is fetched once
    (REFERENCE, snap("partial", 50, synced=ago(days=2)), True),
    (REFERENCE, snap("unknown", 0, attempted=ago(minutes=59), error="x"), False),
    (REFERENCE, snap("unknown", 0, attempted=ago(minutes=61), error="x"), True),
])
def test_is_due_follows_the_timetable_life_cycle(term, snapshot, expected):
    assert is_due(term, snapshot, NOW) is expected


def test_plan_orders_reference_first_and_deduplicates_roles():
    tracked = [NEXT, CURRENT_ACTIVE, REFERENCE, TrackedTerm("2026_Fall", "reference")]
    plan = plan_due(NOW, tracked, {})
    assert [(t.term_id, t.role) for t in plan] == [
        ("2026_Winter", "reference"), ("2026_Fall", "current"), ("2027_Winter", "next")]
    fresh = {"2026_Winter": snap(synced=ago(days=1)), "2026_Fall": snap(synced=ago(hours=1))}
    assert [t.term_id for t in plan_due(NOW, tracked, fresh)] == ["2027_Winter"]


def service(current, automatic, *, current_start=date(2026, 9, 24), dates=None):
    def resolved(key, start=None):
        return SimpleNamespace(year=key.year, quarter=key.quarter, key=key,
                               week2_friday_cutoff=None, instruction_start=start)
    return SimpleNamespace(
        current_term=lambda: resolved(current, current_start) if current else None,
        automatic_term=lambda: resolved(automatic),
        calendar_dates=lambda key: (dates or {}).get(key, {}),
    )


def test_tracked_terms_take_their_dates_from_the_calendar():
    winter = TermKey(2027, "Winter")
    during = tracked_terms(service(TermKey(2026, "Fall"), winter, dates={
        winter: {"instruction_start": date(2027, 1, 4), "soc_available": date(2026, 10, 31)}}))
    assert [(t.term_id, t.role) for t in during] == [
        ("2026_Fall", "current"), ("2027_Winter", "next"), ("2026_Winter", "reference")]
    fall, nxt, _ = during
    assert fall.probe_from is None and fall.active_until.date() == date(2026, 10, 9)   # Fall's Week 2 Friday
    assert nxt.probe_from.date() == date(2026, 10, 30) and nxt.probe_from.tzinfo is not None   # a day early
    assert nxt.active_until.date() == date(2027, 1, 15)                                # Winter's Week 2 Friday

    # No announced date: start in Week 5 of the current term (Week 1 began Mon 28 Sep).
    undated = tracked_terms(service(TermKey(2026, "Fall"), winter))[1]
    assert undated.probe_from.date() == date(2026, 10, 26) and undated.active_until is None

    spring = tracked_terms(service(TermKey(2026, "Spring"), TermKey(2026, "Spring"), current_start=None))
    assert [t.term_id for t in spring] == ["2026_Spring", "2026_Fall", "2025_Fall"]    # Spring → Fall
    assert spring[1].probe_from is None                                                # nothing known: may ask

    on_break = tracked_terms(service(None, winter))
    assert [(t.term_id, t.role) for t in on_break] == [("2027_Winter", "next"), ("2026_Winter", "reference")]


def test_real_term_service_exposes_calendar_dates(isolated_test_environment):
    from app.terms.service import get_term_resolution_service
    from app.terms.store import JsonFileTermStateStore, TermStateSnapshot

    JsonFileTermStateStore(isolated_test_environment.term_state).save(TermStateSnapshot(
        automatic_term="2026 Fall", source="anteater", status="fresh",
        last_success_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        calendar_records=[
            {"year": "2027", "quarter": "Winter", "instructionStart": "2027-01-04", "socAvailable": "2026-10-31"},
            {"year": "2027", "quarter": "Spring", "instructionStart": "2027-03-29", "socAvailable": "soon"},
        ]))
    svc = get_term_resolution_service()
    assert svc.calendar_dates(TermKey(2027, "Winter")) == {
        "instruction_start": date(2027, 1, 4), "soc_available": date(2026, 10, 31)}
    assert svc.calendar_dates(TermKey(2027, "Spring")) == {
        "instruction_start": date(2027, 3, 29), "soc_available": None}
    assert svc.calendar_dates(TermKey(2030, "Fall")) == {"instruction_start": None, "soc_available": None}


def test_run_due_syncs_only_what_is_due_and_survives_errors(tmp_path):
    db = tmp_path / "catalog.db"
    store.replace_term_sections("2026_Fall", [
        {"section_code": "1", "department": "MATH", "course_number": "2A"}], source="t", path=db)
    calls = []

    def syncer(term_id, *, path):
        calls.append(term_id)
        if term_id == "2026_Winter":
            raise RuntimeError("boom")
        return SyncResult(term_id, "synced", "complete", 5)

    svc = service(TermKey(2026, "Fall"), TermKey(2027, "Winter"))
    now = datetime.now(timezone.utc)
    results = run_due(now=now, service=svc, syncer=syncer, course_syncer=None, program_syncer=None, path=db)
    assert calls == ["2026_Winter", "2027_Winter"]            # Fall was synced a moment ago
    assert [r.term_id for r in results] == ["2027_Winter"]

    calls.clear()
    run_due(now=now + timedelta(days=30), service=svc, syncer=syncer, course_syncer=None, program_syncer=None, path=db)
    # Fall is past its Week 2 Friday and already holds its final timetable.
    assert calls == ["2026_Winter", "2027_Winter"]


def test_run_due_never_raises_and_does_not_overlap(tmp_path):
    def broken_service():
        raise RuntimeError("calendar unavailable")
    assert run_due(service=SimpleNamespace(current_term=broken_service), path=tmp_path / "c.db") == []

    assert scheduler._run_lock.acquire(blocking=False)
    try:
        called = []
        assert run_due(service=service(None, TermKey(2027, "Winter")),
                       syncer=lambda *a, **k: called.append(1),
                       course_syncer=lambda **k: called.append(2),
                       program_syncer=lambda **k: called.append(3), path=tmp_path / "c.db") == []
        assert called == []
    finally:
        scheduler._run_lock.release()


def test_auto_sync_flag_defaults_on_but_is_off_in_tests(monkeypatch):
    assert config.catalog_auto_sync_enabled() is False
    monkeypatch.delenv("CATALOG_AUTO_SYNC_ENABLED")
    assert config.catalog_auto_sync_enabled() is True


def csnap(count=9000, synced=None, attempted=None, error=None):
    return {"course_count": count, "last_synced_at": synced,
            "last_attempt_at": attempted or synced, "last_error": error}


PUBLISHED = NOW - timedelta(days=10)


@pytest.mark.parametrize("snapshot, published, expected", [
    (None, None, True),                                               # never fetched
    (csnap(synced=ago(days=60)), None, False),                        # no timetable has appeared since
    (csnap(synced=ago(days=60)), PUBLISHED, True),                    # a new term arrived after the last sync
    (csnap(synced=ago(days=9)), PUBLISHED, False),                    # already fetched for this term
    (csnap(synced=ago(days=400)), NOW - timedelta(days=500), False),  # once per term, not per week
    (csnap(0, attempted=ago(minutes=30), error="timeout"), None, False),
    (csnap(0, attempted=ago(hours=2), error="timeout"), None, True),
    (csnap(synced=ago(days=60), attempted=ago(hours=2), error="page 3: timeout"), PUBLISHED, False),
    (csnap(synced=ago(days=60), attempted=ago(hours=7), error="page 3: timeout"), PUBLISHED, True),
])
def test_course_catalogue_is_fetched_once_per_term(snapshot, published, expected):
    assert is_course_sync_due(snapshot, NOW, latest_publication=published) is expected


def test_run_due_refreshes_courses_when_a_timetable_first_arrives(tmp_path):
    db = tmp_path / "catalog.db"
    svc = service(TermKey(2026, "Fall"), TermKey(2027, "Winter"), current_start=None)   # no dates: may probe
    course_calls = []
    row = [{"section_code": "1", "department": "MATH", "course_number": "2A"}]

    clock = {}

    def stamp():
        return clock["now"].isoformat(timespec="seconds")

    def course_syncer(*, path):
        course_calls.append(path)
        course_store.replace_courses([{"department": "MATH", "course_number": "2A"}],
                                     source="t", synced_at=stamp(), force=True, path=path)
        return CourseSyncResult("synced", 1, 0, 1)

    def storing(term_id, *, path):
        result = store.replace_term_sections(term_id, row, source="t", synced_at=stamp(), force=True, path=path)
        return SyncResult(term_id, "synced", "complete", 1, result.previous_section_count)

    def idle(term_id, *, path):
        return SyncResult(term_id, "failed", None, 0, 0, "offline")

    # Ahead of the wall clock, so the store's own "last attempt" stamps lie in the past.
    start = datetime.now(timezone.utc) + timedelta(days=1)

    def run(days, syncer):
        clock["now"] = start + timedelta(days=days)
        return run_due(now=clock["now"], service=svc, syncer=syncer, course_syncer=course_syncer,
                       program_syncer=None, path=db)

    run(0, idle)
    assert course_calls == [db]                                       # nothing stored yet: fetch once
    run(20, idle)
    assert len(course_calls) == 1                                     # no weekly refresh any more
    run(21, storing)
    assert len(course_calls) == 2                                     # Winter's timetable just appeared
    run(40, storing)
    assert len(course_calls) == 2                                     # same term refreshed: nothing new

    def broken(*, path):
        raise RuntimeError("boom")
    course_store.record_course_sync_failure("x", path=db)
    assert run_due(now=start + timedelta(days=90), service=svc, syncer=idle,
                   course_syncer=broken, program_syncer=broken, path=db) is not None   # contained
