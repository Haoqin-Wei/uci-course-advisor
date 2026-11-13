"""Which terms have a published timetable, read from the local catalog store.

The catalog sync already fetches whole terms and records what it found, so
"does this term have course data" is answered locally instead of by probing
the remote API once more. The records use the same shape the term state used
to hold: ``available`` is True (sections stored), False (verified
unpublished) or None (only failed attempts so far).
"""

from __future__ import annotations

import os
import time
from typing import Optional

from app import config

_cache: Optional[tuple[float, str, dict[str, dict]]] = None


def _ttl() -> float:
    try:
        return max(0.0, float(os.environ.get("CATALOG_VERSION_CHECK_SECONDS", 30.0)))
    except ValueError:
        return 30.0


def _record(snapshot: dict) -> dict:
    held = int(snapshot.get("section_count") or 0)
    status = snapshot.get("status")
    if held > 0:
        available: Optional[bool] = True
    elif status == "unpublished":
        available = False
    else:
        available = None
    return {
        "available": available,
        "course_count": int(snapshot.get("course_count") or 0),
        "section_count": held,
        "status": status,
        "checked_at": snapshot.get("last_synced_at") or snapshot.get("last_attempt_at"),
        "source": "catalog_store",
    }


def catalog_availability() -> dict[str, dict]:
    """Canonical term name ("2027 Winter") → availability record."""
    global _cache
    from app.catalog import store

    path = str(config.catalog_db_path())
    now = time.monotonic()
    if _cache and _cache[1] == path and now - _cache[0] < _ttl():
        return _cache[2]
    records: dict[str, dict] = {}
    for snapshot in store.read_term_snapshots():
        year, quarter = snapshot.get("year"), snapshot.get("quarter")
        if year and quarter:
            records[f"{year} {quarter}"] = _record(snapshot)
    _cache = (now, path, records)
    return records


def merged_availability(state_availability: Optional[dict]) -> dict[str, dict]:
    """Stored-snapshot evidence, falling back to records carried in the term state.

    Nothing writes availability into the term state any more; a record found
    there (older state files, hand-built states) is used only for terms the
    catalog store knows nothing about.
    """
    merged = dict(state_availability or {})
    merged.update(catalog_availability())
    return merged


def clear_cache() -> None:
    global _cache
    _cache = None
