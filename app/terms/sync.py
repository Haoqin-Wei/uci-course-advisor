"""Freshness-gated Anteater synchronization for automatic term state."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Callable, Optional

from app import config, observability
from app.data import anteater
from app.terms.clock import Clock, SystemClock
from app.terms.models import TermKey
from app.terms.parser import parse_term_key
from app.terms.store import JsonFileTermStateStore, TermStateSnapshot, TermStateStore


logger = logging.getLogger(__name__)

SYNC_INTERVAL = timedelta(days=30)
MAX_CACHE_AGE = timedelta(days=45)
FALLBACK_AUTOMATIC_TERM = TermKey(2026, "Fall")


@dataclass(frozen=True)
class SyncResult:
    action: str
    state: TermStateSnapshot
    error: Optional[str] = None


def _iso(instant: datetime) -> str:
    return instant.isoformat(timespec="seconds")


def _parse_instant(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def cache_age(state: TermStateSnapshot, now: datetime) -> Optional[timedelta]:
    last_success = _parse_instant(state.last_success_at)
    if last_success is None:
        return None
    return now - last_success.astimezone(now.tzinfo)


def fallback_state(
    *,
    now: datetime,
    previous: Optional[TermStateSnapshot] = None,
    error: Optional[str] = None,
    fallback_term: TermKey = FALLBACK_AUTOMATIC_TERM,
) -> TermStateSnapshot:
    previous = previous or TermStateSnapshot()
    return replace(
        previous,
        automatic_term=fallback_term.canonical_name,
        source="code_fallback",
        status="fallback",
        last_attempt_at=_iso(now),
        last_error=error,
    )


def _calendar_term(record: dict) -> Optional[TermKey]:
    candidates = [
        record.get("shortName"),
        record.get("term"),
        (
            f"{record.get('year')} {record.get('quarter')}"
            if record.get("year") and record.get("quarter")
            else None
        ),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        parsed = parse_term_key(str(candidate))
        if parsed.kind == "single":
            return parsed.terms[0]
    return None


def _term_sort_key(term: TermKey) -> tuple[int, int]:
    quarter_order = {"Winter": 0, "Spring": 1, "Fall": 2}
    return term.year, quarter_order.get(term.quarter, 3)


class TermStateSynchronizer:
    def __init__(
        self,
        store: TermStateStore,
        *,
        clock: Optional[Clock] = None,
        calendar_fetcher: Callable = anteater.fetch_calendar_all,
        terms_fetcher: Callable = anteater.fetch_websoc_terms,
        availability_checker: Callable = anteater.check_term_data_availability,
        fallback_term: TermKey = FALLBACK_AUTOMATIC_TERM,
    ):
        self.store = store
        self.clock = clock or SystemClock()
        self.calendar_fetcher = calendar_fetcher
        self.terms_fetcher = terms_fetcher
        self.availability_checker = availability_checker
        self.fallback_term = fallback_term

    def state_for_use(self) -> TermStateSnapshot:
        now = self.clock.now()
        state = self.store.load()
        if state is None:
            state = fallback_state(now=now, fallback_term=self.fallback_term)
            self.store.save(state)
            return state
        age = cache_age(state, now)
        if age is None or age > MAX_CACHE_AGE:
            if state.source != "code_fallback" or state.status != "fallback":
                state = fallback_state(
                    now=now,
                    previous=state,
                    error=state.last_error or "term cache is older than 45 days",
                    fallback_term=self.fallback_term,
                )
                self.store.save(state)
                self._log_fallback(state)
        return state

    def sync_if_due(self, *, force: bool = False) -> SyncResult:
        now = self.clock.now()
        state = self.store.load()
        if state is None:
            state = fallback_state(now=now, fallback_term=self.fallback_term)

        age = cache_age(state, now)
        if not force and age is not None and age <= SYNC_INTERVAL:
            return SyncResult("skipped_fresh", state)

        with self.store.sync_lock() as acquired:
            if not acquired:
                observability.increment("term.sync", result="skipped_locked")
                return SyncResult("skipped_locked", state)

            # Re-read after lock acquisition in case another caller just synced.
            current = self.store.load() or state
            current_age = cache_age(current, now)
            if not force and current_age is not None and current_age <= SYNC_INTERVAL:
                return SyncResult("skipped_fresh", current)

            current = replace(current, last_attempt_at=_iso(now))
            self.store.save(current)
            observability.increment("term.sync", result="started")
            observability.log_event(logger, logging.INFO, "term_sync_started")

            calendar_result = self.calendar_fetcher()
            if not calendar_result.ok:
                return self._sync_failed(current, now, f"calendar:{calendar_result.status}")
            terms_result = self.terms_fetcher()
            if not terms_result.ok:
                return self._sync_failed(current, now, f"websoc_terms:{terms_result.status}")

            calendar_records = calendar_result.data or []
            calendar_keys = {
                key for record in calendar_records if (key := _calendar_term(record))
            }
            websoc_keys: set[TermKey] = set()
            for record in terms_result.data or []:
                parsed = parse_term_key(str(record.get("shortName") or ""))
                if parsed.kind == "single":
                    websoc_keys.add(parsed.terms[0])

            candidates = sorted(
                (
                    key
                    for key in calendar_keys & websoc_keys
                    if key.is_regular and _term_sort_key(key) >= _term_sort_key(self.fallback_term)
                ),
                key=_term_sort_key,
            )[:12]

            availability: dict[str, dict] = {}
            for candidate in candidates:
                checked = self.availability_checker(candidate)
                observability.log_event(
                    logger,
                    logging.INFO,
                    "term_availability_checked",
                    term=candidate.canonical_name,
                    status=checked.status,
                    course_count=checked.course_count,
                    section_count=checked.section_count,
                    content_length=checked.content_length,
                    source_url=checked.source_url,
                )
                if checked.status != "ok":
                    return self._sync_failed(
                        current,
                        now,
                        f"availability:{candidate.canonical_name}:{checked.status}",
                    )
                availability[candidate.canonical_name] = {
                    "available": checked.available,
                    "course_count": checked.course_count,
                    "section_count": checked.section_count,
                    "checked_at": checked.checked_at,
                    "source_url": checked.source_url,
                    "content_length": checked.content_length,
                }

            synced = replace(
                current,
                calendar_records=calendar_records,
                websoc_terms=[key.canonical_name for key in sorted(websoc_keys, key=_term_sort_key)],
                availability=availability,
                automatic_term=current.automatic_term or self.fallback_term.canonical_name,
                source="anteater",
                status="fresh",
                source_urls={
                    "calendar": calendar_result.url,
                    "websoc_terms": terms_result.url,
                    "websoc": anteater.WEBSOC_URL,
                },
                last_success_at=_iso(now),
                last_attempt_at=_iso(now),
                last_error=None,
            )
            self.store.save(synced)
            observability.increment("term.sync", result="completed")
            observability.log_event(
                logger,
                logging.INFO,
                "term_sync_completed",
                calendar_records=len(calendar_records),
                websoc_terms=len(websoc_keys),
                availability_probes=len(availability),
            )
            return SyncResult("synced", synced)

    def _sync_failed(
        self,
        state: TermStateSnapshot,
        now: datetime,
        error: str,
    ) -> SyncResult:
        age = cache_age(state, now)
        if age is not None and age <= MAX_CACHE_AGE:
            failed = replace(
                state,
                source="last_known_good",
                status="stale",
                last_attempt_at=_iso(now),
                last_error=error,
            )
            action = "using_last_known_good"
        else:
            failed = fallback_state(
                now=now,
                previous=state,
                error=error,
                fallback_term=self.fallback_term,
            )
            action = "fallback"
            self._log_fallback(failed)
        self.store.save(failed)
        observability.increment("term.sync", result="failed")
        observability.log_event(
            logger,
            logging.WARNING,
            "term_sync_failed",
            error=error,
            fallback=action == "fallback",
        )
        return SyncResult(action, failed, error)

    @staticmethod
    def _log_fallback(state: TermStateSnapshot) -> None:
        observability.increment("term.fallback")
        observability.log_event(
            logger,
            logging.WARNING,
            "term_fallback_activated",
            term=state.automatic_term,
            error=state.last_error,
        )


_synchronizer: Optional[TermStateSynchronizer] = None


def get_term_synchronizer() -> TermStateSynchronizer:
    global _synchronizer
    path = config.term_state_path()
    if _synchronizer is None or getattr(_synchronizer.store, "path", None) != path:
        _synchronizer = TermStateSynchronizer(JsonFileTermStateStore(path))
    return _synchronizer

