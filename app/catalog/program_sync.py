"""Sync every degree-requirement list from Anteater into the catalog store.

Three list requests (majors, minors, specializations), one detail request per
program (about 300) and four campus-wide sets (GE, UC, CHC4, CHC2). Requests
run one at a time, each retried a couple of times. Everything must succeed
before anything is written, so a partial crawl never replaces a complete
snapshot.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.catalog import program_store

logger = logging.getLogger(__name__)

SOURCE = "anteater_programs"
ATTEMPTS = 3
UGRAD_REQUIREMENT_IDS = ("GE", "UC", "CHC4", "CHC2")
_LISTS = (("major", "/programs/majors", "/programs/major"),
          ("minor", "/programs/minors", "/programs/minor"),
          ("specialization", "/programs/specializations", "/programs/specialization"))


@dataclass(frozen=True)
class ProgramSyncResult:
    outcome: str  # "synced" | "rejected_shrink" | "failed"
    program_count: int
    previous_program_count: int = 0
    requests: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.outcome == "synced"


def _default_fetch(endpoint: str, params: Optional[dict] = None):
    from app.data import anteater
    return anteater._get_json_result(f"{anteater.ANTEATER_BASE_URL}{endpoint}", params=params)


class _Failed(Exception):
    pass


def sync_programs(
    *,
    fetch: Optional[Callable] = None,
    force: bool = False,
    retry_wait_s: float = 1.0,
    path: Optional[Path] = None,
) -> ProgramSyncResult:
    """Fetch every program's requirements and store them. Never raises on fetch errors."""
    fetch = fetch or _default_fetch
    before = program_store.get_program_snapshot(path=path)
    held = int(before["program_count"]) if before else 0
    requests = 0

    def get(endpoint: str, params: Optional[dict], expect: type):
        nonlocal requests
        error = "no attempt"
        for attempt in range(ATTEMPTS):
            requests += 1
            try:
                result = fetch(endpoint, params)
            except Exception as exc:
                error = f"fetch_exception: {type(exc).__name__}: {exc}"
            else:
                data = getattr(result, "data", None)
                if not getattr(result, "ok", False):
                    error = f"{getattr(result, 'status', 'error')}: {getattr(result, 'error', None)}"
                elif not isinstance(data, expect):
                    error = f"schema_error: expected {expect.__name__}"
                else:
                    return data
            if attempt + 1 < ATTEMPTS and retry_wait_s > 0:
                time.sleep(retry_wait_s)
        label = f"{endpoint} {params}" if params else endpoint
        raise _Failed(f"{label}: {error}")

    rows: list[dict] = []
    try:
        for kind, list_endpoint, detail_endpoint in _LISTS:
            for summary in get(list_endpoint, None, list):
                if not isinstance(summary, dict) or not summary.get("id"):
                    continue
                detail = get(detail_endpoint, {"programId": summary["id"]}, dict)
                rows.append({"kind": kind, "program_id": summary["id"], "summary": summary, "detail": detail})
        for requirement_id in UGRAD_REQUIREMENT_IDS:
            detail = get("/programs/ugradRequirements", {"id": requirement_id}, dict)
            rows.append({"kind": "ugrad_requirement", "program_id": requirement_id,
                         "summary": {"id": requirement_id, "name": requirement_id}, "detail": detail})
    except _Failed as exc:
        program_store.record_program_sync_failure(str(exc), path=path)
        logger.warning("program sync failed after %s requests: %s", requests, exc)
        return ProgramSyncResult("failed", held, held, requests, str(exc))

    try:
        replaced = program_store.replace_programs(rows, source=SOURCE, force=force, path=path)
    except ValueError as exc:
        program_store.record_program_sync_failure(f"invalid_rows: {exc}", path=path)
        return ProgramSyncResult("failed", held, held, requests, f"invalid_rows: {exc}")
    if not replaced.replaced:
        snapshot = program_store.get_program_snapshot(path=path) or {}
        return ProgramSyncResult("rejected_shrink", held, held, requests, snapshot.get("last_error"))
    logger.info("program sync ok programs=%s previous=%s requests=%s",
                replaced.program_count, replaced.previous_program_count, requests)
    return ProgramSyncResult("synced", replaced.program_count, replaced.previous_program_count, requests)
