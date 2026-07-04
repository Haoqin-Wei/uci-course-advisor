"""
CatalogView — term-scoped, source-agnostic query interface.

Validators and chat.py only call this. They never see raw CSVs / loaders.

Multi-source merge strategy:
  - course_exists: OR across all loaders that provided data
  - get_course(ref): merge field-by-field, first loader to provide wins
  - get_sections(ref): union (each loader contributes its sections)
  - instructor_exists: union across loaders
"""

from __future__ import annotations
import logging
from typing import Optional

from app.catalog.types import (
    CourseRef, CourseRecord, SectionRecord, Provenance,
)
from app.catalog.term import Term
from app.catalog.normalization import (
    normalize_instructor, instructor_last_name,
)

logger = logging.getLogger(__name__)


class CatalogView:
    def __init__(
        self,
        target_term: Term,
        courses: list[CourseRecord],
        sections: list[SectionRecord],
        provenance: Provenance,
    ):
        self.target_term = target_term
        self.provenance = provenance

        # Course index (one entry per CourseRef)
        self._courses_by_ref: dict[CourseRef, CourseRecord] = {
            c.ref: c for c in courses
        }

        # Sections index
        self._sections_by_ref: dict[CourseRef, list[SectionRecord]] = {}
        for s in sections:
            self._sections_by_ref.setdefault(s.course, []).append(s)

        # Instructor indices
        self._all_instructors_upper: set[str] = set()
        self._last_name_index: dict[str, set[str]] = {}
        for s in sections:
            for inst in s.instructors:
                inst_u = normalize_instructor(inst)
                if not inst_u:
                    continue
                self._all_instructors_upper.add(inst_u)
                surname = instructor_last_name(inst_u)
                if surname:
                    self._last_name_index.setdefault(surname, set()).add(inst_u)

        logger.info(
            "CatalogView built: target=%s, %d courses, %d sections, %d unique instructors",
            target_term.term_id, len(self._courses_by_ref),
            len(sections), len(self._all_instructors_upper),
        )

    # ── Course queries ───────────────────────────────────

    def course_exists(self, ref: CourseRef) -> bool:
        return ref in self._courses_by_ref

    def get_course(self, ref: CourseRef) -> Optional[CourseRecord]:
        return self._courses_by_ref.get(ref)

    def all_course_refs(self) -> set[CourseRef]:
        return set(self._courses_by_ref.keys())

    # ── Section queries ──────────────────────────────────

    def get_sections(self, ref: CourseRef) -> list[SectionRecord]:
        return list(self._sections_by_ref.get(ref, []))

    def offered_this_term(self, ref: CourseRef) -> bool:
        """True iff the course has at least one section in target_term.

        With the current single loader this is equivalent to course_exists,
        is already term-scoped. Kept as a separate method so validators
        express intent clearly.
        """
        return bool(self._sections_by_ref.get(ref))

    # ── Instructor queries ───────────────────────────────

    def instructor_exists(self, name: str) -> bool:
        """Exact match against canonical 'LAST, F.' form (case-insensitive)."""
        return normalize_instructor(name) in self._all_instructors_upper

    def find_instructors_by_last_name(self, surname: str) -> list[str]:
        """Return all canonical instructor strings with this surname.

        Used by the instructor validator when LLM says 'Professor Thornton'
        and we need to check if any 'THORNTON, X.' exists.
        """
        return sorted(self._last_name_index.get(surname.upper(), set()))
