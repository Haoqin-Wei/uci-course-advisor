"""Single authority for automatic, explicit, and conversation term resolution."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Optional

from app import observability
from app.terms.calendar import TermCalendarError, calculate_week2_friday_cutoff
from app.terms.clock import Clock, SystemClock
from app.terms.models import ResolvedTerm, TermKey, TermParseError, TermParseResult
from app.terms.parser import parse_term_key, parse_term_text
from app.terms.store import TermStateSnapshot, TermStateStore
from app.terms.sync import (
    FALLBACK_AUTOMATIC_TERM,
    TermStateSynchronizer,
    cache_age,
    get_term_synchronizer,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueryTermResolution:
    automatic: ResolvedTerm
    parsed: TermParseResult
    terms: tuple[ResolvedTerm, ...] = ()

    @property
    def error(self) -> Optional[TermParseError]:
        return self.parsed.error

    @property
    def kind(self) -> str:
        return self.parsed.kind

    @property
    def all_available(self) -> bool:
        return bool(self.terms) and all(term.data_available for term in self.terms)


def _calendar_key(record: dict) -> Optional[TermKey]:
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


def _instruction_start(record: Optional[dict]) -> Optional[date]:
    if not record:
        return None
    raw = record.get("instructionStart") or record.get("instruction_start")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _cutoff(record: Optional[dict]) -> Optional[datetime]:
    start = _instruction_start(record)
    if start is None:
        return None
    official = None
    if record:
        official = (
            record.get("officialAddDropDeadline")
            or record.get("addDropDeadline")
            or record.get("addDeadline")
        )
    try:
        return calculate_week2_friday_cutoff(start, official_deadline=official)
    except TermCalendarError:
        return None


class TermResolutionService:
    def __init__(
        self,
        store: TermStateStore,
        *,
        clock: Optional[Clock] = None,
        synchronizer: Optional[TermStateSynchronizer] = None,
        fallback_term: TermKey = FALLBACK_AUTOMATIC_TERM,
    ):
        self.store = store
        self.clock = clock or SystemClock()
        self.synchronizer = synchronizer or TermStateSynchronizer(
            store,
            clock=self.clock,
            fallback_term=fallback_term,
        )
        self.fallback_term = fallback_term

    def automatic_term(self) -> ResolvedTerm:
        state = self.synchronizer.state_for_use()
        current = self._key_or_fallback(state.automatic_term)
        if state.source == "code_fallback" or state.status == "fallback":
            return self._resolved(current, state, force_available=True)

        records, duplicates = self._calendar_records(state)
        previous = current
        transition_cutoff: Optional[datetime] = None
        transition_evidence: Optional[dict] = None

        # One call catches up through every consecutively eligible regular term.
        for _ in range(12):
            current_record = records.get(current)
            current_cutoff = _cutoff(current_record)
            if current in duplicates or current_cutoff is None or self.clock.now() < current_cutoff:
                break

            candidate = current.next_regular()
            candidate_record = records.get(candidate)
            availability = state.availability.get(candidate.canonical_name) or {}
            if (
                candidate in duplicates
                or _instruction_start(candidate_record) is None
                or candidate.canonical_name not in state.websoc_terms
                or availability.get("available") is not True
                or int(availability.get("course_count") or 0) < 1
                or int(availability.get("section_count") or 0) < 1
            ):
                break

            current = candidate
            transition_cutoff = current_cutoff
            transition_evidence = availability

        if current != previous:
            changed_at = self.clock.now().isoformat(timespec="seconds")
            state = replace(
                state,
                automatic_term=current.canonical_name,
                transition={
                    "previous": previous.canonical_name,
                    "current": current.canonical_name,
                    "cutoff": transition_cutoff.isoformat() if transition_cutoff else None,
                    "availability": transition_evidence,
                    "source": state.source,
                    "changed_at": changed_at,
                },
            )
            self.store.save(state)
            observability.increment("term.automatic_transition")
            observability.log_event(
                logger,
                logging.INFO,
                "automatic_term_changed",
                previous=previous.canonical_name,
                current=current.canonical_name,
                cutoff=transition_cutoff.isoformat() if transition_cutoff else None,
                availability=transition_evidence,
                source=state.source,
                changed_at=changed_at,
            )
        return self._resolved(current, state, force_available=True)

    def resolve_message(self, text: str) -> QueryTermResolution:
        automatic = self.automatic_term()
        parsed = parse_term_text(text, automatic_term=automatic.key)
        if parsed.error:
            return QueryTermResolution(automatic, parsed)
        state = self.store.load() or TermStateSnapshot()
        terms = tuple(
            automatic if key == automatic.key else self._resolved(key, state)
            for key in parsed.terms
        )
        return QueryTermResolution(automatic, parsed, terms)

    def resolve_explicit(self, value: str) -> QueryTermResolution:
        automatic = self.automatic_term()
        parsed = parse_term_key(value)
        if parsed.error:
            return QueryTermResolution(automatic, parsed)
        state = self.store.load() or TermStateSnapshot()
        return QueryTermResolution(
            automatic,
            parsed,
            tuple(
                automatic if key == automatic.key else self._resolved(key, state)
                for key in parsed.terms
            ),
        )

    def effective_for_conversation(self, meta: dict) -> ResolvedTerm:
        automatic = self.automatic_term()
        if meta.get("term_mode") != "manual":
            return automatic
        # ``term_scope`` is read-only compatibility for sessions not yet
        # processed by the startup migration. New writes use default_term.
        parsed = parse_term_key(
            str(meta.get("default_term") or meta.get("term_scope") or "")
        )
        if parsed.kind != "single":
            return automatic
        state = self.store.load() or TermStateSnapshot()
        return self._resolved(
            parsed.terms[0],
            state,
            force_available=True,
            source="conversation_manual",
        )

    def automatic_state(self) -> dict:
        """Return the read-only operational view used by APIs and health."""
        automatic = self.automatic_term()
        state = self.store.load() or TermStateSnapshot()
        age = cache_age(state, self.clock.now())
        return {
            "automatic_term": automatic.canonical_name,
            "source": automatic.source,
            "status": automatic.status,
            "last_success_at": state.last_success_at,
            "last_attempt_at": state.last_attempt_at,
            "last_error": state.last_error,
            "next_cutoff": (
                automatic.week2_friday_cutoff.isoformat()
                if automatic.week2_friday_cutoff
                else None
            ),
            "checked_at": automatic.checked_at.isoformat(),
            "cache_age_days": (
                round(age.total_seconds() / 86_400, 3) if age is not None else None
            ),
            "availability": state.availability.get(automatic.canonical_name),
            "transition": state.transition or None,
            "fallback": (
                automatic.source == "code_fallback"
                or automatic.status == "fallback"
            ),
        }

    def conversation_state(self, meta: dict) -> dict:
        default = self.effective_for_conversation(meta)
        return {
            "default_term": default.canonical_name,
            "term_mode": (
                "manual" if meta.get("term_mode") == "manual" else "auto"
            ),
            "term_source": (
                meta.get("term_source")
                if meta.get("term_mode") == "manual"
                else default.source
            ),
            "term_status": default.status,
            "term_checked_at": default.checked_at.isoformat(),
            "available_terms": self.available_terms(
                include=default.canonical_name,
            ),
        }

    def available_terms(self, *, include: Optional[str] = None) -> list[str]:
        """Canonical published selector choices, ordered chronologically."""
        state = self.store.load() or TermStateSnapshot()
        names = {
            name
            for name, record in state.availability.items()
            if isinstance(record, dict) and record.get("available") is True
        }
        names.update(state.websoc_terms)
        if include:
            names.add(include)
        automatic = self.automatic_term().canonical_name
        names.add(automatic)
        keys: list[TermKey] = []
        for name in names:
            parsed = parse_term_key(str(name))
            if parsed.kind == "single" and parsed.terms[0] not in keys:
                keys.append(parsed.terms[0])
        quarter_order = {
            "Winter": 0,
            "Spring": 1,
            "Summer1": 2,
            "Summer10wk": 3,
            "Summer2": 4,
            "Fall": 5,
        }
        keys.sort(key=lambda key: (key.year, quarter_order[key.quarter]))
        return [key.canonical_name for key in keys]

    def is_selectable_term(self, key: TermKey) -> bool:
        """Return whether the selector is allowed to persist ``key``.

        The WebSoc term list is itself the publication contract for explicit
        user selection.  ``availability`` is a stronger, department-level
        probe used by the automatic-term transition algorithm; most historical
        terms are intentionally not probed.  Keeping these two concepts
        separate prevents the UI from offering a published historical term
        that the mutation endpoint then rejects.
        """
        state = self.store.load() or TermStateSnapshot()
        name = key.canonical_name
        availability = state.availability.get(name)
        if isinstance(availability, dict) and availability.get("available") is True:
            return True
        if name in state.websoc_terms:
            return True
        return name == self.automatic_term().canonical_name

    def _resolved(
        self,
        key: TermKey,
        state: TermStateSnapshot,
        *,
        force_available: bool = False,
        source: Optional[str] = None,
    ) -> ResolvedTerm:
        records, duplicates = self._calendar_records(state)
        record = None if key in duplicates else records.get(key)
        availability_record = state.availability.get(key.canonical_name)
        availability = availability_record or {}
        available = availability.get("available") is True
        if key == self._key_or_fallback(state.automatic_term):
            available = available or force_available
        status = (
            state.status
            if available
            else "unavailable" if availability_record is not None else "unknown"
        )
        return ResolvedTerm.from_key(
            key,
            instruction_start=_instruction_start(record),
            week2_friday_cutoff=_cutoff(record),
            data_available=available or force_available,
            source=source or state.source,
            status=status,
            checked_at=self.clock.now(),
        )

    def _calendar_records(
        self, state: TermStateSnapshot
    ) -> tuple[dict[TermKey, dict], set[TermKey]]:
        records: dict[TermKey, dict] = {}
        duplicates: set[TermKey] = set()
        for record in state.calendar_records:
            if not isinstance(record, dict):
                continue
            key = _calendar_key(record)
            if key is None:
                continue
            if key in records:
                duplicates.add(key)
            else:
                records[key] = record
        return records, duplicates

    def _key_or_fallback(self, value: Optional[str]) -> TermKey:
        parsed = parse_term_key(str(value or ""))
        return parsed.terms[0] if parsed.kind == "single" else self.fallback_term


_service: Optional[TermResolutionService] = None


def get_term_resolution_service() -> TermResolutionService:
    global _service
    synchronizer = get_term_synchronizer()
    if _service is None or _service.store is not synchronizer.store:
        _service = TermResolutionService(
            synchronizer.store,
            synchronizer=synchronizer,
        )
    return _service
