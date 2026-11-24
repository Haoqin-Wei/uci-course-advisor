"""Checks the courses a model picked from the degree-path tray.

``build_graduation_path`` says what the student can and should take this
term. The model chooses within that tray; this module keeps the choice
inside it, free of lecture clashes and complete with courses that must be
taken together, and records what the choice leaves out. Nothing here ranks.
"""

from __future__ import annotations

from typing import Mapping, Optional

from app.recommendations.graduation_path import _fit, _slot, compact

TIER_ORDER = {"required": 0, "choice": 1, "ge": 2}
MAX_NOTE = 400
MAX_REASON = 160


def _clean(text, limit: int) -> Optional[str]:
    value = " ".join(str(text or "").split())
    return (value[:limit].rstrip() + ("…" if len(value) > limit else "")) or None


def review_picks(path: Mapping, picks, rows: Mapping[str, Mapping], *, left_out=None, note=None,
                 max_items: int = 8) -> dict:
    """The picks to stage, in tier order, and every change made to them.

    ``rows`` maps compact course IDs to this term's candidate rows (for
    lecture times). ``left_out`` is the model's own account of required
    courses it dropped: ``[{course_id, reason}]``.
    """
    tray = {compact(course): reason for course, reason in (path.get("reasons") or {}).items()}
    name = {compact(course): course for course in path.get("reasons") or {}}
    required = {compact(item["course_id"]): item for item in path.get("required_now") or []}
    planned = [compact(item["course_id"]) for item in (path.get("suggested_plan") or {}).get("courses") or []]

    wanted: list[str] = []
    outside: list[str] = []
    for raw in picks or []:
        course = compact(raw)
        if course in tray:
            if course not in wanted:
                wanted.append(course)
        elif str(raw or "").strip():
            outside.append(str(raw).strip())
    # Required first, in the server's order (a lab stays next to its lecture);
    # within the other tiers the model's order stands.
    place_in_plan = {course: index for index, course in enumerate(dict.fromkeys([*planned, *required]))}

    def order(course: str):
        tier = TIER_ORDER.get(tray[course].get("tier"), 9)
        return (tier, place_in_plan.get(course, len(place_in_plan)) if tier == 0 else 0)

    wanted.sort(key=order)

    kept: list[str] = []
    slots: list = []
    clashed: list[str] = []

    def place(course: str) -> bool:
        section, fits = _fit(rows.get(course) or {}, slots)
        if not fits:
            return False
        slot = _slot(section) if section else None
        if slot:
            slots.append(slot)
        kept.append(course)
        return True

    for course in wanted:
        if not place(course):
            clashed.append(name[course])

    # A lecture and the lab that must be taken with it go together, whichever was picked.
    companions: list[str] = []
    for course, item in required.items():
        for other in (compact(value) for value in item.get("take_with") or []):
            for present, absent in ((course, other), (other, course)):
                if present in kept and absent not in kept and absent in tray and len(kept) < max_items:
                    kept.append(absent)
                    companions.append(name[absent])

    stated = {compact(entry.get("course_id")): _clean(entry.get("reason"), MAX_REASON)
              for entry in left_out or [] if isinstance(entry, Mapping)}
    # A removed pick is replaced from the server's own plan, never from outside
    # the tray, and never with a course the model said it was leaving out.
    substitutes: list[str] = []
    for course in planned:
        if len(substitutes) >= len(outside) + len(clashed) or len(kept) >= max_items:
            break
        if (course in tray and course not in kept and course not in stated
                and name[course] not in clashed and place(course)):
            substitutes.append(name[course])

    kept = sorted(kept[:max_items], key=order)
    dropped = [{"course_id": name[course], "requirement": required[course].get("requirement"),
                "unlock_count": required[course].get("unlock_count") or 0,
                "unlocks": list(required[course].get("unlocks") or [])[:3],
                "reason": stated.get(course)}
               for course in planned if course in required and course not in kept]
    changed = bool(outside or clashed or companions or substitutes)
    return {
        "courses": [name[course] for course in kept],
        "removed_outside_tray": outside, "removed_for_time_conflict": clashed,
        "added_companions": companions, "substituted": substitutes,
        "left_out_required": dropped, "changed": changed,
        # The model's paragraph describes its own picks; once they changed it no longer does.
        "note": None if changed else _clean(note, MAX_NOTE),
    }
