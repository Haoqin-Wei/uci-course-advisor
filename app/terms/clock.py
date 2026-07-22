"""Injectable clocks for deterministic term resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo


LOS_ANGELES = ZoneInfo("America/Los_Angeles")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(LOS_ANGELES)


@dataclass(frozen=True)
class FixedClock:
    instant: datetime

    def now(self) -> datetime:
        if self.instant.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        return self.instant.astimezone(LOS_ANGELES)

