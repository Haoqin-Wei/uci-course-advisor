"""Gaps in the academic record: the one question to ask, and saving the answer.

A recommendation is given first; at most one question follows it. Each gap
is asked once (courses in progress once per planning term) and never again
after the student declines. Saving goes through the same store call as the
profile page, so a page, this conversation and a later agent agree.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from app.academic import store
from app.academic.models import AcademicDetailsUpdate
from app.catalog.course_equivalents import equivalents
from app.modules.state import DEFAULT_USER_ID
from app.recommendations.graduation_path import compact

logger = logging.getLogger(__name__)

LANGUAGE_CATEGORY = "VI"
# Most useful to a recommendation first.
ITEMS = store.DETAIL_PROMPTS
_COURSE_FIELDS = ("in_progress_courses", "transfer_courses")
_ANSWERS = {"in_progress_courses": "in_progress", "official_major": "official_major",
            "transfer_courses": "transfer_courses"}


def signed_in(user_id) -> bool:
    """Academic details belong to an account; the shared demo user has none."""
    return bool(user_id) and user_id != DEFAULT_USER_ID


def _profile(user_id: str) -> dict:
    from app.data import db
    found = db.get_student_profile(user_id)
    return (found.get("profile") if found.get("found") else {}) or {}


def _language_open(profile: dict) -> bool:
    """GE VI neither shown by a course or exam credit nor answered by the student."""
    basis = profile.get("ge_basis")
    if not basis or LANGUAGE_CATEGORY in {str(code).upper() for code in basis.get("not_met") or []}:
        return False
    from app.agent.graduation import course_catalogue
    from app.recommendations.graduation_path import ge_missing
    return any(entry["category"] == LANGUAGE_CATEGORY
               for entry in ge_missing(set(), course_catalogue().get, basis))


def missing_items(user_id: str, profile: Optional[dict] = None, details: Optional[dict] = None) -> list[str]:
    """What the record cannot tell and the student has not stated, in asking order."""
    details = details or store.get_academic_details(user_id)
    profile = profile if profile is not None else _profile(user_id)
    major = (details.get("official_major") or "").upper()
    checks = {
        "in_progress": not details.get("in_progress_courses"),
        # A transcript printed before a change of major still says "undeclared".
        "official_major": details.get("official_major_source") != "student" and (
            not major or major.startswith("UNDECLARED")),
        "ge_language": _language_open(profile),
        "transfer_courses": bool(store.get_transfer_credits(user_id)) and not details.get("transfer_courses"),
    }
    return [item for item in ITEMS if checks[item]]


def _question(item: str, details: dict, user_id: str, zh: bool) -> str:
    if item == "in_progress":
        return ("另外想确认一件事：你这学期正在上哪些课？把课号告诉我，我会把它们算作在读——"
                "以后不再重复推荐，也会用来判断先修。不想填的话直接说就行。" if zh else
                "One thing to confirm: which courses are you taking this term? Give me the course IDs and "
                "I will count them as in progress, so they are not recommended again and can meet "
                "prerequisites. Just say so if you would rather not answer.")
    if item == "official_major":
        printed = details.get("transcript_major")
        if printed:
            return (f"另外想确认一件事：成绩单上登记的专业是 {printed}。你现在的官方专业是什么？"
                    "它决定哪些课对你有专业限制。" if zh else
                    f"One thing to confirm: your transcript lists your major as {printed}. What is your official "
                    "major now? It decides which major-restricted courses you can enroll in.")
        return ("另外想确认一件事：你在学校登记的官方专业是什么？它决定哪些课对你有专业限制。" if zh else
                "One thing to confirm: what is your official major on record with the university? "
                "It decides which major-restricted courses you can enroll in.")
    if item == "ge_language":
        return ("另外想确认一件事：GE VI（英语以外的语言）从成绩单上判断不了，因为它可以用高中修满三年外语、"
                "AP 语言考试或语言免修考试来满足。你已经满足了吗？" if zh else
                "One thing to confirm: GE VI (Language Other Than English) cannot be read from a transcript, "
                "because three years of high-school language, an AP language exam or a language exemption "
                "exam can satisfy it. Have you already satisfied it?")
    credits = store.get_transfer_credits(user_id)
    listed = ("、" if zh else ", ").join(
        f"{row['institution_name']}（{row['units']:g} 学分）" if zh else f"{row['institution_name']} ({row['units']:g} units)"
        for row in credits if row.get("institution_name") and row.get("units") is not None) or (
            "其他学校" if zh else "another institution")
    return (f"另外想确认一件事：你有从 {listed} 转入的学分，成绩单没有写它们对应 UCI 的哪门课。"
            "它们抵的是哪些课？如果只算学分、不抵具体课程，也告诉我一声。" if zh else
            f"One thing to confirm: you have transfer credit from {listed}, and the transcript does not say "
            "which UCI courses it counts as. Which courses does it replace? Tell me too if it is units only.")


def question_for_turn(context: dict) -> Optional[dict]:
    """The question to put after this turn's recommendation, decided once per turn.

    Choosing it marks it asked: whether or not the student answers, the same
    gap is not raised again.
    """
    if "_profile_question" in context:
        return context["_profile_question"]
    context["_profile_question"] = None
    user_id = context.get("user_id")
    if not signed_in(user_id):
        return None
    try:
        state = context.get("_prefilter_state") or {}
        details = store.get_academic_details(user_id)
        term = (context.get("allowed_query_terms") or [context.get("term")])[0]
        zh = str(context.get("response_language") or "zh").startswith("zh")
        for item in missing_items(user_id, state.get("profile") or None, details):
            asked = details["prompts"].get(item)
            if asked and (asked.get("declined") or item != "in_progress" or asked.get("term") == term):
                continue
            store.mark_detail_prompt(user_id, item, term=term)
            context["_profile_question"] = {"item": item, "text": _question(item, details, user_id, zh)}
            break
    except Exception:
        logger.warning("profile follow-up question could not be chosen", exc_info=True)
    return context["_profile_question"]


def _known_courses(values: Iterable) -> tuple[list[str], list[str]]:
    """Course IDs the catalogue has (under any name the course has had), and the rest."""
    from app.agent.graduation import course_catalogue
    catalogue = course_catalogue()
    known: list[str] = []
    unknown: list[str] = []
    for raw in values or []:
        course = store._canonical_course_id(str(raw))
        if course and any(compact(name) in catalogue for name in equivalents(course)):
            if course not in known:
                known.append(course)
        else:
            unknown.append(str(raw).strip())
    return known, unknown


def save_statement(user_id: str, *, in_progress_courses=None, in_progress_term=None, official_major=None,
                   ge_confirmed=None, ge_not_met=None, transfer_courses=None, declined=None,
                   replace: bool = False) -> dict:
    """Save what the student said. Lists add to what is stored unless ``replace``.

    A course the catalogue does not have is returned in ``not_found`` and not
    saved, so a misheard course ID is confirmed with the student first.
    """
    if not signed_in(user_id):
        return {"ok": False, "error_code": "sign_in_required",
                "error": "Academic details are saved to an account; the student is not signed in."}
    current = store.get_academic_details(user_id)
    changes: dict = {}
    not_found: list[str] = []
    for field, values in (("in_progress_courses", in_progress_courses), ("transfer_courses", transfer_courses)):
        if values is None:
            continue
        known, unknown = _known_courses(values)
        not_found.extend(unknown)
        if known or (replace and not values):
            changes[field] = known if replace else list(dict.fromkeys([*current[field], *known]))
    for field, values in (("ge_confirmed", ge_confirmed), ("ge_not_met", ge_not_met)):
        if values is not None and (values or replace):
            changes[field] = list(values) if replace else [*current[field], *values]
    if isinstance(official_major, str) and official_major.strip():
        changes["official_major"] = official_major
    if isinstance(in_progress_term, str) and in_progress_term.strip():
        changes["in_progress_term"] = in_progress_term
    unknown_items = [item for item in declined or [] if item not in ITEMS]
    declined = [item for item in declined or [] if item in ITEMS]
    if not changes and not declined:
        return {"ok": False, "error_code": "nothing_to_save", "not_found": not_found,
                "rejected": unknown_items,
                "error": "Nothing was saved. Ask the student to confirm any course that was not found."}
    try:
        result = store.update_academic_details(user_id, AcademicDetailsUpdate(**changes)) if changes else {
            **current, "rejected": []}
    except ValueError as exc:
        return {"ok": False, "error_code": "invalid_details", "error": str(exc)[:300]}
    # An answer closes its question as surely as asking it did.
    answered = {_ANSWERS[field] for field in changes if field in _ANSWERS}
    if {"ge_confirmed", "ge_not_met"} & set(changes) and LANGUAGE_CATEGORY in {
            *result.get("ge_confirmed", []), *result.get("ge_not_met", [])}:
        answered.add("ge_language")
    for item in answered:
        store.mark_detail_prompt(user_id, item)
    for item in declined:
        store.mark_detail_prompt(user_id, item, declined=True)
    saved = {field: result[field] for field in changes if field in result}
    if "official_major" in changes:
        saved["official_major"] = result["official_major"]
    return {"ok": True, "saved": saved, "declined": declined, "not_found": not_found,
            "rejected": [*result.get("rejected", []), *unknown_items],
            "still_missing": missing_items(user_id, None, store.get_academic_details(user_id))}


def _saved_line(update: dict, zh: bool) -> str:
    saved = update.get("saved") or {}
    parts: list[str] = []
    sep = "、" if zh else ", "
    if saved.get("in_progress_courses"):
        parts.append(("在读课程 " if zh else "courses in progress: ") + sep.join(saved["in_progress_courses"]))
    if saved.get("official_major"):
        parts.append(("官方专业 " if zh else "official major: ") + str(saved["official_major"]))
    if saved.get("ge_confirmed"):
        parts.append(("已满足的 GE " if zh else "GE satisfied: ") + sep.join(saved["ge_confirmed"]))
    if saved.get("ge_not_met"):
        parts.append(("尚未满足的 GE " if zh else "GE still open: ") + sep.join(saved["ge_not_met"]))
    if saved.get("transfer_courses"):
        parts.append(("转学分对应课程 " if zh else "transfer credit counts as: ") + sep.join(saved["transfer_courses"]))
    lines = []
    if parts:
        applied = (("本次推荐已按更新后的档案计算。" if zh else " This recommendation uses the updated record.")
                   if update.get("applied_this_turn") else "")
        lines.append(("已记录：" if zh else "Saved to your record: ") + ("；" if zh else "; ").join(parts) +
                     ("。" if zh else ".") + applied)
    if update.get("not_found"):
        lines.append(("没有找到这些课号，未记录，请确认：" if zh else "These course IDs were not found and were not saved; please confirm them: ")
                     + sep.join(update["not_found"]) + ("。" if zh else "."))
    return "\n".join(lines)


def closing_note(context: dict, *, ask: bool) -> Optional[str]:
    """What the server adds after a controlled recommendation: saved details, then one question."""
    zh = str(context.get("response_language") or "zh").startswith("zh")
    blocks = [line for line in (_saved_line(update, zh) for update in context.get("_academic_updates") or []) if line]
    question = question_for_turn(context) if ask else context.get("_profile_question")
    if question:
        blocks.append(question["text"])
    return "\n\n".join(blocks) or None
