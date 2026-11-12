"""The one place course-catalogue rows are read from.

``course_rows()`` returns the synced catalogue from the SQLite store when one
is stored, otherwise the checked-in ``data/uci/courses.csv``. Both come back as
lists of CSV-shaped string dicts, so callers never branch on the source.

Rows are cached per process. The stored snapshot's change marker is re-read
at most every ``CATALOG_VERSION_CHECK_SECONDS`` so a sync (possibly by another
process) is picked up without a restart.
"""

from __future__ import annotations

import csv
import logging
import os
import time
from pathlib import Path
from typing import Optional

from app import config
from app.catalog import course_store

logger = logging.getLogger(__name__)

CSV_PATH = Path(__file__).resolve().parents[2] / "data" / "uci" / "courses.csv"

_cache: dict[tuple, tuple[float, Optional[tuple], list[dict]]] = {}
_csv_cache: dict[str, tuple[float, list[dict]]] = {}


def _ttl() -> float:
    try:
        return max(0.0, float(os.environ.get("CATALOG_VERSION_CHECK_SECONDS", 30.0)))
    except ValueError:
        return 30.0


def _csv_rows(path: Path) -> list[dict]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    cached = _csv_cache.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error):
        logger.warning("failed to read course catalogue CSV: %s", path, exc_info=True)
        return []
    _csv_cache[str(path)] = (mtime, rows)
    return rows


def course_rows_token(db_path: Optional[Path] = None) -> Optional[tuple]:
    """Change marker of the stored catalogue; None while the CSV is in use."""
    path = Path(db_path) if db_path is not None else config.catalog_db_path()
    token = course_store.course_token(path=path)
    return (str(path), *token) if token else None


def course_rows(*, csv_path: Optional[Path] = None, db_path: Optional[Path] = None) -> list[dict]:
    """Current course rows. Treat the returned list and its dicts as read-only."""
    csv_file = Path(csv_path) if csv_path is not None else CSV_PATH
    db_file = Path(db_path) if db_path is not None else config.catalog_db_path()
    key = (str(db_file), str(csv_file))
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < _ttl():
        return cached[2]
    token = course_rows_token(db_file)
    if cached and cached[1] == token and token is not None:
        _cache[key] = (now, token, cached[2])
        return cached[2]
    rows = course_store.get_course_rows(path=db_file) if token else []
    if not rows:
        token, rows = None, _csv_rows(csv_file)
    _cache[key] = (now, token, rows)
    return rows


_generation = 0
_version_cache: Optional[tuple[float, str, tuple]] = None


def version() -> tuple:
    """Cheap marker for caches derived from course rows (default store only).

    Changes when this process syncs the catalogue, and within the usual
    re-check interval when another process does.
    """
    global _version_cache
    path = str(config.catalog_db_path())
    now = time.monotonic()
    cached = _version_cache
    if cached and cached[1] == path and now - cached[0] < _ttl():
        return cached[2]
    value = (_generation, course_rows_token())
    _version_cache = (now, path, value)
    return value


def source_label(db_path: Optional[Path] = None) -> str:
    return "catalog_store" if course_rows_token(db_path) else "local_csv"


def clear_cache(*, keep_csv: bool = False) -> None:
    """Forget cached rows; called after a sync in this process and between tests.

    ``keep_csv`` retains the parsed CSV (it is keyed by file modification time
    and stays valid), so derived lookups are not rebuilt needlessly.
    """
    global _generation, _version_cache
    _generation += 1
    _version_cache = None
    _cache.clear()
    if not keep_csv:
        _csv_cache.clear()
