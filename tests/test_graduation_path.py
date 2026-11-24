"""No stated preference means the degree path, in layers, not one blended score."""

from __future__ import annotations

from app.agent import graduation, prefilter
from app.recommendations.filter_plan import empty_filter_plan
from app.recommendations.graduation_path import build_graduation_path, compact, ge_missing, unmet_needs
from app.recommendations.presentation import normalize_recommendation_card, render_recommendation_answer

TERM = "2027 Winter"


def course(tree=None, *, units=4.0, numeric=100, ge=(), offered=("2026 Fall", "2026 Winter", "2026 Spring")):
    return {"title": "T", "units": units, "numeric": numeric, "prerequisite_tree": tree or {},
            "terms_offered": list(offered), "ge": list(ge)}


def needs(*ids, grade=None):
    return {"AND": [{"prereqType": "course", "coreq": False, "courseId": cid} for cid in ids]}


CATALOGUE = {
    "ICS31": course(numeric=31, ge=("II", "VB")), "ICS32": course(numeric=32, ge=("II", "VA", "VB")),
    "ICS33": course(numeric=33, ge=("II", "VB")),
    "ICS45C": course(needs("ICS 33"), numeric=45),
    "ICS46": course(needs("ICS 45C"), numeric=46),
    "CS161": course(needs("ICS 46", "ICS 6B"), numeric=161),
    "CS141": course(needs("ICS 46"), numeric=141),
    "ICS6B": course(numeric=6, ge=("VB",)),
    "PHYS7D": course(numeric=7, offered=("2026 Winter", "2026 Spring")),
    "PHYS7LD": course({"AND": [{"prereqType": "course", "coreq": True, "courseId": "PHYS 7D"}]}, units=1.0, numeric=7),
    "CSE90": course(units=2.0, numeric=90, offered=("2025 Winter", "2026 Winter")),
    "CS132": course(numeric=132), "EECS148": course(numeric=148),
    "WR39B": course(ge=("IA",)), "WR39C": course(ge=("IA",)), "ENGR190W": course(numeric=190, ge=("IB",)),
    "SOC1": course(numeric=1, ge=("III", "VII")), "ANTH2": course(numeric=2, ge=("III", "VIII")),
    "HIST200": course(numeric=200, ge=("VII",)), "ART1": course(numeric=1, ge=("IV",)),
}
REQUIREMENTS = [
    {"label": "Intro", "requirementType": "Group", "requirementCount": 1, "requirements": [
        {"label": "31-33", "requirementType": "Course", "courseCount": 3, "courses": ["ICS31", "ICS32", "ICS33"]}]},
    {"label": "ICS 45C", "requirementType": "Course", "courseCount": 1, "courses": ["ICS45C"]},
    {"label": "ICS 46", "requirementType": "Course", "courseCount": 1, "courses": ["ICS46"]},
    {"label": "CS 161", "requirementType": "Course", "courseCount": 1, "courses": ["CS161"]},
    {"label": "CS 141", "requirementType": "Course", "courseCount": 1, "courses": ["CS141"]},
    {"label": "ICS 6B", "requirementType": "Course", "courseCount": 1, "courses": ["ICS6B"]},
    {"label": "Physics 7D-7LD", "requirementType": "Course", "courseCount": 2, "courses": ["PHYS7D", "PHYS7LD"]},
    {"label": "CSE 90", "requirementType": "Course", "courseCount": 1, "courses": ["CSE90"]},
    {"label": "Networks", "requirementType": "Course", "courseCount": 1, "courses": ["CS132", "EECS148"]},
    {"label": "Advisor approved science", "requirementType": "Course", "courseCount": 2, "courses": []},
]
DONE = ["ICS 31", "ICS 32", "ICS 33"]


def row(course_id, days="MWF", start="10:00", end="10:50", ge=()):
    return {"course_id": course_id, "ge_categories": list(ge), "sections": [
        {"section_code": "1" + str(abs(hash(course_id)) % 9000), "section_type": "Lec",
         "days": days, "start_time": start, "end_time": end}]}


def candidates(**overrides):
    rows = {
        "ICS45C": row("ICS 45C", "TuTh", "12:30", "13:50"), "ICS6B": row("ICS 6B", "MWF", "09:00", "09:50"),
        "PHYS7D": row("PHYS 7D", "MWF", "11:00", "11:50"), "PHYS7LD": row("PHYS 7LD", "Tu", "09:00", "10:50"),
        "CSE90": row("CSE 90", "Tu", "14:00", "15:20"),
        "CS132": row("CS 132", "TuTh", "14:00", "15:20"), "EECS148": row("EECS 148", "TuTh", "14:00", "15:20"),
        "SOC1": row("SOC 1", "MWF", "13:00", "13:50", ge=("III", "VII")),
        "ANTH2": row("ANTH 2", "MWF", "15:00", "15:50", ge=("III", "VIII")),
        "HIST200": row("HIST 200", "MWF", "16:00", "16:50", ge=("VII",)),
    }
    rows.update(overrides)
    return rows


EXCLUDED = {"ICS46": {"reasons": ["prerequisite_not_met"], "missing_prerequisites": ["ICS 45C (min C)"]},
            "CS161": {"reasons": ["prerequisite_not_met"], "missing_prerequisites": ["ICS 46", "ICS 6B"]},
            "CS141": {"reasons": ["prerequisite_not_met"], "missing_prerequisites": ["ICS 46"]}}
EVIDENCE = {"CS132": {"a_rate": 0.4, "rating": 3.0}, "EECS148": {"a_rate": 0.6, "rating": 2.0},
            "SOC1": {"a_rate": 0.7, "rating": 4.0}, "ANTH2": {"a_rate": 0.5, "rating": 4.5}}


def path(**kwargs):
    args = dict(requirements=REQUIREMENTS, completed=DONE, candidates=candidates(), excluded=EXCLUDED,
                course_info=lambda cid: CATALOGUE.get(cid), term=TERM,
                evidence=lambda ids: {cid: EVIDENCE[cid] for cid in ids if cid in EVIDENCE}, exam_credit_count=3)
    args.update(kwargs)
    return build_graduation_path(**args)


def test_finished_group_is_not_a_need_and_empty_list_goes_to_the_advisor():
    found = unmet_needs(REQUIREMENTS, {compact(c) for c in DONE}, lambda cid: 4.0)
    labels = [need["label"] for need in found]
    assert "31-33" not in labels and "Intro" not in labels
    assert {"label": "Advisor approved science", "manual": True} in found
    assert path()["needs_advisor"] == ["Advisor approved science"]


def test_unit_requirement_counts_units_already_earned():
    requirement = [{"label": "8 units", "requirementType": "Unit", "unitCount": 8, "courses": ["ICS31", "CS132", "CS141"]}]
    found = unmet_needs(requirement, {"ICS31"}, lambda cid: 4.0)
    assert found == [{"label": "8 units", "need": 1, "manual": False, "options": ["CS132", "CS141"]}]


def test_required_courses_are_ordered_by_what_waits_on_them_not_by_grades():
    required = path()["required_now"]
    assert [item["course_id"] for item in required][:2] == ["ICS 45C", "ICS 6B"]
    first = required[0]
    assert first["unlock_count"] == 3 and set(first["unlocks"]) == {"ICS46", "CS161", "CS141"}
    assert required[1]["unlock_count"] == 1          # ICS 6B only holds back CS 161
    assert all("a_rate" not in item for item in required)


def test_a_lab_taken_alongside_is_a_companion_not_something_unlocked():
    required = {item["course_id"]: item for item in path()["required_now"]}
    assert required["PHYS 7D"]["unlock_count"] == 0
    assert required["PHYS 7LD"]["take_with"] == ["PHYS 7D"]


def test_course_offered_in_one_quarter_is_flagged():
    required = {item["course_id"]: item for item in path()["required_now"]}
    assert required["CSE 90"]["only_quarter"] == "Winter"
    assert required["ICS 45C"]["only_quarter"] is None


def test_a_real_choice_is_compared_on_grades_and_ratings():
    choice = path()["choose"][0]
    assert choice["requirement"] == "Networks" and choice["need"] == 1 and choice["takeable_count"] == 2
    assert [alt["course_id"] for alt in choice["alternatives"]] == ["EECS 148", "CS 132"]
    assert choice["alternatives"][0]["a_rate"] == 0.6


def test_a_required_course_whose_sections_all_break_the_time_condition_is_named_apart():
    timed_out = {"reasons": ["no_compatible_section_bundle"],
                 "excluded_sections": [{"section_code": "1", "reasons": ["earliest_start_violation"]}]}
    full = {"reasons": ["no_compatible_section_bundle"], "excluded_sections": [{"section_code": "1", "reasons": ["section_cancelled"]}]}
    rows = {key: value for key, value in candidates().items() if key not in {"ICS45C", "CSE90"}}
    result = path(candidates=rows, excluded={**EXCLUDED, "ICS45C": timed_out, "CSE90": full})
    assert result["required_outside_time_condition"] == [
        {"course_id": "ICS45C", "requirement": "ICS 45C", "unlock_count": 3, "unlocks": ["ICS46", "CS141", "CS161"]}]
    blocked = [item["course_id"] for item in result["blocked_by_prerequisite"]]
    assert "ICS45C" not in blocked and "CSE90" in blocked


def test_blocked_and_not_offered_required_courses_are_reported_with_the_reason():
    result = path(candidates={key: value for key, value in candidates().items() if key != "CSE90"})
    assert {"course_id": "CSE90", "requirement": "CSE 90"} in result["not_offered_this_term"]
    blocked = {item["course_id"]: item for item in result["blocked_by_prerequisite"]}
    assert blocked["ICS46"]["missing_prerequisites"] == ["ICS 45C (min C)"]


def test_ge_gaps_come_from_recorded_courses_and_language_stays_a_question():
    done = {"ICS31", "ICS32", "ICS33", "WR39B", "WR39C", "ART1"}
    missing = {entry["category"]: entry["need"] for entry in ge_missing(done, lambda cid: CATALOGUE.get(cid))}
    assert missing == {"IB": 1, "III": 3, "IV": 2, "VI": 1, "VII": 1, "VIII": 1}
    categories = {entry["category"]: entry for entry in path()["ge"]["categories"]}
    assert categories["VI"]["status"] == "unconfirmed" and categories["VI"]["candidates"] == []
    assert path()["ge"]["exam_credits_not_counted"] == 3


def test_ge_candidates_prefer_courses_that_fill_two_gaps_and_skip_graduate_courses():
    categories = {entry["category"]: entry for entry in path()["ge"]["categories"]}
    assert [c["course_id"] for c in categories["VII"]["candidates"]] == ["SOC 1"]      # HIST 200 is graduate level
    assert [c["course_id"] for c in categories["III"]["candidates"]] == ["SOC 1", "ANTH 2"]


def test_suggested_term_takes_required_first_then_one_ge():
    plan = path()["suggested_plan"]
    chosen = [(item["course_id"], item["tier"]) for item in plan["courses"]]
    # Most-waited-on first, then the course offered in a single quarter.
    assert chosen == [("ICS 45C", "required"), ("ICS 6B", "required"), ("CSE 90", "required"), ("SOC 1", "ge")]
    assert not any(course_id in DONE for course_id, _ in chosen)
    assert plan["total_units"] == sum(item["units"] for item in plan["courses"]) == 14.0


def test_a_lab_rides_along_with_its_lecture():
    plan = path(max_courses=5, target_units=20)["suggested_plan"]
    chosen = [item["course_id"] for item in plan["courses"]]
    assert chosen.index("PHYS 7LD") == chosen.index("PHYS 7D") + 1


def test_a_course_that_clashes_is_left_out_and_named():
    rows = candidates(ICS6B=row("ICS 6B", "TuTh", "12:30", "13:50"))
    plan = path(candidates=rows)["suggested_plan"]
    assert "ICS 6B" in plan["skipped_for_time_conflict"]
    assert "ICS 6B" not in [item["course_id"] for item in plan["courses"]]


def test_a_term_plan_takes_the_path_and_a_list_of_one_kind_of_course_does_not():
    plan = empty_filter_plan()
    assert graduation.is_default_request(plan, "帮我推荐一下下学期的课") is True
    assert graduation.is_default_request(plan, "推荐几门 COMPSCI 的课") is False
    assert graduation.is_default_request({**plan, "course_scope": "ge"}, "推荐几门GE") is False
    unmet = {**plan, "hard_filters": {**plan["hard_filters"], "only_unmet_ge": True}}
    assert graduation.is_default_request(unmet, "推荐我还缺的GE") is False
    # For one plain sentence the parser returned goal=gpa_optimization on one run and
    # high_a_rate=true on another: neither decides the kind of request.
    for goal in ("gpa_optimization", "balanced", "graduation_progress", "interest_exploration"):
        assert graduation.is_default_request({**plan, "goal": goal}, "推荐给我下学期的选课") is True
    graded = {**plan, "goal": "gpa_optimization",
              "soft_preferences": {**plan["soft_preferences"], "high_a_rate": True, "professor_rating": "high"}}
    assert graduation.is_default_request(graded, "推荐一下下学期的选课") is True
    # A time condition shapes the plan; it does not turn the request into a list.
    late = {**plan, "hard_filters": {**plan["hard_filters"], "earliest_start": "10:00", "earliest_start_is_hard": True},
            "soft_preferences": {**plan["soft_preferences"], "avoid_early_classes": True}}
    assert graduation.is_default_request(late, "帮我排下学期的课，不要早八") is True


def test_card_reason_is_the_requirement_not_a_score():
    reason = {"tier": "required", "requirement": "ICS 45C", "unlock_count": 6,
              "unlocks": ["I&C SCI 46", "COMPSCI 161", "COMPSCI 141"], "only_quarter": None}
    card = normalize_recommendation_card({"course_id": "I&C SCI 45C", "term": TERM, "graduation_path": reason}, "zh")
    assert card["reason"] == "专业必修；I&C SCI 46、COMPSCI 161、COMPSCI 141 等 6 门后续必修直接或间接以它为先修。"
    english = normalize_recommendation_card(
        {"course_id": "CSE 90", "term": TERM, "graduation_path": {"tier": "required", "only_quarter": "Winter"}}, "en")
    assert english["reason"] == "Required for your major; offered only in Winter over the last two years."
    text = render_recommendation_answer(
        [{"course_id": "I&C SCI 45C", "term": TERM, "graduation_path": reason}], "zh")
    assert text.startswith("以下按毕业路径推荐") and "推荐依据：专业必修" in text


def test_card_without_a_path_reason_is_unchanged():
    card = normalize_recommendation_card({"course_id": "I&C SCI 45C", "term": TERM}, "zh")
    assert "专业必修" not in card["reason"]


def test_course_allowed_alongside_is_not_a_missing_prerequisite():
    context = {"_prefilter_state": {"profile": {"completed_course_attempts": [{"course_id": "MATH 2A", "grade": "A"}]}}}
    lab = {"course_id": "PHYSICS 7LD", "prerequisites": [],
           "prerequisite_tree": {"AND": [{"prereqType": "course", "coreq": True, "courseId": "PHYSICS 7D"}]}}
    row_ = prefilter._metadata_candidate(lab, [{"section_code": "1", "section_type": "Lab"}], context, TERM)
    assert row_["prerequisite_status"] == "met"
    strict = {**lab, "prerequisite_tree": {"AND": [{"prereqType": "course", "coreq": False, "courseId": "PHYSICS 7D"}]}}
    assert prefilter._metadata_candidate(strict, [], context, TERM)["prerequisite_status"] == "not_met"


def test_a_rejected_plan_logs_where_the_schema_broke_and_not_the_text(caplog):
    from app.recommendations.filter_plan import validate_filter_plan

    with caplog.at_level("INFO"):
        assert validate_filter_plan({"type": "PRIVATE"}, "推荐一下下学期的选课")["status"] == "fallback"
    assert "type:extra_forbidden" in caplog.text and "PRIVATE" not in caplog.text
