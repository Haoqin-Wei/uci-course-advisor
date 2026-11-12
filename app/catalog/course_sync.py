"""Whole-catalogue course sync from Anteater: page through → convert → replace.

About 9,300 courses arrive 100 per request. Every page must succeed (each is
retried a couple of times) before anything is written, so a partial crawl can
never replace a complete catalogue.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.catalog import course_store

logger = logging.getLogger(__name__)

SOURCE = "anteater_courses"
PAGE_SIZE = 100
MAX_PAGES = 400          # 40,000 courses; far above the real catalogue
PAGE_ATTEMPTS = 3


@dataclass(frozen=True)
class CourseSyncResult:
    outcome: str  # "synced" | "rejected_shrink" | "failed"
    course_count: int
    previous_course_count: int = 0
    pages: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.outcome == "synced"


def _flat_refs(items) -> list[dict]:
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        department, number = item.get("department"), item.get("courseNumber")
        if not department or not number:
            continue
        out.append({
            "id": f"{str(department).replace(' ', '_')}_{number}",
            "department": department,
            "course_number": number,
            "title": item.get("title", "") or "",
        })
    return out


def _plain(value) -> str:
    return "" if value is None else str(value)


def course_row_from_api(course: dict) -> dict:
    """One Anteater course → the courses.csv string layout, plus the extra fields.

    Mirrors scripts/fetch_uci_data.py so stored rows and CSV rows read alike.
    Instructor e-mail addresses and titles in the payload are not kept; only
    the shortened display names the CSV always carried.
    """
    names: list[str] = []
    for instructor in course.get("instructors") or []:
        if isinstance(instructor, dict):
            names.extend(n for n in instructor.get("shortenedNames") or [] if isinstance(n, str))
    department, number = course["department"], course["courseNumber"]
    dump = lambda value: json.dumps(value, ensure_ascii=False)
    return {
        "course_id": f"{str(department).replace(' ', '_')}_{number}",
        "department": department,
        "course_number": number,
        "course_numeric": _plain(course.get("courseNumeric")),
        "title": _plain(course.get("title")),
        "min_units": _plain(course.get("minUnits")),
        "max_units": _plain(course.get("maxUnits")),
        "description": _plain(course.get("description")),
        "course_level": _plain(course.get("courseLevel")),
        "school": _plain(course.get("school")),
        "department_name": _plain(course.get("departmentName")),
        "same_as": _plain(course.get("sameAs")),
        "restriction": _plain(course.get("restriction")),
        "prerequisite_text": _plain(course.get("prerequisiteText")),
        "prerequisite_tree_json": dump(course.get("prerequisiteTree") or {}),
        "prerequisites_flat_json": dump(_flat_refs(course.get("prerequisites"))),
        "dependencies_flat_json": dump(_flat_refs(course.get("dependencies"))),
        "terms_offered_json": dump(course.get("terms") or []),
        "ge_list": "|".join(g for g in course.get("geList") or [] if isinstance(g, str)),
        "ge_text": _plain(course.get("geText")),
        "instructors_shortened_json": dump(sorted(set(names))),
        "corequisites": _plain(course.get("corequisites")),
        "grading_option": _plain(course.get("gradingOption")),
        "repeatability": _plain(course.get("repeatability")),
        "repeatability_times": _plain(course.get("repeatabilityTimes")),
        "repeatability_type": _plain(course.get("repeatabilityType")),
        "concurrent": _plain(course.get("concurrent")),
        "overlap": _plain(course.get("overlap")),
    }


def _default_fetch_page(cursor: Optional[str]):
    from app.data import anteater
    params: dict = {"take": PAGE_SIZE}
    if cursor:
        params["cursor"] = cursor
    return anteater._get_json_result(f"{anteater.ANTEATER_BASE_URL}/coursesCursor", params=params)


def sync_courses(
    *,
    fetch_page: Optional[Callable] = None,
    force: bool = False,
    retry_wait_s: float = 1.0,
    path: Optional[Path] = None,
) -> CourseSyncResult:
    """Fetch the complete catalogue and store it. Never raises on fetch errors."""
    fetch_page = fetch_page or _default_fetch_page
    before = course_store.get_course_snapshot(path=path)
    held = int(before["course_count"]) if before else 0

    def failed(error: str, pages: int) -> CourseSyncResult:
        course_store.record_course_sync_failure(error, path=path)
        logger.warning("course catalogue sync failed after %s pages: %s", pages, error)
        return CourseSyncResult("failed", held, held, pages, error)

    rows: list[dict] = []
    cursor: Optional[str] = None
    pages = 0
    while True:
        if pages >= MAX_PAGES:
            return failed("page_limit: the source kept returning a next cursor", pages)
        error = "no attempt"
        items = next_cursor = None
        for attempt in range(PAGE_ATTEMPTS):
            try:
                result = fetch_page(cursor)
            except Exception as exc:
                error = f"fetch_exception: {type(exc).__name__}: {exc}"
            else:
                data = getattr(result, "data", None)
                if not getattr(result, "ok", False):
                    error = f"{getattr(result, 'status', 'error')}: {getattr(result, 'error', None)}"
                elif not isinstance(data, dict) or not isinstance(data.get("items"), list):
                    error = "schema_error: expected an object with an items list"
                else:
                    items, next_cursor = data["items"], data.get("nextCursor")
                    break
            if attempt + 1 < PAGE_ATTEMPTS and retry_wait_s > 0:
                time.sleep(retry_wait_s)
        if items is None:
            return failed(f"page {pages + 1}: {error}", pages)
        pages += 1
        try:
            rows.extend(course_row_from_api(item) for item in items if isinstance(item, dict))
        except (KeyError, TypeError) as exc:
            return failed(f"convert_error on page {pages}: {type(exc).__name__}: {exc}", pages)
        if not next_cursor or not items:
            break
        cursor = next_cursor

    if not rows:
        return failed("empty_catalogue: the source returned no courses", pages)
    try:
        replaced = course_store.replace_courses(rows, source=SOURCE, force=force, path=path)
    except ValueError as exc:
        return failed(f"invalid_rows: {exc}", pages)
    if not replaced.replaced:
        snapshot = course_store.get_course_snapshot(path=path) or {}
        return CourseSyncResult("rejected_shrink", held, held, pages, snapshot.get("last_error"))
    from app.catalog import course_source
    from app.catalog.cache import get_catalog_cache
    course_source.clear_cache()
    get_catalog_cache().invalidate()  # Every term view joins course metadata.
    logger.info("course catalogue sync ok courses=%s previous=%s pages=%s",
                replaced.course_count, replaced.previous_course_count, pages)
    return CourseSyncResult("synced", replaced.course_count, replaced.previous_course_count, pages)
