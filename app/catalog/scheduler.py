"""Decide which snapshots are due for a sync, and run those.

Tracked terms, relative to the UCI calendar:
  - "current":   the term in progress (none during breaks)
  - "next":      the following regular term
  - "reference": last year's same quarter as "next" — a completeness yardstick
                 and the stand-in while "next" is unpublished

A term's timetable has one life cycle (one HTTP request per fetch):
  1. Before publication: nothing is requested until the day before the
     calendar's Schedule-of-Classes date (without that date: Week 5 of the
     current term). From then on the term is probed every 6 hours, so a new
     timetable arrives the same day. A partly posted timetable keeps that pace.
  2. From publication until the term's own Week 2 Friday (end of add/drop):
     refreshed daily — instructors, rooms, capacities and sections still move.
  3. After that Friday: fetched one last time, then never again.
The reference term is fetched once; past listings do not change. A failed
attempt is retried after an hour at the earliest, or after six hours when
usable rows are already stored.

The course catalogue (titles, prerequisites, GE labels, …) is one snapshot for
all terms. It is fetched once per term: whenever a tracked term's timetable was
first published after the catalogue was last synced.

``plan_due`` and the ``is_*_due`` functions are pure; ``run_due`` adds the
calendar lookup and the actual sync calls.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from app import observability
from app.catalog import course_store, store
from app.catalog.course_sync import sync_courses
from app.catalog.sync import SyncResult, sync_term

logger = logging.getLogger(__name__)

PROBE_INTERVAL = timedelta(hours=6)
ACTIVE_INTERVAL = timedelta(days=1)
RETRY_INTERVAL = timedelta(hours=1)
PROBE_LEAD = timedelta(days=1)          # start looking a day before the announced date
FALLBACK_PROBE_WEEK = 5                 # of the current term, when no date is announced

_ROLE_ORDER = {"reference": 0, "current": 1, "next": 2}
_run_lock = threading.Lock()


@dataclass(frozen=True)
class TrackedTerm:
    term_id: str
    role: str  # "current" | "next" | "reference"
    # No request for an unstored term before this instant; None = may probe now.
    probe_from: Optional[datetime] = None
    # The term's Week 2 Friday. Daily refresh ends here; None = still moving.
    active_until: Optional[datetime] = None


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _older_than(stamp: Optional[datetime], interval: timedelta, now: datetime) -> bool:
    return stamp is None or now - stamp >= interval


def is_due(term: TrackedTerm, snapshot: Optional[Mapping], now: datetime) -> bool:
    too_early = term.role != "reference" and term.probe_from is not None and now < term.probe_from
    if snapshot is None:
        return not too_early
    attempted = _parse(snapshot.get("last_attempt_at"))
    synced = _parse(snapshot.get("last_synced_at"))
    held = int(snapshot.get("section_count") or 0)
    complete = held > 0 and snapshot.get("status") == "complete"
    if not _older_than(attempted, RETRY_INTERVAL, now):
        return False  # Just tried (success or failure); never hammer the source.
    if term.role == "reference":
        return not complete
    if not complete:
        if not held and too_early:
            return False  # Not announced yet: do not ask.
        return _older_than(attempted, PROBE_INTERVAL, now)
    if held and snapshot.get("last_error") and not _older_than(attempted, PROBE_INTERVAL, now):
        return False  # Usable rows are stored; a failing source is retried calmly.
    if term.active_until is not None and now > term.active_until:
        # Add/drop is over. One fetch after the deadline freezes the final timetable.
        return synced is None or synced <= term.active_until
    return _older_than(synced, ACTIVE_INTERVAL, now)


def is_course_sync_due(
    snapshot: Optional[Mapping], now: datetime, *, latest_publication: Optional[datetime] = None,
) -> bool:
    """Whether the whole course catalogue should be fetched now.

    ``latest_publication`` is when a tracked term's timetable most recently
    appeared for the first time. A catalogue older than that predates the new
    term's courses.
    """
    if snapshot is None:
        return True
    attempted = _parse(snapshot.get("last_attempt_at"))
    if not _older_than(attempted, RETRY_INTERVAL, now):
        return False
    if not int(snapshot.get("course_count") or 0):
        return True
    if snapshot.get("last_error") and not _older_than(attempted, PROBE_INTERVAL, now):
        return False
    synced = _parse(snapshot.get("last_synced_at"))
    return latest_publication is not None and (synced is None or synced < latest_publication)


def plan_due(
    now: datetime, tracked: Iterable[TrackedTerm], snapshots: Mapping[str, Mapping],
) -> list[TrackedTerm]:
    """Tracked terms to sync now; reference terms first so comparisons can use them."""
    unique: dict[str, TrackedTerm] = {}
    for term in sorted(tracked, key=lambda item: _ROLE_ORDER.get(item.role, 9)):
        unique[term.term_id] = term  # current/next outrank a reference role
    due = [term for term in unique.values() if is_due(term, snapshots.get(term.term_id), now)]
    return sorted(due, key=lambda item: (_ROLE_ORDER.get(item.role, 9), item.term_id))


def _week_two_friday(instruction_start) -> Optional[datetime]:
    if instruction_start is None:
        return None
    from app.terms.calendar import calculate_week2_friday_cutoff
    return calculate_week2_friday_cutoff(instruction_start)


def _calendar_dates(service, key) -> dict:
    lookup = getattr(service, "calendar_dates", None)
    try:
        return dict(lookup(key)) if lookup else {}
    except Exception:
        return {}


def _probe_from(announced, current_instruction_start) -> Optional[datetime]:
    """When to start asking for the next term's timetable."""
    from app.terms.clock import LOS_ANGELES
    if announced is not None:
        return datetime.combine(announced, time.min, tzinfo=LOS_ANGELES) - PROBE_LEAD
    if current_instruction_start is not None:
        week1 = current_instruction_start + timedelta(days=(7 - current_instruction_start.weekday()) % 7)
        return datetime.combine(
            week1 + timedelta(weeks=FALLBACK_PROBE_WEEK - 1), time.min, tzinfo=LOS_ANGELES)
    return None


def tracked_terms(service=None) -> list[TrackedTerm]:
    """Current, next and reference terms from the calendar-backed term service."""
    if service is None:
        from app.terms.service import get_term_resolution_service
        service = get_term_resolution_service()
    tracked: list[TrackedTerm] = []
    current = service.current_term()
    current_start = None
    if current is not None:
        current_start = current.instruction_start
        settles = current.week2_friday_cutoff or _week_two_friday(current_start)
        tracked.append(TrackedTerm(f"{current.year}_{current.quarter}", "current", None, settles))
        upcoming = current.key.next_regular()
    else:
        upcoming = service.automatic_term().key  # During a break the default is the next term.
    dates = _calendar_dates(service, upcoming)
    tracked.append(TrackedTerm(
        f"{upcoming.year}_{upcoming.quarter}", "next",
        _probe_from(dates.get("soc_available"), current_start),
        _week_two_friday(dates.get("instruction_start")),
    ))
    tracked.append(TrackedTerm(f"{upcoming.year - 1}_{upcoming.quarter}", "reference"))
    return tracked


def _latest_publication(tracked, path) -> Optional[datetime]:
    watched = {term.term_id for term in tracked if term.role in {"current", "next"}}
    stamps = [
        _parse(item.get("published_detected_at"))
        for item in store.list_term_snapshots(path=path)
        if item["term_id"] in watched and int(item.get("section_count") or 0) > 0
    ]
    stamps = [stamp for stamp in stamps if stamp is not None]
    return max(stamps) if stamps else None


def _sync_courses_if_due(now, tracked, course_syncer, path) -> None:
    try:
        latest = _latest_publication(tracked, path)
        if not is_course_sync_due(course_store.get_course_snapshot(path=path), now, latest_publication=latest):
            return
        outcome = course_syncer(path=path)
    except Exception as exc:
        observability.increment("catalog.course_sync", result="error")
        observability.log_event(logger, logging.WARNING, "catalog_course_sync_error",
                                error=f"{type(exc).__name__}: {exc}")
        return
    observability.increment("catalog.course_sync", result=outcome.outcome)
    observability.log_event(
        logger, logging.INFO if outcome.ok else logging.WARNING, "catalog_course_sync",
        outcome=outcome.outcome, courses=outcome.course_count, pages=outcome.pages, error=outcome.error,
    )


def run_due(
    *,
    now: Optional[datetime] = None,
    service=None,
    syncer: Callable[..., SyncResult] = sync_term,
    course_syncer: Optional[Callable] = sync_courses,
    path: Optional[Path] = None,
) -> list[SyncResult]:
    """Sync every tracked term that is due, then the course catalogue if due.

    Returns the term results. Safe to call often; never raises. Pass
    ``course_syncer=None`` to leave the course catalogue alone.
    """
    if not _run_lock.acquire(blocking=False):
        return []  # Another pass is still downloading.
    try:
        now = now or datetime.now(timezone.utc)
        try:
            tracked = tracked_terms(service)
            snapshots = {item["term_id"]: item for item in store.list_term_snapshots(path=path)}
        except Exception as exc:
            observability.increment("catalog.sync", result="plan_error")
            observability.log_event(logger, logging.WARNING, "catalog_sync_plan_failed",
                                    error=f"{type(exc).__name__}: {exc}")
            return []
        results: list[SyncResult] = []
        for term in plan_due(now, tracked, snapshots):
            try:
                result = syncer(term.term_id, path=path)
            except Exception as exc:  # sync_term contains fetch errors; this is a last resort.
                observability.increment("catalog.sync", result="error", role=term.role)
                observability.log_event(logger, logging.WARNING, "catalog_sync_error",
                                        term=term.term_id, error=f"{type(exc).__name__}: {exc}")
                continue
            results.append(result)
            observability.increment("catalog.sync", result=result.outcome, role=term.role)
            observability.log_event(
                logger, logging.INFO if result.ok else logging.WARNING, "catalog_sync",
                term=term.term_id, role=term.role, outcome=result.outcome, status=result.status,
                sections=result.section_count, error=result.error,
            )
        if course_syncer is not None:
            _sync_courses_if_due(now, tracked, course_syncer, path)
        return results
    finally:
        _run_lock.release()
