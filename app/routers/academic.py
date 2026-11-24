"""Authenticated endpoints for structured transcript imports."""

from __future__ import annotations

import logging
from threading import RLock

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.academic import get_academic_profile, import_transcript, store
from app.academic.models import AcademicDetailsUpdate, TranscriptImportRequest
from app.agent import profile_followup
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
        def sync_completed_courses(completed_course_ids: list[str]) -> None:
            # Run before the academic commit so a failed or silently incomplete
            # memory write cannot report success or leave the new snapshot saved.
            manager = get_memory_manager()
            try:
                updated = manager.update_profile(
                    user_id,
                    {"completed_courses": completed_course_ids},
                    source_type="transcript_import",
                    strict=True,
                )
                if updated.get("completed_courses") != completed_course_ids:
                    raise RuntimeError("Completed-course mirror did not match the import")
            except Exception as exc:
                logger.warning("transcript_import mirror_sync_failed error=%s", type(exc).__name__)
                raise HTTPException(
                    status_code=503,
                    detail="Transcript import could not be completed. Please try again.",
                ) from exc

        # Successful duplicate requests skip the callback: an old request ID
        # must not undo a newer import or subsequent legitimate manual edits.
        result = import_transcript(user_id, body, before_commit=sync_completed_courses)

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
        "details": store.get_academic_details(user["id"]),
        "exam_credits": store.get_exam_credits(user["id"]),
        "transfer_credits": store.get_transfer_credits(user["id"]),
        "target_major": profile.get("major"),
        # What the record cannot tell and the student has not stated yet.
        "details_missing": profile_followup.missing_items(user["id"]),
    }


@router.post("/details")
def update_details(body: AcademicDetailsUpdate, response: Response,
                   user: dict = Depends(current_user_required)):
    """Save what the student states about their record: major on record,
    courses in progress, GE categories already met, transfer equivalents."""
    response.headers["Cache-Control"] = "no-store"
    with _TRANSCRIPT_LOCKS[hash(user["id"]) % len(_TRANSCRIPT_LOCKS)]:
        details = store.update_academic_details(user["id"], body)
    return {"ok": True, "details": details}


@router.get("/progress")
def degree_progress(response: Response, term: str = "", user: dict = Depends(current_user_required)):
    """Where the student stands on the degree for one term: required courses
    that can be taken now, what blocks the others, open choices and GE gaps."""
    response.headers["Cache-Control"] = "no-store"
    from app.agent import graduation
    from app.terms import parse_term_key
    from app.terms.service import get_term_resolution_service

    wanted = term.strip() or get_term_resolution_service().automatic_state()["automatic_term"]
    parsed = parse_term_key(wanted)
    if parsed.kind != "single":
        raise HTTPException(status_code=422, detail="term must be one quarter, for example 2027 Winter")
    canonical = parsed.terms[0].canonical_name
    path = graduation.progress_for_user(user["id"], canonical)
    if path is None:
        return {"ok": True, "available": False, "term": canonical,
                "reason": "A target major and recorded courses are needed to compute progress."}
    return {"ok": True, "available": True, "progress": {key: value for key, value in path.items() if key != "reasons"}}
