"""Authenticated endpoints for structured transcript imports."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.academic import get_academic_profile, import_transcript
from app.academic.models import TranscriptImportRequest
from app.auth.deps import current_user_required
from app.auth.rate_limit import RateLimit, check_rate_limit
from app.memory import get_memory_manager


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/academic", tags=["academic"])

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
    result = import_transcript(user_id, body)

    # Keep the existing recommendation/session compatibility field in sync.
    # Preserve manual courses that the transcript has never described. For
    # transcript-known courses, mirror the newest effective pass/fail state.
    manager = get_memory_manager()
    profile = manager.get_profile(user_id) or {}
    existing = profile.get("completed_courses") or []
    transcript_known = set(result["transcript_course_ids"])
    manual_only = [course_id for course_id in existing if course_id not in transcript_known]
    merged = list(dict.fromkeys([*manual_only, *result["completed_course_ids"]]))
    if merged != existing:
        manager.update_profile(
            user_id,
            {"completed_courses": merged},
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
def academic_profile(user: dict = Depends(current_user_required)):
    profile = get_memory_manager().get_profile(user["id"]) or {}
    return {
        "ok": True,
        **get_academic_profile(user["id"], profile.get("completed_courses") or []),
    }
