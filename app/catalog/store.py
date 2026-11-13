"""SQLite store for term-scoped section snapshots.

One row in ``term_snapshots`` answers "what do we hold for this term and how
fresh is it"; ``sections`` holds one row per (term, section code).

Contract:
  - A term refresh replaces every section row of that term in one
    transaction. Readers observe the old snapshot or the new one, never a mix.
  - A refresh that shrinks a term far below its previous size is rejected and
    recorded as a failed attempt; the previous rows stay in place.
  - ``status`` describes the rows currently stored. ``last_error`` describes
    the most recent attempt. A failed attempt never relabels good data.
  - This module performs no network I/O and does not decide term switching.
  - Parent comments (school/department notices) repeat on every section of a
    department and run to several KB. They are stored once per term in
    ``comment_scopes`` and re-attached on read, so callers still receive the
    full ``comments`` context for each section.
  - The file is a rebuildable snapshot cache. An older schema is reset rather
    than migrated; re-run the import/sync scripts to refill it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional

from app import config
from app.catalog.term import Term

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
SNAPSHOT_STATUSES = ("unknown", "unpublished", "partial", "complete")
DEFAULT_MIN_RETAIN_RATIO = 0.7

_SCHEMA = """
CREATE TABLE IF NOT EXISTS term_snapshots (
    term_id               TEXT PRIMARY KEY,
    year                  INTEGER NOT NULL,
    quarter               TEXT NOT NULL,
    status                TEXT NOT NULL
        CHECK (status IN ('unknown', 'unpublished', 'partial', 'complete')),
    source                TEXT,
    course_count          INTEGER NOT NULL DEFAULT 0,
    section_count         INTEGER NOT NULL DEFAULT 0,
    department_count      INTEGER NOT NULL DEFAULT 0,
    published_detected_at TEXT,
    last_synced_at        TEXT,
    last_attempt_at       TEXT,
    last_error            TEXT
);

CREATE TABLE IF NOT EXISTS sections (
    term_id           TEXT NOT NULL,
    section_code      TEXT NOT NULL,
    department        TEXT NOT NULL,
    course_number     TEXT NOT NULL,
    course_numeric    INTEGER,
    section_type      TEXT,
    section_num       TEXT,
    units             TEXT,
    instructors_json  TEXT NOT NULL DEFAULT '[]',
    ge_json           TEXT NOT NULL DEFAULT '[]',
    days              TEXT,
    start_time        TEXT,
    end_time          TEXT,
    location          TEXT,
    time_is_tba       INTEGER,
    meetings_json     TEXT,
    final_exam_json   TEXT,
    restrictions      TEXT,
    comment_scope     TEXT,
    course_comment_json TEXT,
    section_comment   TEXT,
    payload_version   TEXT,
    is_cancelled      INTEGER,
    status            TEXT,
    max_capacity      INTEGER,
    total_enrolled    INTEGER,
    section_enrolled  INTEGER,
    num_on_waitlist   INTEGER,
    waitlist_cap      INTEGER,
    num_requested     INTEGER,
    new_only_reserved INTEGER,
    source_updated_at TEXT,
    fetched_at        TEXT NOT NULL,
    PRIMARY KEY (term_id, section_code)
);

CREATE INDEX IF NOT EXISTS idx_sections_term_course
    ON sections (term_id, department, course_number);

CREATE TABLE IF NOT EXISTS comment_scopes (
    term_id    TEXT NOT NULL,
    scope_hash TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    PRIMARY KEY (term_id, scope_hash)
);
"""

_TEXT_FIELDS = (
    "section_type", "section_num", "units", "days", "start_time", "end_time",
    "location", "restrictions", "status", "payload_version", "source_updated_at",
)
_INT_FIELDS = (
    "course_numeric", "max_capacity", "total_enrolled", "section_enrolled",
    "num_on_waitlist", "waitlist_cap", "num_requested", "new_only_reserved",
)
_BOOL_FIELDS = ("time_is_tba", "is_cancelled")
_JSON_FIELDS = ("meetings_json", "final_exam_json")
_COLUMNS = (
    "term_id", "section_code", "department", "course_number", "course_numeric",
    "section_type", "section_num", "units", "instructors_json", "ge_json",
    "days", "start_time", "end_time", "location", "time_is_tba",
    "meetings_json", "final_exam_json", "restrictions", "comment_scope",
    "course_comment_json", "section_comment", "payload_version", "is_cancelled", "status",
    "max_capacity", "total_enrolled", "section_enrolled", "num_on_waitlist",
    "waitlist_cap", "num_requested", "new_only_reserved", "source_updated_at", "fetched_at",
)


@dataclass(frozen=True)
class ReplaceResult:
    """Outcome of one term refresh attempt."""

    term_id: str
    replaced: bool
    reason: str  # "ok" | "rejected_shrink"
    section_count: int
    previous_section_count: int


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _term(term_id: str) -> Term:
    term = Term.parse(term_id or "")
    if not term:
        raise ValueError(f"unrecognized term id {term_id!r}")
    return term


@contextmanager
def _conn(path: Optional[Path] = None):
    db_path = Path(path) if path is not None else config.catalog_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass  # Some filesystems cannot host WAL; rollback journal is still atomic.
    try:
        _ensure_schema(conn)
        yield conn
    finally:
        conn.close()


# Columns added after schema 2 shipped. They are appended in place, so a
# database that already holds synced terms keeps its rows.
_ADDED_COLUMNS = {"sections": (("new_only_reserved", "INTEGER"),)}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns:
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        _add_missing_columns(conn)
        return
    has_tables = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sections'"
    ).fetchone()
    if has_tables:
        logger.warning(
            "catalog store schema %s is not %s; resetting the snapshot cache "
            "(re-run the import/sync scripts to refill it)", version, SCHEMA_VERSION,
        )
        conn.executescript(
            "DROP TABLE IF EXISTS sections; DROP TABLE IF EXISTS term_snapshots; "
            "DROP TABLE IF EXISTS comment_scopes;"
        )
        conn.execute("VACUUM")
    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _read_only(path: Optional[Path]):
    """Open without creating the file or schema; None when nothing is stored."""
    db_path = Path(path) if path is not None else config.catalog_db_path()
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=10.0)
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            conn.close()
            return None
        return conn
    except sqlite3.Error:
        return None


def _text(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _bool(value) -> Optional[int]:
    """Tri-state: unknown stays NULL instead of becoming false."""
    if isinstance(value, bool):
        return int(value)
    normalized = str(value if value is not None else "").strip().lower()
    if normalized in {"true", "1"}:
        return 1
    if normalized in {"false", "0"}:
        return 0
    return None


def _json_text(value) -> Optional[str]:
    """Accept decoded objects or JSON text; keep valid source text verbatim."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        json.loads(text)  # Reject malformed payloads before they are stored.
        return text
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _string_list(value) -> str:
    if value is None:
        items: Iterable = ()
    elif isinstance(value, str):
        items = json.loads(value) if value.strip() else ()
    else:
        items = value
    seen: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.append(text)
    return json.dumps(seen, ensure_ascii=False)


def _normalize(term_id: str, row: Mapping, fetched_at: str) -> dict:
    section_code = _text(row.get("section_code"))
    department = _text(row.get("department"))
    course_number = _text(row.get("course_number"))
    if not section_code or not department or not course_number:
        raise ValueError(
            "each section needs section_code, department and course_number; "
            f"got {section_code!r}, {department!r}, {course_number!r}"
        )
    out = {
        "term_id": term_id,
        "section_code": section_code,
        "department": department.upper(),
        "course_number": course_number.upper(),
        "instructors_json": _string_list(row.get("instructors")),
        "ge_json": _string_list(row.get("ge_categories")),
        # An empty comment can be source evidence; only None means absent.
        "section_comment": row.get("section_comment"),
        "fetched_at": fetched_at,
    }
    for name in _TEXT_FIELDS:
        out[name] = _text(row.get(name))
    for name in _INT_FIELDS:
        out[name] = _int(row.get(name))
    for name in _BOOL_FIELDS:
        out[name] = _bool(row.get(name))
    out["_scope_json"], out["comment_scope"], out["course_comment_json"] = _split_comments(
        row["comments"] if "comments" in row else row.get("comments_json"))
    for name in _JSON_FIELDS:
        # Callers may pass a decoded object ("meetings") or JSON text ("meetings_json").
        decoded_key = name[: -len("_json")]
        out[name] = _json_text(row[decoded_key] if decoded_key in row else row.get(name))
    return out


def _split_comments(value) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """comments → (shared parent scope JSON, its hash, per-course JSON).

    Everything except the ``course`` entry is identical for all sections of
    a department, so it is stored once and referenced by hash.
    """
    if value is None:
        return None, None, None
    if isinstance(value, str):
        if not value.strip():
            return None, None, None
        value = json.loads(value)
        if value is None:
            return None, None, None
    if not isinstance(value, Mapping):
        raise ValueError("comments must be an object")
    scope = {key: item for key, item in value.items() if key != "course"}
    scope_json = json.dumps(scope, ensure_ascii=False, separators=(",", ":"))
    scope_hash = hashlib.sha256(scope_json.encode("utf-8")).hexdigest()[:32]
    course_json = (
        json.dumps(value["course"], ensure_ascii=False, separators=(",", ":"))
        if "course" in value else None
    )
    return scope_json, scope_hash, course_json


def _snapshot_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def _section_dict(row: sqlite3.Row, scopes: Mapping[str, dict]) -> dict:
    out = {key: row[key] for key in row.keys()}
    scope_hash = out.pop("comment_scope")
    course_json = out.pop("course_comment_json")
    if scope_hash is None:
        out["comments"] = None
    else:
        # The parent scope object is shared between sections; treat as read-only.
        comments = dict(scopes.get(scope_hash) or {})
        if course_json is not None:
            comments["course"] = json.loads(course_json)
        out["comments"] = comments
    out["instructors"] = json.loads(out.pop("instructors_json") or "[]")
    out["ge_categories"] = json.loads(out.pop("ge_json") or "[]")
    for name in _JSON_FIELDS:
        raw = out.pop(name)
        out[name[: -len("_json")]] = json.loads(raw) if raw else None
    for name in _BOOL_FIELDS:
        out[name] = None if out[name] is None else bool(out[name])
    return out


def replace_term_sections(
    term_id: str,
    rows: Iterable[Mapping],
    *,
    source: str,
    status: str = "complete",
    fetched_at: Optional[str] = None,
    synced_at: Optional[str] = None,
    min_retain_ratio: float = DEFAULT_MIN_RETAIN_RATIO,
    force: bool = False,
    path: Optional[Path] = None,
) -> ReplaceResult:
    """Atomically replace one term's sections.

    ``status`` must be "complete" or "partial"; use ``mark_term_unpublished``
    for a term without sections. Duplicate section codes keep the last row.
    Unless ``force`` is set, a batch smaller than ``min_retain_ratio`` of the
    stored section count is rejected: the old rows stay and the attempt is
    recorded in ``last_error``.
    """
    term = _term(term_id)
    if status not in ("complete", "partial"):
        raise ValueError("status must be 'complete' or 'partial'")
    if not 0 <= min_retain_ratio <= 1:
        raise ValueError("min_retain_ratio must be between 0 and 1")
    now = _now_iso()
    fetched_at = fetched_at or now
    by_code: dict[str, dict] = {}
    for row in rows:
        normalized = _normalize(term.term_id, row, fetched_at)
        by_code[normalized["section_code"]] = normalized
    if not by_code:
        raise ValueError("refusing to store an empty section snapshot")
    new_rows = list(by_code.values())
    scope_rows = {
        row["comment_scope"]: row.pop("_scope_json") for row in new_rows
        if row["comment_scope"] is not None
    }
    for row in new_rows:
        row.pop("_scope_json", None)
    courses = {(r["department"], r["course_number"]) for r in new_rows}
    departments = {r["department"] for r in new_rows}

    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            previous = conn.execute(
                "SELECT COUNT(*) FROM sections WHERE term_id = ?", (term.term_id,)
            ).fetchone()[0]
            if not force and previous and len(new_rows) < previous * min_retain_ratio:
                error = (
                    f"rejected_shrink: {len(new_rows)} sections is below "
                    f"{min_retain_ratio:.0%} of the stored {previous}"
                )
                conn.execute(
                    "UPDATE term_snapshots SET last_attempt_at = ?, last_error = ? WHERE term_id = ?",
                    (now, error, term.term_id),
                )
                conn.execute("COMMIT")
                return ReplaceResult(term.term_id, False, "rejected_shrink", previous, previous)

            conn.execute("DELETE FROM sections WHERE term_id = ?", (term.term_id,))
            conn.execute("DELETE FROM comment_scopes WHERE term_id = ?", (term.term_id,))
            conn.executemany(
                "INSERT INTO comment_scopes (term_id, scope_hash, scope_json) VALUES (?, ?, ?)",
                [(term.term_id, key, text) for key, text in scope_rows.items()],
            )
            conn.executemany(
                f"INSERT INTO sections ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join(':' + c for c in _COLUMNS)})",
                new_rows,
            )
            conn.execute(
                """
                INSERT INTO term_snapshots (
                    term_id, year, quarter, status, source, course_count,
                    section_count, department_count, published_detected_at,
                    last_synced_at, last_attempt_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(term_id) DO UPDATE SET
                    status = excluded.status,
                    source = excluded.source,
                    course_count = excluded.course_count,
                    section_count = excluded.section_count,
                    department_count = excluded.department_count,
                    published_detected_at = COALESCE(
                        term_snapshots.published_detected_at,
                        excluded.published_detected_at),
                    last_synced_at = excluded.last_synced_at,
                    last_attempt_at = excluded.last_attempt_at,
                    last_error = NULL
                """,
                (
                    term.term_id, term.year, term.quarter, status, source,
                    len(courses), len(new_rows), len(departments),
                    synced_at or now, synced_at or now, now,
                ),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return ReplaceResult(term.term_id, True, "ok", len(new_rows), previous)


def mark_term_unpublished(term_id: str, *, source: str, path: Optional[Path] = None) -> bool:
    """Record a verified "not published yet" probe.

    Returns False and changes nothing when sections are already stored: a
    later empty probe must not hide a published snapshot.
    """
    term = _term(term_id)
    now = _now_iso()
    with _conn(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            held = conn.execute(
                "SELECT COUNT(*) FROM sections WHERE term_id = ?", (term.term_id,)
            ).fetchone()[0]
            if held:
                conn.execute("COMMIT")
                return False
            conn.execute(
                """
                INSERT INTO term_snapshots (term_id, year, quarter, status, source, last_attempt_at)
                VALUES (?, ?, ?, 'unpublished', ?, ?)
                ON CONFLICT(term_id) DO UPDATE SET
                    status = 'unpublished', source = excluded.source,
                    last_attempt_at = excluded.last_attempt_at, last_error = NULL
                """,
                (term.term_id, term.year, term.quarter, source, now),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return True


def record_sync_failure(term_id: str, error: str, *, path: Optional[Path] = None) -> None:
    """Note a failed attempt without touching stored rows or their status."""
    term = _term(term_id)
    now = _now_iso()
    with _conn(path) as conn:
        updated = conn.execute(
            "UPDATE term_snapshots SET last_attempt_at = ?, last_error = ? WHERE term_id = ?",
            (now, str(error)[:500], term.term_id),
        ).rowcount
        if not updated:
            # Nothing is known about publication; do not claim "unpublished".
            conn.execute(
                """
                INSERT INTO term_snapshots (term_id, year, quarter, status, last_attempt_at, last_error)
                VALUES (?, ?, ?, 'unknown', ?, ?)
                """,
                (term.term_id, term.year, term.quarter, now, str(error)[:500]),
            )


def get_term_snapshot(term_id: str, *, path: Optional[Path] = None) -> Optional[dict]:
    term = _term(term_id)
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT * FROM term_snapshots WHERE term_id = ?", (term.term_id,)
        ).fetchone()
    return _snapshot_dict(row) if row else None


def list_term_snapshots(*, path: Optional[Path] = None) -> list[dict]:
    with _conn(path) as conn:
        rows = conn.execute("SELECT * FROM term_snapshots").fetchall()
    order = {"Winter": 0, "Spring": 1, "Summer": 2, "Fall": 3}
    return sorted(
        (_snapshot_dict(row) for row in rows),
        key=lambda item: (item["year"], order.get(item["quarter"], 9)),
        reverse=True,
    )


def get_sections(
    term_id: str,
    *,
    department: Optional[str] = None,
    course_number: Optional[str] = None,
    path: Optional[Path] = None,
) -> list[dict]:
    """Stored sections for a term, optionally narrowed to a department/course.

    An empty list means "no stored row", never "not offered".
    """
    term = _term(term_id)
    sql = "SELECT * FROM sections WHERE term_id = ?"
    params: list = [term.term_id]
    if department:
        sql += " AND department = ?"
        params.append(department.strip().upper())
    if course_number:
        sql += " AND course_number = ?"
        params.append(course_number.strip().upper())
    sql += " ORDER BY department, course_number, section_code"
    with _conn(path) as conn:
        rows = conn.execute(sql, params).fetchall()
        scopes = {
            key: json.loads(text) for key, text in conn.execute(
                "SELECT scope_hash, scope_json FROM comment_scopes WHERE term_id = ?", (term.term_id,))
        } if rows else {}
    return [_section_dict(row, scopes) for row in rows]


def snapshot_token(term_id: str, *, path: Optional[Path] = None) -> Optional[tuple]:
    """Cheap change marker for one term's stored sections; None if there are none.

    Read-only: never creates the database. Callers compare tokens to decide
    whether an in-memory view of the term is still current.
    """
    term = _term(term_id)
    conn = _read_only(path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT status, section_count, last_synced_at FROM term_snapshots "
            "WHERE term_id = ? AND section_count > 0", (term.term_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return tuple(row) if row else None


def read_term_snapshots(*, path: Optional[Path] = None) -> list[dict]:
    """Every term snapshot row, without creating the database. [] when none."""
    conn = _read_only(path)
    if conn is None:
        return []
    try:
        conn.row_factory = sqlite3.Row
        return [_snapshot_dict(row) for row in conn.execute("SELECT * FROM term_snapshots")]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def get_department_scope(term_id: str, department: str, *, path: Optional[Path] = None) -> Optional[dict]:
    """School/department notices stored for one department in one term. Read-only.

    Returns the parent comment scope ({"school": {...}, "department": {...}})
    shared by that department's sections, or None when nothing is stored.
    """
    term = _term(term_id)
    conn = _read_only(path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            """
            SELECT scopes.scope_json
            FROM sections JOIN comment_scopes AS scopes
              ON scopes.term_id = sections.term_id AND scopes.scope_hash = sections.comment_scope
            WHERE sections.term_id = ? AND sections.department = ?
            ORDER BY scopes.scope_hash LIMIT 1
            """,
            (term.term_id, " ".join(str(department or "").split()).upper()),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def stored_departments(*, path: Optional[Path] = None) -> list[str]:
    """Registrar department codes present in any stored term. Read-only."""
    conn = _read_only(path)
    if conn is None:
        return []
    try:
        return [row[0] for row in conn.execute("SELECT DISTINCT department FROM sections ORDER BY 1")]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def stored_term_ids(*, path: Optional[Path] = None) -> list[str]:
    """Terms that currently hold sections. Read-only; [] when nothing is stored."""
    conn = _read_only(path)
    if conn is None:
        return []
    try:
        return [row[0] for row in conn.execute(
            "SELECT term_id FROM term_snapshots WHERE section_count > 0 ORDER BY term_id")]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
