"""
CatalogLoader — abstract base for any data source that produces
Course/Section records for a given Term.

Why separate base + impls:
  - Multiple data feeds can coexist (registrar dump, API snapshots, etc.)
  - CatalogView merges output across loaders by field priority
  - Swapping a loader doesn't touch validators
"""

from __future__ import annotations
from abc import ABC, abstractmethod

from app.catalog.types import CourseRecord, SectionRecord
from app.catalog.term import Term


class CatalogLoader(ABC):
    """One data source for one or more terms."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier, e.g. 'uci_relational'."""

    @abstractmethod
    def available_terms(self) -> list[Term]:
        """Which terms this loader can serve."""

    @abstractmethod
    def load(self, target_term: Term) -> tuple[list[CourseRecord], list[SectionRecord]]:
        """Produce (courses, sections) for the given target term.

        If the loader's source data is from a different term (historical
        proxy), it should still return data and mark Provenance with the
        actual source_term.
        """
