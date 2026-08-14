"""
Memory inspection API.

Powers the Memory panel in the UI — lets users see what ZotAdvisor
remembers about them (facts, preferences, major progress) and forget
individual preferences or all of them.

Endpoints read and write through the active MemoryManager provider. The
router intentionally does not touch JSON files directly, so provider
caches stay coherent inside the current process.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.deps import current_user_optional
from app.catalog.normalization import parse_course_mention
from app.memory import get_memory_manager
from app.data.uci_general.major_requirements import (
    get_major, compute_progress,
)


router = APIRouter()


# Names students might say → catalogue slug. Add entries as more majors
# get hand-encoded. Unrecognized names → no progress section in response.
_MAJOR_NAME_TO_SLUG: dict[str, str] = {
    "computer science":     "computerscience_bs",
    "computer science b.s.": "computerscience_bs",
    "cs":                   "computerscience_bs",
    # "data science":         "datascience_bs",       # future
    # "software engineering": "softwareengineering_bs",
    # "informatics":          "informatics_bs",
}


def _major_slug_from_profile(profile: dict) -> Optional[str]:
    raw = (profile or {}).get("major", "")
    if not isinstance(raw, str):
        return None
    return _MAJOR_NAME_TO_SLUG.get(raw.strip().lower())


# ── GET snapshot ─────────────────────────────────────────
#
# Endpoint paths still take {user_id} for backward compatibility with
# the existing frontend (USER_ID = 'demo_001'), but the authoritative
# user id comes from the session cookie via current_user_optional.
# Unauthenticated callers fall back to demo_001 — the legacy demo
# flow keeps working with no frontend change required.

@router.get("/api/memory/{user_id}")
def get_memory(user_id: str, user: dict = Depends(current_user_optional)):
    real_user_id = user["id"]
    snapshot = get_memory_manager().get_memory_snapshot(real_user_id)
    profile = snapshot.get("profile") or {}
    prefs = snapshot.get("preferences") or []
    facts = snapshot.get("facts") or []

    # Coerce shapes defensively (the JSON files are user-editable).
    if not isinstance(profile, dict):
        profile = {}
    if not isinstance(prefs, list):
        prefs = []
    if not isinstance(facts, list):
        facts = []

    # Compute major progress (only for hand-crafted majors).
    progress = None
    slug = _major_slug_from_profile(profile)
    if slug:
        major = get_major(slug)
        if isinstance(major, dict) and "specializations" in major:
            progress = compute_progress(
                slug,
                completed=profile.get("completed_courses") or [],
                in_progress=profile.get("selected_courses") or [],
            )
            progress["name"]   = major.get("name", "")
            progress["degree"] = major.get("degree", "")
            progress["slug"]   = slug

    return {
        "user_id": real_user_id,
        "profile": profile,
        "facts":   facts,
        "preferences": prefs,
        "major_progress": progress,
    }


# ── POST profile (onboarding write + later profile-editor) ──
#
# Generic merge-update endpoint. Used by:
#   - Phase C onboarding wizard (writes major/year/school + courses)
#   - Phase D profile editor (toggles completed_courses)
# Path user_id is ignored — real user comes from session cookie.
# For brand-new users we mkdir on demand so the wizard's very first
# save doesn't 404.

class ProfileUpdate(BaseModel):
    major:             Optional[str]       = None
    year:              Optional[str]       = None
    college:           Optional[str]       = None
    school_slug:       Optional[str]       = None
    program_id:        Optional[str]       = None  # Anteater id, e.g. "BS-201"
    catalog_year:      Optional[str]       = None  # e.g. "2024-2025"
    completed_courses: Optional[list[str]] = None
    selected_courses:  Optional[list[str]] = None


@router.post("/api/memory/{user_id}/profile")
def update_profile(
    user_id: str, body: ProfileUpdate,
    user: dict = Depends(current_user_optional),
):
    real_user_id = user["id"]
    manager = get_memory_manager()
    profile = manager.get_profile(real_user_id)

    updates = body.model_dump(exclude_none=True)
    # Treat "" / [] as "skip" so partial submissions don't blank fields
    # the user didn't touch on this round. Lists are deduplicated +
    # stable-sorted so re-submits don't churn the file.
    cleaned: dict = {}
    profile_warnings: list[str] = []
    for k, v in updates.items():
        if isinstance(v, str):
            v = v.strip()
            if v:
                cleaned[k] = v
        elif isinstance(v, list):
            # Drop empties + dedupe (preserve first-seen order)
            seen, deduped = set(), []
            for item in v:
                if not isinstance(item, str): continue
                item = item.strip().upper()
                if not item:
                    continue
                if item in seen:
                    profile_warnings.append(
                        f"Duplicate {k} entry {item} was submitted once; confirm your course list."
                    )
                    continue
                seen.add(item)
                deduped.append(item)
                if parse_course_mention(item) is None:
                    profile_warnings.append(
                        f"{item} is not a recognized course-number format; it was kept for you to confirm."
                    )
            if deduped:
                cleaned[k] = deduped

    if not cleaned:
        return {
            "ok": True,
            "profile": profile,
            "updated": [],
            "profile_warnings": profile_warnings,
        }

    profile = manager.update_profile(real_user_id, cleaned)
    return {
        "ok": True,
        "profile": profile,
        "updated": list(cleaned.keys()),
        "profile_warnings": profile_warnings,
    }


# ── DELETE one preference ────────────────────────────────

@router.delete("/api/memory/{user_id}/preferences/{pref_id}")
def forget_preference(
    user_id: str, pref_id: str,
    user: dict = Depends(current_user_optional),
):
    real_user_id = user["id"]
    try:
        result = get_memory_manager().forget_preference(real_user_id, pref_id)
    except ValueError:
        raise HTTPException(status_code=500, detail="preferences.json is malformed")

    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No preference with id {pref_id!r}",
        )

    return {"ok": True, **result}


# ── POST forget all preferences ──────────────────────────

@router.post("/api/memory/{user_id}/preferences/forget_all")
def forget_all_preferences(
    user_id: str,
    user: dict = Depends(current_user_optional),
):
    real_user_id = user["id"]
    removed = get_memory_manager().forget_all_preferences(real_user_id)
    return {"ok": True, "removed": removed}
