"""Whole-term section sync: fetch → convert → validate → atomic replace.

One call copies one term's complete WebSoc listing into the SQLite catalog
store. Scheduling, publication polling and default-term switching live
elsewhere; this module only answers "bring term X up to date now".

Outcomes never overstate what is known:
  - transport/schema failure  → stored rows untouched, attempt recorded
  - verified empty listing    → "unpublished" (only if nothing is stored)
  - listing far below the same quarter of the previous year → "partial"
  - otherwise                 → "complete"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.catalog import store
from app.catalog.term import Term
from app.data.websoc_payload import cancellation_fields, course_comment_context
from app.scheduling.service import section_meetings
from app.terms.models import TermKey

logger = logging.getLogger(__name__)

SOURCE = "anteater_websoc"
PAYLOAD_VERSION = "2"
# A new listing below this share of last year's same quarter is not yet
# treated as the whole timetable (early, department-by-department posting).
COMPLETE_REFERENCE_RATIO = 0.5
_PLACEHOLDER_INSTRUCTORS = {"STAFF", "TBA", "TBD"}


@dataclass(frozen=True)
class SyncResult:
    term_id: str
    outcome: str  # "synced" | "unpublished" | "rejected_shrink" | "failed"
    status: Optional[str]  # stored snapshot status after the attempt
    section_count: int
    previous_section_count: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.outcome in {"synced", "unpublished"}


def _numeric(course_number: str) -> Optional[int]:
    digits = "".join(ch for ch in course_number if ch.isdigit())
    return int(digits) if digits else None


def sections_from_websoc(data: dict) -> list[dict]:
    """Flatten an Anteater WebSoc ``data`` object into store rows.

    Mirrors the CSV importer's field rules so both feeds stay comparable:
    flat time fields come from the first meeting while ``meetings`` keeps
    every meeting; parent comments keep their scope; a non-boolean
    cancellation flag stays unknown; placeholder instructors are dropped so
    "STAFF" is never treated as a person.
    """
    rows: list[dict] = []
    for school in data.get("schools") or []:
        if not isinstance(school, dict):
            continue
        for dept in school.get("departments") or []:
            if not isinstance(dept, dict):
                continue
            dept_code = str(dept.get("deptCode") or "").strip()
            for course in dept.get("courses") or []:
                if not isinstance(course, dict):
                    continue
                number = str(course.get("courseNumber") or "").strip()
                comments = course_comment_context(school, dept, course)
                for sec in course.get("sections") or []:
                    if not isinstance(sec, dict):
                        continue
                    code = str(sec.get("sectionCode") or "").strip()
                    if not code or not dept_code or not number:
                        continue
                    meetings = sec.get("meetings")
                    first = section_meetings({"meetings": meetings})[0]
                    enrolled = sec.get("numCurrentlyEnrolled")
                    enrolled = enrolled if isinstance(enrolled, dict) else {}
                    comment = sec.get("sectionComment")
                    rows.append({
                        "section_code": code,
                        "department": dept_code,
                        "course_number": number,
                        "course_numeric": _numeric(number),
                        "section_type": sec.get("sectionType"),
                        "section_num": sec.get("sectionNum"),
                        "units": sec.get("units"),
                        "instructors": [
                            name.strip() for name in (sec.get("instructors") or [])
                            if isinstance(name, str) and name.strip()
                            and name.strip().upper() not in _PLACEHOLDER_INSTRUCTORS
                        ],
                        "days": first.get("days"),
                        "start_time": first.get("start_time"),
                        "end_time": first.get("end_time"),
                        "location": first.get("location"),
                        "time_is_tba": first.get("time_is_tba"),
                        "meetings": meetings if isinstance(meetings, list) else [],
                        "final_exam": sec.get("finalExam") or {},
                        "restrictions": sec.get("restrictions"),
                        "comments": comments,
                        "section_comment": comment if isinstance(comment, str) else "",
                        "payload_version": PAYLOAD_VERSION,
                        "is_cancelled": cancellation_fields(sec.get("isCancelled"))["is_cancelled"],
                        "status": sec.get("status"),
                        "max_capacity": sec.get("maxCapacity"),
                        "total_enrolled": enrolled.get("totalEnrolled"),
                        "section_enrolled": enrolled.get("sectionEnrolled"),
                        "num_on_waitlist": sec.get("numOnWaitlist"),
                        "waitlist_cap": sec.get("numWaitlistCap"),
                        "num_requested": sec.get("numRequested"),
                        "source_updated_at": sec.get("updatedAt"),
                    })
    return rows


def _default_fetcher(term: TermKey):
    from app.data import anteater
    return anteater.fetch_full_websoc(term)


def _reference_count(term: Term, path: Optional[Path]) -> int:
    previous = store.get_term_snapshot(f"{term.year - 1}_{term.quarter}", path=path)
    if previous and previous["status"] == "complete":
        return int(previous["section_count"] or 0)
    return 0


def sync_term(
    term_id: str,
    *,
    fetcher: Optional[Callable] = None,
    force: bool = False,
    path: Optional[Path] = None,
) -> SyncResult:
    """Fetch one term's full listing and store it. Never raises on fetch errors."""
    term = Term.parse(term_id or "")
    if not term:
        raise ValueError(f"unrecognized term id {term_id!r}")
    tid = term.term_id
    before = store.get_term_snapshot(tid, path=path)
    held = int(before["section_count"]) if before else 0

    def failed(error: str) -> SyncResult:
        store.record_sync_failure(tid, error, path=path)
        after = store.get_term_snapshot(tid, path=path)
        logger.warning("catalog sync failed term=%s error=%s", tid, error)
        return SyncResult(tid, "failed", after["status"] if after else None, held, held, error)

    try:
        result = (fetcher or _default_fetcher)(TermKey(term.year, term.quarter))
    except Exception as exc:  # A fetcher bug must not corrupt or hide stored data.
        return failed(f"fetch_exception: {type(exc).__name__}: {exc}")
    if not getattr(result, "ok", False):
        return failed(f"{getattr(result, 'status', 'error')}: {getattr(result, 'error', None)}")
    data = getattr(result, "data", None)
    if not isinstance(data, dict) or not isinstance(data.get("schools"), list):
        return failed("schema_error: WebSoc data must contain a schools list")

    try:
        rows = sections_from_websoc(data)
    except Exception as exc:
        return failed(f"convert_error: {type(exc).__name__}: {exc}")

    if not rows:
        if store.mark_term_unpublished(tid, source=SOURCE, path=path):
            return SyncResult(tid, "unpublished", "unpublished", 0, held)
        # Sections are stored but the source now lists none: treat as an outage.
        return failed("empty_listing: source returned no sections for a stored term")

    reference = _reference_count(term, path)
    status = "partial" if reference and len(rows) < reference * COMPLETE_REFERENCE_RATIO else "complete"
    try:
        replaced = store.replace_term_sections(
            tid, rows, source=SOURCE, status=status, force=force, path=path,
        )
    except ValueError as exc:
        return failed(f"invalid_rows: {exc}")
    after = store.get_term_snapshot(tid, path=path)
    if not replaced.replaced:
        logger.warning("catalog sync rejected term=%s new=%s stored=%s", tid, len(rows), held)
        return SyncResult(tid, "rejected_shrink", after["status"], held, held, after["last_error"])
    from app.catalog.cache import get_catalog_cache
    get_catalog_cache().invalidate(term)  # This process serves the new rows at once.
    logger.info("catalog sync ok term=%s status=%s sections=%s previous=%s", tid, status, len(rows), held)
    return SyncResult(tid, "synced", status, replaced.section_count, replaced.previous_section_count)
