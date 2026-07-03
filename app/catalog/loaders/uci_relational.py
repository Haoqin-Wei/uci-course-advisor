"""
UCIRelationalLoader — reads the cleaned 3-table CSV dump.

Expects three CSVs at <data_dir>:
    sections.csv               (section_id, term_id, course_id, year, quarter,
                                sectionCode, department, courseNumber, courseNumeric)
    section_instructors.csv    (section_id, instructor_name_raw)
    section_ge.csv             (section_id, ge_code)

Build steps:
  1. Read all three CSVs once at first use (small enough for memory).
  2. On load(term), filter sections to that term, group instructors
     and ge_codes by section_id, build SectionRecord list.
  3. Group sections by (department, courseNumber), build CourseRecord
     list (one course can have multiple sections; CourseRecord aggregates
     section-level GE info into a union).

Note: we build CourseRef directly from (department, courseNumber) columns,
NEVER by parsing the course_id string — that's ambiguous for multi-word
departments like 'SOC SCI'.
"""

from __future__ import annotations
import csv
import logging
from collections import defaultdict
from pathlib import Path

from app.catalog.types import CourseRef, CourseRecord, SectionRecord, Provenance
from app.catalog.term import Term
from app.catalog.loaders.base import CatalogLoader

logger = logging.getLogger(__name__)


def _str_or_none(s):
    """CSV gives '' for missing — coerce to None so downstream consumers
    can treat 'unknown' uniformly across loaders."""
    s = (s or "").strip()
    return s or None


def _int_or_none(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _parse_json_field(s):
    """CSV stores nested objects (finalExam, meetings) as JSON strings.
    Decode silently — bad rows shouldn't crash the catalog build."""
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        import json as _json
        return _json.loads(s)
    except (ValueError, TypeError):
        return None


class UCIRelationalLoader(CatalogLoader):
    def __init__(self, data_dir: str | Path = "data/uci"):
        self.data_dir = Path(data_dir)
        self._sections_rows: list[dict] = []
        self._instructors_rows: list[dict] = []
        self._ge_rows: list[dict] = []
        self._loaded = False

    @property
    def name(self) -> str:
        return "uci_relational"

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._sections_rows = self._read_csv("sections.csv")
        self._instructors_rows = self._read_csv("section_instructors.csv")
        self._ge_rows = self._read_csv("section_ge.csv")
        self._loaded = True
        logger.info(
            "Loaded UCI relational data: %d sections, %d instructor rows, %d ge rows",
            len(self._sections_rows), len(self._instructors_rows), len(self._ge_rows),
        )

    def _read_csv(self, filename: str) -> list[dict]:
        path = self.data_dir / filename
        if not path.exists():
            logger.warning("UCI data file not found: %s", path)
            return []
        with path.open("r", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def available_terms(self) -> list[Term]:
        self._ensure_loaded()
        seen: set[Term] = set()
        for row in self._sections_rows:
            t = Term.parse(row.get("term_id", ""))
            if t:
                seen.add(t)
        return sorted(seen, reverse=True)

    def load(self, target_term: Term) -> tuple[list[CourseRecord], list[SectionRecord]]:
        self._ensure_loaded()
        source_term_id = target_term.term_id

        # ── 1. Filter sections to this term ──
        section_rows = [
            r for r in self._sections_rows
            if r.get("term_id") == source_term_id
        ]
        if not section_rows:
            logger.info("UCIRelationalLoader: no data for term %s", source_term_id)
            return [], []

        section_ids = {r["section_id"] for r in section_rows}

        # ── 2. Group instructors / GE by section_id ──
        instructors_by_section: dict[str, list[str]] = defaultdict(list)
        for r in self._instructors_rows:
            sid = r.get("section_id", "")
            if sid in section_ids:
                name = (r.get("instructor_name_raw") or "").strip()
                if name:
                    instructors_by_section[sid].append(name)

        ge_by_section: dict[str, list[str]] = defaultdict(list)
        for r in self._ge_rows:
            sid = r.get("section_id", "")
            if sid in section_ids:
                code = (r.get("ge_code") or "").strip()
                if code:
                    ge_by_section[sid].append(code)

        provenance = Provenance(
            source_term=source_term_id,
            target_term=source_term_id,
            loader=self.name,
            source_file=str(self.data_dir),
        )

        # ── 3. Build SectionRecords ──
        sections: list[SectionRecord] = []
        for r in section_rows:
            sid = r["section_id"]
            try:
                ref = CourseRef(
                    department=r["department"],
                    course_number=r["courseNumber"],
                )
            except Exception as e:
                logger.warning("Skipping malformed section row %r: %s", sid, e)
                continue
            sections.append(SectionRecord(
                section_id=sid,
                course=ref,
                section_code=(r.get("sectionCode") or "").strip(),
                term_id=r.get("term_id", ""),
                instructors=tuple(sorted(set(instructors_by_section.get(sid, [])))),
                ge_categories=tuple(sorted(set(ge_by_section.get(sid, [])))),
                # Schedule + capacity (all optional; empty strings → None)
                section_type=    _str_or_none(r.get("sectionType")),
                section_num=     _str_or_none(r.get("sectionNum")),
                days=            _str_or_none(r.get("days")),
                start_time=      _str_or_none(r.get("start_time")),
                end_time=        _str_or_none(r.get("end_time")),
                location=        _str_or_none(r.get("location")),
                max_capacity=    _int_or_none(r.get("max_capacity")),
                section_enrolled=_int_or_none(r.get("section_enrolled")) or _int_or_none(r.get("total_enrolled")),
                num_on_waitlist= _int_or_none(r.get("num_on_waitlist")),
                status=          _str_or_none(r.get("status")),
                is_cancelled=    (r.get("is_cancelled") or "").strip().lower() == "true",
                restrictions=    _str_or_none(r.get("restrictions")),
                final_exam=      _parse_json_field(r.get("final_exam_json")),
                provenance=provenance,
            ))

        # ── 4. Build CourseRecords by grouping sections ──
        sections_by_ref: dict[CourseRef, list[SectionRecord]] = defaultdict(list)
        numeric_by_ref: dict[CourseRef, int | None] = {}
        for sec_row, sec in zip(section_rows, sections):
            sections_by_ref[sec.course].append(sec)
            try:
                numeric_by_ref[sec.course] = int(sec_row.get("courseNumeric") or 0)
            except (TypeError, ValueError):
                numeric_by_ref[sec.course] = None

        courses: list[CourseRecord] = []
        for ref, secs in sections_by_ref.items():
            ge_union = sorted({g for s in secs for g in s.ge_categories})
            courses.append(CourseRecord(
                ref=ref,
                course_numeric=numeric_by_ref.get(ref),
                title=None,                          # not in this feed
                units=None,                          # not in this feed
                description=None,                    # not in this feed
                prerequisites=tuple(),               # not in this feed
                major_requirements=tuple(),          # not in this feed
                ge_categories=tuple(ge_union),
                provenance=provenance,
            ))

        return courses, sections
