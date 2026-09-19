"""UCI calendar rules used by automatic term selection."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional

from app.terms.clock import LOS_ANGELES


class TermCalendarError(ValueError):
    pass


def calculate_week8_cutoff(instruction_start: date | datetime | str) -> datetime:
    """Start of instructional Week 8, in Pacific time (including DST).

    Fall's short opening week is Week 0; Week 1 starts on the first
    Monday on or after instruction begins, as in the Registrar calendar.
    """
    start = _coerce_date(instruction_start, field="instruction start")
    week1 = start + timedelta(days=(7 - start.weekday()) % 7)
    return datetime.combine(week1 + timedelta(weeks=7), time.min, tzinfo=LOS_ANGELES)


def _coerce_date(value: date | datetime | str, *, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        raw = value.strip()
        try:
            return date.fromisoformat(raw[:10])
        except ValueError as exc:
            raise TermCalendarError(f"invalid {field}: {value!r}") from exc
    raise TermCalendarError(f"invalid {field}: {value!r}")


def _coerce_deadline(value: date | datetime | str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=LOS_ANGELES)
        return value.astimezone(LOS_ANGELES)
    deadline_date = _coerce_date(value, field="official deadline")
    return datetime.combine(deadline_date, time(17, 0), tzinfo=LOS_ANGELES)


def calculate_week2_friday_cutoff(
    instruction_start: date | datetime | str,
    *,
    official_deadline: Optional[date | datetime | str] = None,
) -> datetime:
    """Return the authoritative or inferred Week 2 Friday 17:00 cutoff.

    The inferred rule finds the first Monday on or after instruction start,
    then advances eleven days. ZoneInfo owns DST behavior.
    """
    if official_deadline is not None:
        return _coerce_deadline(official_deadline)

    start = _coerce_date(instruction_start, field="instruction start")
    days_until_monday = (7 - start.weekday()) % 7
    week1_monday = start + timedelta(days=days_until_monday)
    week2_friday = week1_monday + timedelta(days=11)
    return datetime.combine(week2_friday, time(17, 0), tzinfo=LOS_ANGELES)
