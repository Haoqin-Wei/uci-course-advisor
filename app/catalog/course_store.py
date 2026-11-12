"""Course-catalogue snapshot (titles, prerequisites, GE labels, …) in the catalog store.

One row per course, kept in the same string layout as ``data/uci/courses.csv``
so every reader can consume either source through one code path. The whole
catalogue is replaced in a single transaction; a failed or sharply smaller
fetch leaves the stored rows untouched and only records the attempt.

Extra columns the CSV never had (corequisites, grading option, repeatability,
concurrent listing, overlapping courses) are stored for later eligibility
checks. Nothing here interprets them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from app.catalog import store

CSV_COLUMNS = (
    "course_id", "department", "course_number", "course_numeric",
    "title", "min_units", "max_units", "description",
    "course_level", "school", "department_name",
    "same_as", "restriction",
    "prerequisite_text", "prerequisite_tree_json",
    "prerequisites_flat_json", "dependencies_flat_json",
    "terms_offered_json", "ge_list", "ge_text",
    "instructors_shortened_json",
)
EXTRA_COLUMNS = (
    "corequisites", "grading_option", "repeatability", "repeatability_times",
    "repeatability_type", "concurrent", "overlap",
)
COLUMNS = CSV_COLUMNS + EXTRA_COLUMNS
DEFAULT_MIN_RETAIN_RATIO = store.DEFAULT_MIN_RETAIN_RATIO

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS courses (\n"
    + "".join(f"    {name} TEXT NOT NULL DEFAULT '',\n" for name in COLUMNS)
    + "    PRIMARY KEY (department, course_number)\n);\n"
    """
CREATE TABLE IF NOT EXISTS course_snapshot (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    source          TEXT,
    course_count    INTEGER NOT NULL DEFAULT 0,
    last_synced_at  TEXT,
    last_attempt_at TEXT,
    last_error      TEXT
);
"""
)


@dataclass(frozen=True)
class CourseReplaceResult:
    replaced: bool
    reason: str  # "ok" | "rejected_shrink"
    course_count: int
    previous_course_count: int


def _open(path: Optional[Path]):
    """Read-write connection with the course tables present."""
    manager = store._conn(path)
    conn = manager.__enter__()
    try:
        conn.executescript(_SCHEMA)
    except BaseException:
        manager.__exit__(None, None, None)
        raise
    return manager, conn


def _normalize(row: Mapping) -> dict:
    out = {name: "" if row.get(name) is None else str(row.get(name)) for name in COLUMNS}
    out["department"] = " ".join(out["department"].split()).upper()
    out["course_number"] = out["course_number"].strip().upper()
    if not out["department"] or not out["course_number"]:
        raise ValueError("each course needs department and course_number")
    if not out["course_id"]:
        out["course_id"] = f"{out['department'].replace(' ', '_')}_{out['course_number']}"
    return out


def replace_courses(
    rows: Iterable[Mapping],
    *,
    source: str,
    synced_at: Optional[str] = None,
    min_retain_ratio: float = DEFAULT_MIN_RETAIN_RATIO,
    force: bool = False,
    path: Optional[Path] = None,
) -> CourseReplaceResult:
    """Atomically replace the stored catalogue. Duplicate courses keep the last row."""
    if not 0 <= min_retain_ratio <= 1:
        raise ValueError("min_retain_ratio must be between 0 and 1")
    by_key: dict[tuple[str, str], dict] = {}
    for row in rows:
        normalized = _normalize(row)
        by_key[(normalized["department"], normalized["course_number"])] = normalized
    if not by_key:
        raise ValueError("refusing to store an empty course catalogue")
    now = store._now_iso()
    manager, conn = _open(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            previous = conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
            if not force and previous and len(by_key) < previous * min_retain_ratio:
                error = (f"rejected_shrink: {len(by_key)} courses is below "
                         f"{min_retain_ratio:.0%} of the stored {previous}")
                conn.execute(
                    "UPDATE course_snapshot SET last_attempt_at = ?, last_error = ? WHERE id = 1",
                    (now, error))
                conn.execute("COMMIT")
                return CourseReplaceResult(False, "rejected_shrink", previous, previous)
            conn.execute("DELETE FROM courses")
            conn.executemany(
                f"INSERT INTO courses ({', '.join(COLUMNS)}) "
                f"VALUES ({', '.join(':' + name for name in COLUMNS)})",
                list(by_key.values()))
            conn.execute(
                """
                INSERT INTO course_snapshot (id, source, course_count, last_synced_at, last_attempt_at, last_error)
                VALUES (1, ?, ?, ?, ?, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source, course_count = excluded.course_count,
                    last_synced_at = excluded.last_synced_at,
                    last_attempt_at = excluded.last_attempt_at, last_error = NULL
                """,
                (source, len(by_key), synced_at or now, now))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        manager.__exit__(None, None, None)
    return CourseReplaceResult(True, "ok", len(by_key), previous)


def record_course_sync_failure(error: str, *, path: Optional[Path] = None) -> None:
    """Note a failed attempt without touching stored rows."""
    now = store._now_iso()
    manager, conn = _open(path)
    try:
        conn.execute(
            """
            INSERT INTO course_snapshot (id, last_attempt_at, last_error) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_attempt_at = excluded.last_attempt_at, last_error = excluded.last_error
            """,
            (now, str(error)[:500]))
    finally:
        manager.__exit__(None, None, None)


def get_course_snapshot(*, path: Optional[Path] = None) -> Optional[dict]:
    """Snapshot metadata, or None when no sync was ever attempted. Read-only."""
    conn = store._read_only(path)
    if conn is None:
        return None
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM course_snapshot WHERE id = 1").fetchone()
        return {key: row[key] for key in row.keys() if key != "id"} if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def course_token(*, path: Optional[Path] = None) -> Optional[tuple]:
    """Cheap change marker; None means "no stored catalogue, use the CSV"."""
    snapshot = get_course_snapshot(path=path)
    if not snapshot or not snapshot.get("course_count"):
        return None
    return (snapshot["course_count"], snapshot["last_synced_at"])


def get_course_rows(*, path: Optional[Path] = None) -> list[dict]:
    """Stored courses as CSV-shaped string dicts; [] when nothing is stored."""
    conn = store._read_only(path)
    if conn is None:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT {', '.join(COLUMNS)} FROM courses ORDER BY department, course_number").fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def stored_course_departments(*, path: Optional[Path] = None) -> list[str]:
    conn = store._read_only(path)
    if conn is None:
        return []
    try:
        return [row[0] for row in conn.execute("SELECT DISTINCT department FROM courses ORDER BY 1")]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
