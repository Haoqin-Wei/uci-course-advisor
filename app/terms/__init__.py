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
from app.terms.parser import parse_term_key, parse_term_text

__all__ = [
    "Clock",
    "FixedClock",
    "LOS_ANGELES",
    "REGULAR_QUARTERS",
    "ResolvedTerm",
    "SUPPORTED_QUARTERS",
    "SystemClock",
    "TermCalendarError",
    "TermKey",
    "TermParseError",
    "TermParseResult",
    "calculate_week2_friday_cutoff",
    "parse_term_key",
    "parse_term_text",
]

