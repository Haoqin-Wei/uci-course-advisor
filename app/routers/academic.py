"""Authenticated endpoints for structured transcript imports."""

from __future__ import annotations

import logging
from threading import RLock

from fastapi import APIRouter, Depends, Request, Response

from app.academic import get_academic_profile, import_transcript
from app.academic.models import TranscriptImportRequest
from app.auth.deps import current_user_required
from app.auth.rate_limit import RateLimit, check_rate_limit
from app.memory import get_memory_manager


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/academic", tags=["academic"])
# Keep academic snapshots and the recommendation/profile course mirror ordered
# across overlapping requests in this process, without unbounded per-user locks.
_TRANSCRIPT_LOCKS = tuple(RLock() for _ in range(64))

TRANSCRIPT_IMPORT_LIMIT = RateLimit(
    "academic.transcript_import",
    limit=5,
    window_seconds=24 * 60 * 60,
)


@router.post("/transcript/import")
def import_transcript_data(
    body: TranscriptImportRequest,
    request: Request,
    user: dict = Depends(current_user_required),
):
    """Persist only the browser's allow-listed structured academic fields."""
    user_id = user["id"]
    check_rate_limit(request, TRANSCRIPT_IMPORT_LIMIT, user_id)
    with _TRANSCRIPT_LOCKS[hash(user_id) % len(_TRANSCRIPT_LOCKS)]:
        result = import_transcript(user_id, body)

        # A new transcript replaces the completed-course compatibility snapshot,
        # including prior manual selections. An idempotent retry must not undo edits
        # or replace a newer import already saved under a different request ID.
        if not result["duplicate"]:
            manager = get_memory_manager()
            manager.update_profile(
                user_id,
                {"completed_courses": result["completed_course_ids"]},
                source_type="transcript_import",
            )

    logger.info(
        "transcript_import result=success read=%s accepted=%s added=%s "
        "updated=%s unchanged=%s older_ignored=%s skipped=%s",
        result["read"],
        result["accepted"],
        result["added"],
        result["updated"],
        result["unchanged"],
        result["older_ignored"],
        result["skipped"],
    )
    return result


@router.get("/profile")
def academic_profile(
    response: Response, include_gpa: bool = False, user: dict = Depends(current_user_required)
):
    response.headers["Cache-Control"] = "no-store"
    profile = get_memory_manager().get_profile(user["id"]) or {}
    return {
        "ok": True,
        **get_academic_profile(
            user["id"], profile.get("completed_courses") or [], include_gpa=include_gpa
        ),
    }
