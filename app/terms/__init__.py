"""Unified automatic-term context domain."""

from app.terms.calendar import TermCalendarError, calculate_week2_friday_cutoff
from app.terms.clock import Clock, FixedClock, LOS_ANGELES, SystemClock
from app.terms.models import (
    REGULAR_QUARTERS,
    SUPPORTED_QUARTERS,
    ResolvedTerm,
    TermKey,
    TermParseError,
    TermParseResult,
)
from app.terms.parser import extract_explicit_term_keys, parse_term_key, parse_term_text
from app.terms.query_scope import (
    QueryScope,
    next_recent_focus_terms,
    resolve_query_scope,
)
from app.terms.store import (
    InMemoryTermStateStore,
    JsonFileTermStateStore,
    TermStateSnapshot,
    TermStateStore,
)

__all__ = [
    "Clock",
    "FixedClock",
    "InMemoryTermStateStore",
    "JsonFileTermStateStore",
    "LOS_ANGELES",
    "REGULAR_QUARTERS",
    "QueryScope",
    "ResolvedTerm",
    "SUPPORTED_QUARTERS",
    "SystemClock",
    "TermCalendarError",
    "TermKey",
    "TermParseError",
    "TermParseResult",
    "TermStateSnapshot",
    "TermStateStore",
    "calculate_week2_friday_cutoff",
    "extract_explicit_term_keys",
    "next_recent_focus_terms",
    "parse_term_key",
    "parse_term_text",
    "resolve_query_scope",
]
