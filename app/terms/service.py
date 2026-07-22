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
from app.terms.sync import FALLBACK_AUTOMATIC_TERM, TermStateSynchronizer


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
        terms = tuple(self._resolved(key, self.store.load() or TermStateSnapshot()) for key in parsed.terms)
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
            tuple(self._resolved(key, state) for key in parsed.terms),
        )

    def effective_for_conversation(self, meta: dict) -> ResolvedTerm:
        automatic = self.automatic_term()
        if meta.get("term_mode") != "pinned":
            return automatic
        parsed = parse_term_key(str(meta.get("term_scope") or ""))
        if parsed.kind != "single":
            return automatic
        state = self.store.load() or TermStateSnapshot()
        return self._resolved(
            parsed.terms[0],
            state,
            force_available=True,
            source="conversation_pinned",
        )

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
        availability = state.availability.get(key.canonical_name) or {}
        available = availability.get("available") is True
        if key == self._key_or_fallback(state.automatic_term):
            available = available or force_available
        status = state.status if available else "unavailable"
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

