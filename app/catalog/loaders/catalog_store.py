"""CatalogStoreLoader — synced SQLite section snapshots, CSV as fallback.

Sections for a term come from the catalog store when a snapshot is stored
there; otherwise the checked-in CSV rows are used unchanged. Course-level
metadata (titles, prerequisites, GE labels) comes from the synced course
catalogue when one is stored, otherwise from courses.csv. The two sources are
independent: either, both or neither may be synced.

A course that appears in a synced term but in neither catalogue keeps unknown
metadata, except its title, which the WebSoc listing carries.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Optional

from app.catalog import course_source, store
from app.catalog.loaders.uci_relational import UCIRelationalLoader
from app.catalog.term import Term
from app.catalog.types import CourseRecord, CourseRef, Provenance, SectionRecord

logger = logging.getLogger(__name__)


def _section_record(term_id: str, row: dict, provenance: Provenance) -> SectionRecord:
    """Same field rules as the CSV loader, applied to typed store values."""
    versioned = bool(row.get("payload_version"))
    meetings = row.get("meetings")
    if meetings == [] and not versioned:
        meetings = None  # Legacy imports used [] as a placeholder beside flat times.
    comment = row.get("section_comment")
    return SectionRecord(
        section_id=f"{term_id}_{row['section_code']}",
        course=CourseRef(department=row["department"], course_number=row["course_number"]),
        section_code=row["section_code"],
        term_id=term_id,
        instructors=tuple(sorted(set(row.get("instructors") or []))),
        ge_categories=tuple(sorted(set(row.get("ge_categories") or []))),
        section_type=row.get("section_type"),
        section_num=row.get("section_num"),
        days=row.get("days"),
        start_time=row.get("start_time"),
        end_time=row.get("end_time"),
        location=row.get("location"),
        max_capacity=row.get("max_capacity"),
        section_enrolled=row.get("section_enrolled") or row.get("total_enrolled"),
        num_on_waitlist=row.get("num_on_waitlist"),
        status=row.get("status"),
        is_cancelled=row.get("is_cancelled"),
        restrictions=row.get("restrictions"),
        final_exam=row.get("final_exam"),
        provenance=provenance,
        meetings=meetings,
        comments=row.get("comments"),
        section_comment=comment if versioned else ((comment or "").strip() or None),
    )


class CatalogStoreLoader(UCIRelationalLoader):
    def __init__(self, data_dir: str | Path = "data/uci", db_path: Optional[Path] = None):
        super().__init__(data_dir)
        self._db_path = db_path

    @property
    def name(self) -> str:
        return "catalog_store"

    def _section_token(self, term: Term) -> Optional[tuple]:
        """Change marker of the term's stored sections; None means "use the CSV rows"."""
        token = store.snapshot_token(term.term_id, path=self._db_path)
        if token is None:
            return None
        return (str(self._db_path or store.config.catalog_db_path()), *token)

    def snapshot_token(self, term: Term) -> Optional[tuple]:
        """Marker for everything a term view is built from: sections and course rows."""
        sections = self._section_token(term)
        courses = course_source.course_rows_token(self._db_path)
        return (sections, courses) if sections or courses else None

    def _get_course_rows(self) -> list[dict]:
        if course_source.course_rows_token(self._db_path) is None:
            return super()._get_course_rows()
        return course_source.course_rows(csv_path=self.data_dir / "courses.csv", db_path=self._db_path)

    def _provenance(self, target_term: Term) -> Provenance:
        provenance = super()._provenance(target_term)
        if self._section_token(target_term) is None:
            # CSV-backed terms keep the CSV loader's provenance label.
            return dataclasses.replace(provenance, loader="uci_relational")
        return dataclasses.replace(
            provenance, source_file=str(self._db_path or store.config.catalog_db_path()))

    def available_terms(self) -> list[Term]:
        terms = set(super().available_terms())
        for term_id in store.stored_term_ids(path=self._db_path):
            term = Term.parse(term_id)
            if term:
                terms.add(term)
        return sorted(terms, reverse=True)

    def _load_sections(self, target_term: Term, provenance: Provenance):
        if self._section_token(target_term) is None:
            return super()._load_sections(target_term, provenance)
        term_id = target_term.term_id
        stored = provenance
        sections: list[SectionRecord] = []
        numeric_by_ref: dict[CourseRef, int | None] = {}
        for row in store.get_sections(term_id, path=self._db_path):
            section = _section_record(term_id, row, stored)
            sections.append(section)
            numeric_by_ref[section.course] = row.get("course_numeric")
        logger.info("catalog_store: %d sections for %s", len(sections), term_id)
        return sections, numeric_by_ref

    def load(self, target_term: Term) -> tuple[list[CourseRecord], list[SectionRecord]]:
        courses, sections = super().load(target_term)
        titles: dict[CourseRef, str] = {}
        for section in sections:
            course = (section.comments or {}).get("course") if isinstance(section.comments, dict) else None
            title = course.get("title") if isinstance(course, dict) else None
            if isinstance(title, str) and title.strip():
                titles.setdefault(section.course, title.strip())
        if titles:
            courses = [
                dataclasses.replace(course, title=titles[course.ref])
                if course.title is None and course.ref in titles else course
                for course in courses
            ]
        return self._with_anteater_ge(courses), sections

    def _with_anteater_ge(self, courses: list[CourseRecord]) -> list[CourseRecord]:
        """GE labels of the synced Anteater catalogue are the course's GE status.

        A catalogue course without a label is a verified non-GE course. A course
        known only from a timetable row has no catalogue entry and stays unknown.
        """
        if course_source.course_rows_token(self._db_path) is None:
            return courses  # CSV rows: labels of unknown edition, left unverified.
        from app.catalog import course_store
        from app.catalog.ge import parse_ge_categories
        from app.data.ge_evidence import anteater_provenance, anteater_sync_time

        synced_at = anteater_sync_time(course_store.get_course_snapshot(path=self._db_path))
        if not synced_at:
            return courses
        rows_by_ref, _ = self._course_index()
        marked = []
        for course in courses:
            row = rows_by_ref.get(course.ref)
            if row is None:
                marked.append(course)
                continue
            parsed = parse_ge_categories(row.get("ge_list"))
            marked.append(dataclasses.replace(
                course,
                ge_categories=tuple(sorted(parsed["categories"])),
                ge_categories_verified=True,
                ge_provenance=anteater_provenance(synced_at, unrecognized=parsed["unrecognized"]),
            ))
        return marked
