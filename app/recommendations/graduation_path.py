"""What to take next when the student states no preference: the degree path.

A request such as "recommend my courses for next quarter" names no goal, so
the default goal is graduating. From the courses the student can take this
term the path keeps only those that move the degree forward, in layers:

1. Required courses that can be taken now. A required course is taken sooner
   or later, so an A rate cannot say whether to take it. The order is how
   many later required courses wait on it, then how rarely it is offered.
2. Requirements with a real choice ("COMPSCI 132 or EECS 148", electives).
   Here grades and ratings do help, and are shown as they are, not as a score.
3. General-education categories the recorded courses do not cover yet.

Pure functions over plain dicts; nothing here reads a database or the network.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Callable, Iterable, Mapping, Optional

# Courses per category on the catalogue's GE page. Category V is three courses
# with at least one in Va and one in Vb, and is handled apart.
GE_REQUIRED = (("IA", 2), ("IB", 1), ("II", 3), ("III", 3), ("IV", 3), ("VI", 1), ("VII", 1), ("VIII", 1))
# VI can be met by school records or exams that no transcript row shows.
GE_NOT_PROVABLE_FROM_COURSES = frozenset({"VI"})
# A requirement with more options than this is an elective pool, not a path:
# its courses do not count as "required later" when measuring what a course unlocks.
SMALL_CHOICE = 10
MAX_ALTERNATIVES = 6
MAX_GE_CANDIDATES = 5
MAX_LISTED = 12

_DAY = re.compile(r"Th|Tu|Sa|Su|M|W|F")
_REGULAR = ("Fall", "Winter", "Spring")


def compact(course_id) -> str:
    """``"I&C SCI 45C"`` and ``"I&CSCI45C"`` are the same course."""
    return re.sub(r"\s+", "", str(course_id or "")).upper()


# ── requirements ─────────────────────────────────────────


def unmet_needs(requirements, done: set[str], units_of: Callable[[str], Optional[float]]) -> list[dict]:
    """Requirements still open, each as ``{label, need, options}`` or ``{label, manual}``."""
    needs: list[dict] = []
    for requirement in requirements or []:
        if not isinstance(requirement, Mapping):
            continue
        kind = requirement.get("requirementType")
        label = " ".join(str(requirement.get("label") or "").split())
        if kind == "Group":
            branches = [unmet_needs([branch], done, units_of) for branch in requirement.get("requirements") or []]
            required = int(requirement.get("requirementCount") or len(branches))
            satisfied = sum(1 for branch in branches if not branch)
            if satisfied >= required:
                continue
            # Follow the branches the student is closest to finishing.
            open_branches = sorted((branch for branch in branches if branch),
                                   key=lambda branch: sum(need.get("need", 1) for need in branch))
            for branch in open_branches[: required - satisfied]:
                needs.extend(branch)
        elif kind in {"Course", "Unit"}:
            courses = [compact(course) for course in requirement.get("courses") or [] if compact(course)]
            if not courses:
                needs.append({"label": label, "manual": True})
                continue
            have = [course for course in courses if course in done]
            if kind == "Course":
                need = int(requirement.get("courseCount") or len(courses)) - len(have)
            else:
                remaining = float(requirement.get("unitCount") or 0) - sum(units_of(course) or 4.0 for course in have)
                need = math.ceil(remaining / 4.0) if remaining > 0 else 0
            if need > 0:
                needs.append({"label": label, "need": need, "manual": False,
                              "options": [course for course in courses if course not in done]})
        else:
            needs.append({"label": label, "manual": True})
    return needs


def _leaves(tree) -> Iterable[tuple[str, bool]]:
    """Every course a prerequisite tree names, with whether it may be taken alongside."""
    if isinstance(tree, list):
        for child in tree:
            yield from _leaves(child)
    elif isinstance(tree, Mapping):
        for key in ("AND", "OR"):
            if key in tree:
                yield from _leaves(tree[key])
        if str(tree.get("prereqType") or "").lower() == "course" and tree.get("courseId"):
            yield compact(tree["courseId"]), tree.get("coreq") is True


def _unlocks(targets: set[str], done: set[str], info) -> dict[str, list[str]]:
    """course → the later required courses that list it (directly or through a chain)."""
    direct: dict[str, set[str]] = {}
    for target in targets:
        for leaf, together in _leaves((info(target) or {}).get("prerequisite_tree")):
            # A course taken alongside (a lab with its lecture) is a companion,
            # not something waiting on this one.
            if leaf not in done and leaf != target and not together:
                direct.setdefault(leaf, set()).add(target)
    reach: dict[str, list[str]] = {}
    for course in direct:
        seen: list[str] = []
        queue = sorted(direct[course])
        while queue:
            following = queue.pop(0)
            if following in seen or following == course:
                continue
            seen.append(following)
            queue.extend(sorted(direct.get(following, ())))
        reach[course] = seen
    return reach


def _offered_quarters(record: Mapping, term: str) -> list[str]:
    """Regular quarters the course ran in over the last two years, this term included."""
    match = re.match(r"(\d{4})\s+(\w+)", term or "")
    year = int(match[1]) if match else None
    quarters = {match[2]} if match and match[2] in _REGULAR else set()
    for offered in record.get("terms_offered") or []:
        found = re.match(r"(\d{4})\s+(\w+)", str(offered))
        if found and found[2] in _REGULAR and (year is None or year - 2 <= int(found[1]) <= year):
            quarters.add(found[2])
    return [quarter for quarter in _REGULAR if quarter in quarters]


# ── general education ────────────────────────────────────


def ge_missing(done: set[str], info, basis: Optional[Mapping] = None) -> list[dict]:
    """GE categories not yet covered, with how many courses are missing.

    Without ``basis`` every done course's labels count. With it, the count
    comes from the courses actually earned (so a renamed course or an
    "A or B" exam credit is one course), plus what the AP chart states and
    the categories the student marked complete.
    """
    counts: Counter = Counter()
    quantitative = 0

    def add(course: str) -> None:
        nonlocal quantitative
        labels = set((info(course) or {}).get("ge") or [])
        counts.update(labels)
        quantitative += bool(labels & {"VA", "VB"})

    if basis is None:
        for course in done:
            add(course)
    else:
        for course in dict.fromkeys(compact(course) for course in basis.get("courses") or []):
            add(course)
        for exam in basis.get("exam_credits") or []:
            earned: Counter = Counter()
            for course in exam.get("ge_courses") or []:
                labels = set((info(compact(course)) or {}).get("ge") or [])
                earned.update(labels)
                quantitative += bool(labels & {"VA", "VB"})
            for code, stated in (exam.get("ge_direct") or {}).items():
                earned[code] = max(earned[code], int(stated))   # the chart and the label are the same credit
            counts.update(earned)
    confirmed = {str(code).upper() for code in (basis or {}).get("confirmed") or []}
    missing = [{"category": code, "need": required - counts[code]}
               for code, required in GE_REQUIRED if counts[code] < required and code not in confirmed]
    for code in ("VA", "VB"):
        if counts[code] < 1 and code not in confirmed and "V" not in confirmed:
            missing.append({"category": code, "need": 1})
    if (quantitative < 3 and counts["VA"] >= 1 and counts["VB"] >= 1
            and not confirmed & {"V"} and not {"VA", "VB"} <= confirmed):
        missing.append({"category": "V", "need": 3 - quantitative})
    return missing


# ── timetable ────────────────────────────────────────────


def _minutes(value) -> Optional[int]:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value or "").strip())
    return int(match[1]) * 60 + int(match[2]) if match else None


def _slot(section: Mapping):
    days = set(_DAY.findall(str(section.get("days") or "")))
    start, end = _minutes(section.get("start_time")), _minutes(section.get("end_time"))
    return (days, start, end) if days and start is not None and end is not None else None


def _clash(first, second) -> bool:
    return bool(first[0] & second[0]) and first[1] < second[2] and second[1] < first[2]


def _primary_sections(row: Mapping) -> list[Mapping]:
    sections = [section for section in row.get("sections") or []
                if isinstance(section, Mapping) and not section.get("is_cancelled") and section.get("section_code")]
    lectures = [section for section in sections
                if str(section.get("section_type") or "").lower().startswith(("lec", "sem"))]
    return lectures or sections


def _fit(row: Mapping, taken: list) -> tuple[Optional[Mapping], bool]:
    """A primary section that does not clash with what is already chosen.

    ``(section, True)`` when it fits, ``(None, False)`` when every section
    clashes. A section without a published time fits, since nothing is known
    against it.
    """
    for section in _primary_sections(row):
        slot = _slot(section)
        if slot is None or not any(_clash(slot, other) for other in taken):
            return section, True
    return None, False


_TIME_RULES = frozenset({"earliest_start_violation", "excluded_weekday"})


def _only_time_rules(record: Mapping) -> bool:
    """Excluded solely because no section fits the student's time condition."""
    if list(record.get("reasons") or []) != ["no_compatible_section_bundle"]:
        return False
    found = {reason for section in record.get("excluded_sections") or [] if isinstance(section, Mapping)
             for reason in section.get("reasons") or []}
    return bool(found & _TIME_RULES)


# ── the path ─────────────────────────────────────────────


def build_graduation_path(
    *,
    requirements,
    completed: Iterable[str],
    candidates: Mapping[str, Mapping],
    excluded: Mapping[str, Mapping],
    course_info: Callable[[str], Optional[Mapping]],
    term: str,
    evidence: Optional[Callable[[list[str]], Mapping[str, Mapping]]] = None,
    program_name: Optional[str] = None,
    exam_credit_count: int = 0,
    in_progress: Iterable[str] = (),
    ge_basis: Optional[Mapping] = None,
    equivalents: Optional[Callable[[str], Iterable[str]]] = None,
    target_units: float = 16.0,
    max_courses: int = 4,
) -> dict:
    """The layered shortlist and a suggested term. Keys of ``candidates`` and
    ``excluded`` are compact course IDs; ``candidates`` are this term's
    takeable courses, ``excluded`` the ones the prefilter ruled out."""
    done = {compact(course) for course in completed or [] if compact(course)}
    info = lambda course: course_info(course) or {}  # noqa: E731
    units_of = lambda course: (candidates.get(course) or {}).get("units_value") or info(course).get("units")  # noqa: E731

    def display(course: str) -> str:
        return str((candidates.get(course) or {}).get("course_id") or info(course).get("course_id") or course)

    taking = {compact(course) for course in in_progress or [] if compact(course)}

    def offered_id(course: str) -> str:
        """A requirement may list a course under an ID it no longer has."""
        if course in candidates or equivalents is None:
            return course
        return next((compact(name) for name in equivalents(display(course)) if compact(name) in candidates), course)

    needs = unmet_needs(requirements, done, units_of)
    open_needs = [need for need in needs if not need["manual"]]
    for need in open_needs:
        need["options"] = list(dict.fromkeys(offered_id(course) for course in need["options"]))
    path_courses = {course for need in open_needs if len(need["options"]) <= SMALL_CHOICE for course in need["options"]}
    unlocks = _unlocks(path_courses, done, info)

    required_now: list[dict] = []
    choices: list[dict] = []
    blocked: list[dict] = []
    outside_times: list[dict] = []
    not_offered: list[dict] = []
    reasons: dict[str, dict] = {}
    placed: set[str] = set()
    for need in open_needs:
        options = need["options"]
        takeable = [course for course in options if course in candidates]
        fixed = need["need"] >= len(options)  # every remaining option must be taken
        for course in options:
            if course in candidates or len(options) > SMALL_CHOICE:
                continue
            if course in excluded and _only_time_rules(excluded[course]):
                # Takeable, but every section breaks the student's own time rule: theirs to weigh.
                outside_times.append({"course_id": display(course), "requirement": need["label"],
                                      "unlock_count": len(unlocks.get(course, [])),
                                      "unlocks": [display(item) for item in unlocks.get(course, [])][:3]})
            elif course in excluded:
                blocked.append({"course_id": display(course), "requirement": need["label"],
                                "missing_prerequisites": list(excluded[course].get("missing_prerequisites") or []),
                                "reasons": list(excluded[course].get("reasons") or [])})
            else:
                not_offered.append({"course_id": display(course), "requirement": need["label"]})
        if fixed:
            for course in takeable:
                if course in placed:
                    continue
                placed.add(course)
                quarters = _offered_quarters(info(course), term)
                following = [display(item) for item in unlocks.get(course, [])]
                companions = [display(leaf) for leaf, together in _leaves(info(course).get("prerequisite_tree"))
                              if together and leaf not in done and leaf in candidates]
                waiting_on = [display(leaf) for leaf, together in _leaves(info(course).get("prerequisite_tree"))
                              if leaf in taking and not together]
                item = {"course_id": display(course), "title": info(course).get("title"),
                        "units": units_of(course), "requirement": need["label"],
                        # Takeable only if these in-progress courses are passed (with any minimum grade).
                        **({"depends_on_in_progress": waiting_on} if waiting_on else {}),
                        "unlock_count": len(following), "unlocks": following[:6],
                        "offered_quarters": quarters,
                        "only_quarter": quarters[0] if len(quarters) == 1 else None,
                        "take_with": companions}
                required_now.append(item)
                reasons[course] = {"tier": "required", "requirement": need["label"],
                                   "unlock_count": len(following), "unlocks": following[:3],
                                   "only_quarter": item["only_quarter"]}
        elif takeable:
            choices.append({"requirement": need["label"], "need": need["need"], "_takeable": takeable,
                            "takeable_count": len(takeable),
                            "blocked_count": sum(course in excluded for course in options),
                            "not_offered_count": sum(course not in candidates and course not in excluded
                                                     for course in options)})
    required_now.sort(key=lambda item: (-item["unlock_count"], len(item["offered_quarters"]) or 9,
                                        info(compact(item["course_id"])).get("numeric") or 999, item["course_id"]))

    # General education, from recorded courses and catalogue labels.
    missing = ge_missing(done, info, ge_basis)
    missing_codes = {entry["category"] for entry in missing}
    # Unprovable from courses, unless the student has said it is still open.
    unprovable = GE_NOT_PROVABLE_FROM_COURSES - {str(code).upper() for code in (ge_basis or {}).get("not_met") or []}
    major_open = {course for need in open_needs for course in need["options"]}
    ge_pool: dict[str, list[str]] = {}
    for entry in missing:
        code = entry["category"]
        wanted = {"VA", "VB"} if code == "V" else {code}
        ge_pool[code] = [] if code in unprovable else [
            course for course, row in candidates.items()
            if course not in done and wanted & set(row.get("ge_categories") or [])
            and (info(course).get("numeric") or 0) < 200]

    wanted_evidence = sorted({course for choice in choices for course in choice["_takeable"]}
                             | {course for pool in ge_pool.values() for course in pool})
    facts = dict(evidence(wanted_evidence) or {}) if evidence and wanted_evidence else {}

    def measured(course: str) -> dict:
        fact = facts.get(course) or {}
        return {"course_id": display(course), "title": info(course).get("title"), "units": units_of(course),
                **{key: fact.get(key) for key in ("a_rate", "a_rate_sample", "a_rate_scope",
                                                  "rating", "rating_sample", "instructor")}}

    def by_evidence(course: str):
        fact = facts.get(course) or {}
        return (fact.get("a_rate") is None, -(fact.get("a_rate") or 0.0),
                fact.get("rating") is None, -(fact.get("rating") or 0.0), course)

    for choice in choices:
        ordered = sorted(choice.pop("_takeable"), key=by_evidence)
        choice["alternatives"] = [measured(course) for course in ordered[:MAX_ALTERNATIVES]]
        choice["_order"] = ordered
        for course in ordered:
            reasons.setdefault(course, {"tier": "choice", "requirement": choice["requirement"],
                                        "alternatives": choice["takeable_count"]})

    ge_entries: list[dict] = []
    for entry in missing:
        code = entry["category"]
        if code in unprovable:
            ge_entries.append({**entry, "status": "unconfirmed", "candidate_count": 0, "candidates": []})
            continue
        pool = ge_pool[code]
        covered = [display(course) for course in pool if course in major_open and course in placed]
        covers = lambda course: len(missing_codes & set(candidates[course].get("ge_categories") or []))  # noqa: E731
        ordered = sorted(pool, key=lambda course: (course not in major_open, -covers(course), *by_evidence(course)))
        ge_entries.append({**entry, "status": "missing", "covered_by_required_course": covered,
                           "candidate_count": len(pool), "_order": ordered,
                           # A required course already fills it: no extra GE course to pick.
                           "candidates": [] if covered else [
                               {**measured(course),
                                "ge_categories": list(candidates[course].get("ge_categories") or []),
                                "also_major_requirement": course in major_open}
                               for course in ordered[:MAX_GE_CANDIDATES]]})
        for course in ordered:
            fills = sorted(missing_codes & set(candidates[course].get("ge_categories") or []))
            reasons.setdefault(course, {"tier": "ge", "ge_categories": fills or [code]})

    plan = _suggest_term(required_now, choices, ge_entries, candidates, info, units_of, done,
                         target_units=target_units, max_courses=max_courses)
    for choice in choices:
        choice.pop("_order", None)
    for entry in ge_entries:
        entry.pop("_order", None)
    return {
        "term": term, "program": program_name,
        "basis": "recorded courses, this term's timetable, program requirements and catalogue GE labels",
        "required_now": required_now[:MAX_LISTED],
        "choose": choices[:MAX_LISTED],
        "ge": {"estimated_from": "recorded courses, mapped exam and transfer credit, courses in progress and catalogue GE labels",
               "exam_credits_not_counted": int(exam_credit_count or 0),
               "confirmed_by_student": sorted((ge_basis or {}).get("confirmed") or []),
               "categories": ge_entries},
        "blocked_by_prerequisite": blocked[:MAX_LISTED],
        "required_outside_time_condition": outside_times[:MAX_LISTED],
        "not_offered_this_term": not_offered[:MAX_LISTED],
        "needs_advisor": [need["label"] for need in needs if need["manual"]][:6],
        "in_progress": sorted(display(course) for course in taking),
        "suggested_plan": plan,
        "reasons": {display(course): reason for course, reason in reasons.items()},
    }


def _suggest_term(required_now, choices, ge_entries, candidates, info, units_of, done, *,
                  target_units, max_courses) -> dict:
    """Greedy term: required courses first, one per open choice, one GE course."""
    ge_first = next((entry["_order"][0] for entry in ge_entries
                     if entry.get("status") == "missing" and entry.get("_order")
                     and not entry.get("covered_by_required_course")), None)
    queue: list[tuple[str, str]] = [(compact(item["course_id"]), "required") for item in required_now]
    queue += [(choice["_order"][0], "choice") for choice in choices if choice.get("_order")]
    major_slots = max_courses - (1 if ge_first and max_courses >= 3 else 0)
    chosen: list[dict] = []
    slots: list = []
    skipped: list[str] = []
    units = 0.0
    picked: set[str] = set()

    def add(course: str, tier: str) -> bool:
        nonlocal units
        row = candidates.get(course)
        if row is None or course in picked or course in done:
            return False
        section, fits = _fit(row, slots)
        if not fits:
            skipped.append(str(row.get("course_id") or course))
            return False
        slot = _slot(section) if section else None
        if slot:
            slots.append(slot)
        picked.add(course)
        course_units = float(units_of(course) or 4.0)
        units += course_units
        chosen.append({"course_id": str(row.get("course_id") or course), "tier": tier, "units": course_units,
                       "section_code": section.get("section_code") if section else None,
                       "time_known": slot is not None})
        return True

    main = 0
    for course, tier in queue:
        if main >= major_slots or units >= target_units:
            break
        if add(course, tier):
            # A lab that must be taken with its lecture rides along.
            main += 1
            for leaf, together in _leaves(info(course).get("prerequisite_tree")):
                if together:
                    add(leaf, tier)
            for item in required_now:
                other = compact(item["course_id"])
                if course in {leaf for leaf, together in _leaves(info(other).get("prerequisite_tree")) if together}:
                    add(other, "required")
    if ge_first and units < target_units + 4:
        for entry in ge_entries:
            if entry.get("status") == "missing" and not entry.get("covered_by_required_course"):
                if any(add(course, "ge") for course in entry.get("_order", [])[:MAX_GE_CANDIDATES]):
                    break
    return {"courses": chosen, "total_units": units, "skipped_for_time_conflict": skipped,
            "time_check": "lecture times only; discussion and lab times are not checked"}
