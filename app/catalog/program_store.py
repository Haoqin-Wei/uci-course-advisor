"""Degree-requirement snapshot in the catalog store.

Four kinds of record, all from Anteater's ``/programs`` endpoints:
  - "major", "minor", "specialization": one program each
  - "ugrad_requirement": the campus-wide sets (GE, UC, CHC4, CHC2)

Requirement trees nest (Group → Course/Unit/Marker, several levels deep) and
carry optional keys, so each program's list entry and full detail are stored
as the JSON the source returned; nothing is flattened or interpreted here.
The whole snapshot is replaced in one transaction. A failed or sharply
smaller fetch leaves the stored rows untouched and only records the attempt.

These are published requirement lists, not a student's degree audit.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from app.catalog import store

KINDS = ("major", "minor", "specialization", "ugrad_requirement")
DEFAULT_MIN_RETAIN_RATIO = store.DEFAULT_MIN_RETAIN_RATIO

_SCHEMA = """
CREATE TABLE IF NOT EXISTS programs (
    kind         TEXT NOT NULL,
    program_id   TEXT NOT NULL,
    name         TEXT,
    major_id     TEXT,
    catalog_year TEXT,
    summary_json TEXT NOT NULL,
    detail_json  TEXT NOT NULL,
    PRIMARY KEY (kind, program_id)
);

CREATE TABLE IF NOT EXISTS program_snapshot (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    source          TEXT,
    program_count   INTEGER NOT NULL DEFAULT 0,
    last_synced_at  TEXT,
    last_attempt_at TEXT,
    last_error      TEXT
);
"""


@dataclass(frozen=True)
class ProgramReplaceResult:
    replaced: bool
    reason: str  # "ok" | "rejected_shrink"
    program_count: int
    previous_program_count: int


def _open(path: Optional[Path]):
    manager = store._conn(path)
    conn = manager.__enter__()
    try:
        conn.executescript(_SCHEMA)
    except BaseException:
        manager.__exit__(None, None, None)
        raise
    return manager, conn


def _normalize(row: Mapping) -> dict:
    kind = str(row.get("kind") or "")
    program_id = str(row.get("program_id") or "").strip()
    if kind not in KINDS or not program_id:
        raise ValueError(f"each program needs a known kind and an id; got {kind!r}, {program_id!r}")
    summary, detail = row.get("summary"), row.get("detail")
    if not isinstance(summary, Mapping) or not isinstance(detail, Mapping):
        raise ValueError(f"{kind} {program_id}: summary and detail must be objects")
    text = lambda value: None if value is None else str(value)
    return {
        "kind": kind, "program_id": program_id,
        "name": text(summary.get("name") or detail.get("name")),
        "major_id": text(summary.get("majorId")),
        "catalog_year": text(detail.get("catalogYear") or summary.get("catalogYear")),
        "summary_json": json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
        "detail_json": json.dumps(detail, ensure_ascii=False, separators=(",", ":")),
    }


def replace_programs(
    rows: Iterable[Mapping],
    *,
    source: str,
    synced_at: Optional[str] = None,
    min_retain_ratio: float = DEFAULT_MIN_RETAIN_RATIO,
    force: bool = False,
    path: Optional[Path] = None,
) -> ProgramReplaceResult:
    """Atomically replace every stored program. Rows: {kind, program_id, summary, detail}."""
    if not 0 <= min_retain_ratio <= 1:
        raise ValueError("min_retain_ratio must be between 0 and 1")
    by_key: dict[tuple[str, str], dict] = {}
    for row in rows:
        normalized = _normalize(row)
        by_key[(normalized["kind"], normalized["program_id"])] = normalized
    if not by_key:
        raise ValueError("refusing to store an empty program snapshot")
    now = store._now_iso()
    manager, conn = _open(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            previous = conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0]
            if not force and previous and len(by_key) < previous * min_retain_ratio:
                error = (f"rejected_shrink: {len(by_key)} programs is below "
                         f"{min_retain_ratio:.0%} of the stored {previous}")
                conn.execute(
                    "UPDATE program_snapshot SET last_attempt_at = ?, last_error = ? WHERE id = 1",
                    (now, error))
                conn.execute("COMMIT")
                return ProgramReplaceResult(False, "rejected_shrink", previous, previous)
            conn.execute("DELETE FROM programs")
            conn.executemany(
                "INSERT INTO programs (kind, program_id, name, major_id, catalog_year, summary_json, detail_json) "
                "VALUES (:kind, :program_id, :name, :major_id, :catalog_year, :summary_json, :detail_json)",
                list(by_key.values()))
            conn.execute(
                """
                INSERT INTO program_snapshot (id, source, program_count, last_synced_at, last_attempt_at, last_error)
                VALUES (1, ?, ?, ?, ?, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source, program_count = excluded.program_count,
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
    return ProgramReplaceResult(True, "ok", len(by_key), previous)


def record_program_sync_failure(error: str, *, path: Optional[Path] = None) -> None:
    now = store._now_iso()
    manager, conn = _open(path)
    try:
        conn.execute(
            """
            INSERT INTO program_snapshot (id, last_attempt_at, last_error) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_attempt_at = excluded.last_attempt_at, last_error = excluded.last_error
            """,
            (now, str(error)[:500]))
    finally:
        manager.__exit__(None, None, None)


def _query(sql: str, params: tuple = (), *, path: Optional[Path] = None) -> list[sqlite3.Row]:
    """Read-only query; [] when the database or the tables do not exist."""
    conn = store._read_only(path)
    if conn is None:
        return []
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def get_program_snapshot(*, path: Optional[Path] = None) -> Optional[dict]:
    rows = _query("SELECT * FROM program_snapshot WHERE id = 1", path=path)
    return {key: rows[0][key] for key in rows[0].keys() if key != "id"} if rows else None


def has_programs(*, path: Optional[Path] = None) -> bool:
    snapshot = get_program_snapshot(path=path)
    return bool(snapshot and snapshot.get("program_count"))


def counts_by_kind(*, path: Optional[Path] = None) -> dict[str, int]:
    return {row[0]: row[1] for row in _query("SELECT kind, COUNT(*) FROM programs GROUP BY kind", path=path)}


def list_programs(kind: str, *, path: Optional[Path] = None) -> list[dict]:
    """List entries (as the source's list endpoint returned them) for one kind."""
    rows = _query("SELECT summary_json FROM programs WHERE kind = ? ORDER BY name, program_id", (kind,), path=path)
    return [json.loads(row[0]) for row in rows]


def get_program(kind: str, program_id: str, *, path: Optional[Path] = None) -> Optional[dict]:
    """Full requirement detail for one program, or None when it is not stored."""
    rows = _query("SELECT detail_json FROM programs WHERE kind = ? AND program_id = ?",
                  (kind, str(program_id or "").strip()), path=path)
    return json.loads(rows[0][0]) if rows else None
