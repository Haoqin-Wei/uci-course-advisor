"""
UCIRelationalLoader — reads the cleaned relational CSV dump.

Expects four CSVs at <data_dir>:
    courses.csv                (course-level metadata and prerequisite trees)
    sections.csv               (section_id, term_id, course_id, year, quarter,
                                sectionCode, department, courseNumber, courseNumeric)
    section_instructors.csv    (section_id, instructor_name_raw)
    section_ge.csv             (section_id, ge_code)

Build steps:
  1. Read all CSVs once at first use (small enough for memory).
  2. On load(term), filter course metadata and sections to that term, group instructors
     and ge_codes by section_id, build SectionRecord list.
  3. Build CourseRecord list from courses.csv, enriched by section-level
     GE info when sections are present.

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


def _float_or_none(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
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


def _parse_json_list(s) -> list:
    value = _parse_json_field(s)
    return value if isinstance(value, list) else []


def _course_ref_from_row(row: dict, *, department_key: str, number_key: str) -> CourseRef | None:
    department = (row.get(department_key) or "").strip()
    course_number = (row.get(number_key) or "").strip()
    if not department or not course_number:
        return None
    return CourseRef(department=department, course_number=course_number)


def _course_refs_from_json(s) -> tuple[CourseRef, ...]:
    refs: list[CourseRef] = []
    seen: set[CourseRef] = set()
    for item in _parse_json_list(s):
        if not isinstance(item, dict):
            continue
        ref = _course_ref_from_row(
            item,
            department_key="department",
            number_key="course_number",
        )
        if ref and ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return tuple(refs)


def _terms_from_json(s) -> tuple[str, ...]:
    terms: list[str] = []
    for item in _parse_json_list(s):
        if isinstance(item, str) and item.strip():
            terms.append(item.strip())
    return tuple(terms)


def _row_mentions_term(row: dict, target_term: Term) -> bool:
    for term_text in _terms_from_json(row.get("terms_offered_json")):
        parsed = Term.parse(term_text)
        if parsed == target_term:
            return True
    return False


def _string_list_from_json(s) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for item in _parse_json_list(s):
        if not isinstance(item, str):
            continue
        value = item.strip()
        if value and value not in seen:
            seen.add(value)
            values.append(value)
    return tuple(values)


def _ge_values_from_course_row(row: dict) -> list[str]:
    raw = (row.get("ge_list") or "").strip()
    if not raw:
        return []
    parsed = _parse_json_field(raw)
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [raw]


class UCIRelationalLoader(CatalogLoader):
    def __init__(self, data_dir: str | Path = "data/uci"):
        self.data_dir = Path(data_dir)
        self._course_rows: list[dict] = []
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
        self._course_rows = self._read_csv("courses.csv")
        self._sections_rows = self._read_csv("sections.csv")
        self._instructors_rows = self._read_csv("section_instructors.csv")
        self._ge_rows = self._read_csv("section_ge.csv")
        self._loaded = True
        logger.info(
            "Loaded UCI relational data: %d courses, %d sections, %d instructor rows, %d ge rows",
            len(self._course_rows), len(self._sections_rows),
            len(self._instructors_rows), len(self._ge_rows),
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
        for row in self._course_rows:
            for term_text in _terms_from_json(row.get("terms_offered_json")):
                t = Term.parse(term_text)
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
        section_pairs: list[tuple[dict, SectionRecord]] = []
        for r in section_rows:
            sid = r["section_id"]
            try:
                ref = _course_ref_from_row(
                    r,
                    department_key="department",
                    number_key="courseNumber",
                )
                if ref is None:
                    raise ValueError("missing department or courseNumber")
            except Exception as e:
                logger.warning("Skipping malformed section row %r: %s", sid, e)
                continue
            section = SectionRecord(
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
            )
            sections.append(section)
            section_pairs.append((r, section))

        # ── 4. Index sections by course without relying on zip alignment.
        sections_by_ref: dict[CourseRef, list[SectionRecord]] = defaultdict(list)
        numeric_by_ref: dict[CourseRef, int | None] = {}
        for sec_row, sec in section_pairs:
            sections_by_ref[sec.course].append(sec)
            try:
                numeric_by_ref[sec.course] = int(sec_row.get("courseNumeric") or 0)
            except (TypeError, ValueError):
                numeric_by_ref[sec.course] = None

        # ── 5. Build CourseRecords from courses.csv plus section-only fallbacks.
        rows_by_ref: dict[CourseRef, dict] = {}
        rows_in_term: dict[CourseRef, dict] = {}
        for row in self._course_rows:
            ref = _course_ref_from_row(
                row,
                department_key="department",
                number_key="course_number",
            )
            if ref is None:
                logger.warning("Skipping malformed course row %r", row.get("course_id"))
                continue
            rows_by_ref[ref] = row
            if _row_mentions_term(row, target_term):
                rows_in_term[ref] = row

        refs_for_term: set[CourseRef] = set(rows_in_term) | set(sections_by_ref)
        courses: list[CourseRecord] = []
        for ref in sorted(refs_for_term, key=lambda r: (r.department, r.course_number)):
            row = rows_in_term.get(ref) or rows_by_ref.get(ref) or {}
            secs = sections_by_ref.get(ref, [])
            ge_union = sorted({g for s in secs for g in s.ge_categories})
            ge_union = sorted(set(ge_union) | set(_ge_values_from_course_row(row)))
            min_units = _float_or_none(row.get("min_units"))
            max_units = _float_or_none(row.get("max_units"))
            units = min_units if min_units is not None and min_units == max_units else None
            numeric = _int_or_none(row.get("course_numeric"))
            if numeric is None:
                numeric = numeric_by_ref.get(ref)
            courses.append(CourseRecord(
                ref=ref,
                course_numeric=numeric,
                title=_str_or_none(row.get("title")),
                units=units,
                min_units=min_units,
                max_units=max_units,
                description=_str_or_none(row.get("description")),
                prerequisites=_course_refs_from_json(row.get("prerequisites_flat_json")),
                prerequisite_text=_str_or_none(row.get("prerequisite_text")),
                prerequisite_tree=_parse_json_field(row.get("prerequisite_tree_json")),
                major_requirements=tuple(),          # not in this feed
                ge_categories=tuple(ge_union),
                course_level=_str_or_none(row.get("course_level")),
                school=_str_or_none(row.get("school")),
                department_name=_str_or_none(row.get("department_name")),
                same_as=_str_or_none(row.get("same_as")),
                restriction=_str_or_none(row.get("restriction")),
                dependencies=_course_refs_from_json(row.get("dependencies_flat_json")),
                terms_offered=_terms_from_json(row.get("terms_offered_json")),
                all_known_instructors=_string_list_from_json(row.get("instructors_shortened_json")),
                provenance=provenance,
            ))

        if not courses and not sections:
            logger.info("UCIRelationalLoader: no data for term %s", source_term_id)
        return courses, sections
