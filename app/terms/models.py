"""Canonical term-domain models used across chat, tools, and scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Optional


Quarter = Literal[
    "Fall",
    "Winter",
    "Spring",
    "Summer1",
    "Summer10wk",
    "Summer2",
]
TermSource = Literal[
    "anteater",
    "cache",
    "last_known_good",
    "code_fallback",
    "explicit",
    "conversation_pinned",
]
TermStatus = Literal[
    "available",
    "unavailable",
    "partial",
    "stale",
    "fallback",
    "fresh",
    "error",
    "unknown",
]

SUPPORTED_QUARTERS: tuple[Quarter, ...] = (
    "Fall",
    "Winter",
    "Spring",
    "Summer1",
    "Summer10wk",
    "Summer2",
)
REGULAR_QUARTERS: tuple[Quarter, ...] = ("Fall", "Winter", "Spring")


@dataclass(frozen=True, order=True)
class TermKey:
    """A UCI term in the product-wide canonical ``YYYY Quarter`` form."""

    year: int
    quarter: Quarter

    def __post_init__(self) -> None:
        if not 2000 <= self.year <= 2200:
            raise ValueError(f"term year out of supported range: {self.year!r}")
        if self.quarter not in SUPPORTED_QUARTERS:
            raise ValueError(f"unsupported UCI quarter: {self.quarter!r}")

    @property
    def canonical_name(self) -> str:
        return f"{self.year} {self.quarter}"

    @property
    def catalog_name(self) -> str:
        """Legacy catalog label retained at data-layer boundaries."""
        return f"{self.quarter} {self.year}"

    @property
    def is_regular(self) -> bool:
        return self.quarter in REGULAR_QUARTERS

    def next_regular(self) -> "TermKey":
        if self.quarter == "Fall":
            return TermKey(self.year + 1, "Winter")
        if self.quarter == "Winter":
            return TermKey(self.year, "Spring")
        if self.quarter == "Spring":
            return TermKey(self.year, "Fall")
        raise ValueError("Summer terms do not participate in the automatic sequence")

    def previous_regular(self) -> "TermKey":
        if self.quarter == "Fall":
            return TermKey(self.year, "Spring")
        if self.quarter == "Spring":
            return TermKey(self.year, "Winter")
        if self.quarter == "Winter":
            return TermKey(self.year - 1, "Fall")
        raise ValueError("Summer terms do not participate in the automatic sequence")


@dataclass(frozen=True)
class ResolvedTerm:
    """A canonical term enriched with calendar and availability evidence."""

    year: int
    quarter: Quarter
    canonical_name: str
    instruction_start: Optional[date]
    week2_friday_cutoff: Optional[datetime]
    data_available: bool
    source: TermSource
    status: TermStatus
    checked_at: datetime

    @classmethod
    def from_key(
        cls,
        key: TermKey,
        *,
        instruction_start: Optional[date] = None,
        week2_friday_cutoff: Optional[datetime] = None,
        data_available: bool = False,
        source: TermSource = "explicit",
        status: TermStatus = "unknown",
        checked_at: datetime,
    ) -> "ResolvedTerm":
        return cls(
            year=key.year,
            quarter=key.quarter,
            canonical_name=key.canonical_name,
            instruction_start=instruction_start,
            week2_friday_cutoff=week2_friday_cutoff,
            data_available=data_available,
            source=source,
            status=status,
            checked_at=checked_at,
        )

    @property
    def key(self) -> TermKey:
        return TermKey(self.year, self.quarter)


@dataclass(frozen=True)
class TermParseError:
    code: Literal["invalid", "ambiguous", "missing_base"]
    message: str
    value: str


@dataclass(frozen=True)
class TermParseResult:
    """Structured result for zero, one, many, or invalid term references."""

    terms: tuple[TermKey, ...] = ()
    error: Optional[TermParseError] = None
    reset_to_auto: bool = False

    @property
    def kind(self) -> Literal["none", "single", "multi", "error"]:
        if self.error:
            return "error"
        if len(self.terms) == 1:
            return "single"
        if len(self.terms) > 1:
            return "multi"
        return "none"
