"""The model picks inside the degree-path tray; the server checks the picks."""

from __future__ import annotations

from app import config
from app.agent import graduation, prefilter
from app.recommendations.plan_check import review_picks
from app.recommendations.presentation import plan_review_text, render_recommendation_answer

TERM = "2027 Winter"


def row(course_id, days="MWF", start="10:00", end="10:50", kind="Lec"):
    return {"course_id": course_id, "sections": [
        {"section_code": course_id[-3:], "section_type": kind, "days": days, "start_time": start, "end_time": end}]}


ROWS = {"ICS46": row("ICS 46", "TuTh", "09:30", "10:50"), "PHYSICS7D": row("PHYSICS 7D"),
        "PHYSICS7LD": row("PHYSICS 7LD", "W", "14:00", "16:50", "Lab"), "EECS31L": row("EECS 31L", "MWF", "13:00", "13:50"),
        "ICS6B": row("ICS 6B", "MWF", "10:00", "10:50"), "SOCSCI4A": row("SOC SCI 4A", "TuTh", "14:00", "15:20"),
        "HISTORY10": row("HISTORY 10", "TuTh", "11:00", "12:20")}
PATH = {
    "term": TERM,
    "required_now": [
        {"course_id": "ICS 46", "requirement": "ICS 46", "unlock_count": 9, "unlocks": ["CS 141", "CS 142A", "CS 143A"], "take_with": []},
        {"course_id": "PHYSICS 7D", "requirement": "Physics", "unlock_count": 3, "unlocks": ["EECS 70A"], "take_with": []},
        {"course_id": "EECS 31L", "requirement": "EECS 31L", "unlock_count": 3, "unlocks": [], "take_with": []},
        {"course_id": "ICS 6B", "requirement": "ICS 6B", "unlock_count": 1, "unlocks": [], "take_with": []},
        {"course_id": "PHYSICS 7LD", "requirement": "Physics", "unlock_count": 0, "unlocks": [], "take_with": ["PHYSICS 7D"]}],
    "suggested_plan": {"courses": [{"course_id": "ICS 46", "tier": "required"}, {"course_id": "PHYSICS 7D", "tier": "required"},
                                   {"course_id": "PHYSICS 7LD", "tier": "required"}, {"course_id": "EECS 31L", "tier": "required"},
                                   {"course_id": "SOC SCI 4A", "tier": "ge"}]},
    "required_outside_time_condition": [],
    "reasons": {"ICS 46": {"tier": "required"}, "PHYSICS 7D": {"tier": "required"}, "PHYSICS 7LD": {"tier": "required"},
                "EECS 31L": {"tier": "required"}, "ICS 6B": {"tier": "required"},
                "SOC SCI 4A": {"tier": "ge"}, "HISTORY 10": {"tier": "ge"}},
}
PLAN = ["ICS 46", "PHYSICS 7D", "PHYSICS 7LD", "EECS 31L", "SOC SCI 4A"]


def test_picks_that_follow_the_tray_stand_and_keep_the_models_paragraph():
    review = review_picks(PATH, ["SOC SCI 4A", *PLAN[:4]], ROWS, note="  先拿下门槛课，\n再补一门 GE。 ")
    assert review["courses"] == PLAN                                   # required first, whatever order was given
    assert review["changed"] is False and review["note"] == "先拿下门槛课， 再补一门 GE。"
    assert review["left_out_required"] == []


def test_swapping_inside_a_tier_is_the_models_call():
    review = review_picks(PATH, [*PLAN[:4], "HISTORY 10"], ROWS, note="换一门历史")
    assert review["courses"][-1] == "HISTORY 10" and review["changed"] is False and review["note"] == "换一门历史"


def test_a_course_outside_the_tray_is_removed_replaced_from_the_plan_and_the_paragraph_dropped():
    review = review_picks(PATH, ["EARTHSS 3", "PHYSICS 7D", "PHYSICS 7LD", "EECS 31L", "SOC SCI 4A"], ROWS, note="轻松一点")
    assert review["removed_outside_tray"] == ["EARTHSS 3"] and review["substituted"] == ["ICS 46"]
    assert review["courses"] == PLAN                                   # the substitute takes its place in the plan's order
    assert review["changed"] is True and review["note"] is None


def test_the_top_required_course_may_be_left_out_and_is_not_put_back():
    review = review_picks(PATH, ["EARTHSS 3", *PLAN[1:]], ROWS,
                          left_out=[{"course_id": "ics46", "reason": "45C 成绩还没出，想等确认后再修"}])
    assert "ICS 46" not in review["courses"] and review["substituted"] == []
    assert review["left_out_required"] == [{
        "course_id": "ICS 46", "requirement": "ICS 46", "unlock_count": 9,
        "unlocks": ["CS 141", "CS 142A", "CS 143A"], "reason": "45C 成绩还没出，想等确认后再修"}]
    # Left out without a word: the server still says what it delays.
    silent = review_picks(PATH, PLAN[1:], ROWS, note="少修一门")
    assert silent["changed"] is False and silent["note"] == "少修一门"
    assert silent["left_out_required"][0]["reason"] is None


def test_a_lecture_and_its_lab_go_together_whichever_was_picked():
    assert review_picks(PATH, ["PHYSICS 7D", "EECS 31L"], ROWS)["added_companions"] == ["PHYSICS 7LD"]
    lab_only = review_picks(PATH, ["PHYSICS 7LD", "EECS 31L"], ROWS)
    assert lab_only["added_companions"] == ["PHYSICS 7D"] and set(lab_only["courses"]) == {"PHYSICS 7D", "PHYSICS 7LD", "EECS 31L"}


def test_a_lecture_clash_removes_the_later_pick():
    review = review_picks(PATH, ["PHYSICS 7D", "ICS 6B"], ROWS)       # both MWF 10:00
    assert review["removed_for_time_conflict"] == ["ICS 6B"] and "ICS 6B" not in review["courses"]
    assert review["substituted"] == ["ICS 46"]                         # the freed slot goes to the plan's first course


def test_the_answer_states_every_change_as_a_fact_and_escapes_the_models_words():
    review = review_picks(PATH, ["EARTHSS 3", "PHYSICS 7D", "EECS 31L"], ROWS,
                          left_out=[{"course_id": "ICS 46", "reason": "想先**轻松**一点"}])
    review["required_outside_time_condition"] = [{"course_id": "CSE 90"}]
    text = plan_review_text(review, "zh")
    assert "这次没有排入专业必修 **ICS 46**；会推迟 CS 141、CS 142A、CS 143A 等 9 门后续必修。原因：想先\\*\\*轻松\\*\\*一点" in text
    assert "已去掉不在你本学期可选范围内的课程：**EARTHSS 3**。" in text
    assert "已补上必须同修的课程：**PHYSICS 7LD**。" in text
    assert "只有不符合你时间条件的班次，没有排入：**CSE 90**。" in text
    english = plan_review_text(review, "en")
    assert "Required course **ICS 46** is not in this plan; this delays CS 141, CS 142A, CS 143A and 6 more required courses." in english
    assert plan_review_text(None) is None and plan_review_text({"courses": ["X"], "changed": False}) is None

    answer = render_recommendation_answer([{"course_id": "PHYSICS 7D", "term": TERM}], "zh", lead=text)
    assert answer.index(text) < answer.index("**PHYSICS 7D**")           # above the cards' lines
    assert text not in render_recommendation_answer([], "zh", lead=text)  # nothing staged: nothing to introduce


def _context(monkeypatch, path=PATH):
    monkeypatch.setattr(config, "dynamic_prefilter_enabled", lambda: True)
    monkeypatch.setattr(prefilter, "ensure_candidate", lambda context, course_id, term=None: ({}, None))
    return {"term": TERM, "allowed_query_terms": [TERM], "_prefilter_state": {
        "enabled": True, "plan": {"hard_filters": {}}, "plan_status": "validated", "unresolved_constraints": [],
        "candidates": {(TERM, value["course_id"]): value for value in ROWS.values()}, "excluded": {},
        **({"graduation_path": path} if path else {})}}


def test_with_a_tray_the_search_is_refused_and_the_proposal_is_checked(monkeypatch):
    context = _context(monkeypatch)
    _, refused = prefilter.guard_call("search_courses", {"term": TERM, "department": "COMPSCI"}, context)
    assert refused["error_code"] == "tray_is_the_candidate_set"

    items = [{"course_id": "EARTHSS 3", "category": "elective", "reason": "easy"},
             {"course_id": "PHYSICS 7D", "category": "core", "reason": "physics", "priority": "high"}]
    args, error = prefilter.guard_call("propose_recommendation", {"items": items, "term": TERM, "note": "n"}, context)
    assert error is None
    assert [item["course_id"] for item in args["items"]] == ["ICS 46", "PHYSICS 7D", "PHYSICS 7LD"]
    assert args["items"][1] == items[1]                                 # the model's own card fields survive
    assert args["items"][2] == {"course_id": "PHYSICS 7LD", "category": "core", "reason": ""}
    assert context["_plan_review"]["removed_outside_tray"] == ["EARTHSS 3"] and context["_plan_review"]["note"] is None


def test_without_a_tray_nothing_changes(monkeypatch):
    context = _context(monkeypatch, path=None)
    assert prefilter.guard_call("search_courses", {"term": TERM}, context)[1] is None
    items = [{"course_id": "EARTHSS 3", "category": "elective", "reason": "easy"}]
    args, error = prefilter.guard_call("propose_recommendation", {"items": items, "term": TERM}, context)
    assert error is None and args["items"] == items and "_plan_review" not in context
    # A tray built for another term does not govern this one.
    other = _context(monkeypatch, path={**PATH, "term": "2027 Spring"})
    assert graduation.review_proposal(other, {"items": items}, TERM) == ({"items": items}, None)


def test_a_complete_term_plan_says_how_to_ask_for_a_list_instead(monkeypatch):
    from app.agent import loop

    context = {"term": TERM, "allowed_query_terms": [TERM], "response_language": "zh", "user_id": "demo_001",
               "_controlled_recommendation_cards": [{"course_id": "PHYSICS 7D", "term": TERM}],
               "_plan_review": review_picks(PATH, PLAN, ROWS, note="先拿下门槛课。")}
    result = loop.controlled_recommendation_result(context)
    assert result["lead"] == "先拿下门槛课。" and result["text"].index("先拿下门槛课。") < result["text"].index("**PHYSICS 7D**")
    assert result["follow_up"].startswith("这是按毕业进度排的学期计划") and result["text"].endswith(result["follow_up"])
    from app.routers import chat
    assert chat._controlled_agent_reply(result["cards"], "zh", result) == result["text"]
    plain = loop.controlled_recommendation_result({key: value for key, value in context.items() if key != "_plan_review"})
    assert "follow_up" not in plain and "lead" not in plain


def test_in_a_follow_up_a_removed_pick_leaves_its_slot_for_the_model_to_fill():
    review = review_picks(PATH, ["EARTHSS 3", "PHYSICS 7D", "PHYSICS 7LD", "EECS 31L"], ROWS, substitute=False)
    assert review["removed_outside_tray"] == ["EARTHSS 3"] and review["substituted"] == []
    assert review["courses"] == ["PHYSICS 7D", "PHYSICS 7LD", "EECS 31L"]       # SOC SCI 4A is not put back
    assert review_picks(PATH, ["EARTHSS 3"], ROWS, substitute=False)["courses"] == []


def test_a_pick_that_got_no_card_and_a_full_required_course_are_both_said_out_loud():
    review = {"courses": ["ICS 46", "HISTORY 10"], "changed": False,
              "not_staged": [{"course_id": "HISTORY 10", "code": "full"}, {"course_id": "X 1", "code": "other"}]}
    text = plan_review_text(review, "zh")
    assert "**HISTORY 10** 没有排入：实时课表显示它本学期的班次都已满。" in text
    assert "**X 1** 没有排入：出卡片时没有找到可以选的班次。" in text
    assert "**HISTORY 10** is not in the plan: the live timetable shows all of its sections full this term." in plan_review_text(review, "en")
    full = render_recommendation_answer([{"course_id": "ENGR 190W", "term": TERM, "all_sections_full": True}], "zh")
    assert "本学期所有班次都已满：需要排等位名单，或联系开课院系。" in full
    assert "已满" not in render_recommendation_answer([{"course_id": "ENGR 190W", "term": TERM}], "zh")


def test_only_full_sections_let_a_tray_course_keep_its_card_marked_full():
    from app.agent.tools import _blocked_by_full_sections
    lec, dis = {"section_code": "1", "section_type": "Lec"}, {"section_code": "2", "section_type": "Dis"}
    assert _blocked_by_full_sections([lec, dis], []) is True
    assert _blocked_by_full_sections([lec, dis], [lec]) is True              # every discussion is full
    assert _blocked_by_full_sections([lec, dis], [dis]) is True              # every lecture is full
    assert _blocked_by_full_sections([lec, dis], [lec, dis]) is False
    assert _blocked_by_full_sections([lec], [lec]) is False


def _lec(code, days, start, end, num="A"):
    return {"section_code": code, "section_type": "Lec", "section_num": num, "days": days, "start_time": start, "end_time": end}


def test_a_card_takes_the_best_lecture_that_is_clear_of_the_other_cards():
    from app.agent.tools import _lecture_beside, _lecture_slot
    best, same_prof, online = _lec("3", "TuTh", "12:30", "13:50", "C"), _lec("4", "TuTh", "14:00", "15:20", "D"), {
        "section_code": "1", "section_type": "Lec", "section_num": "A"}
    taken = [(_lecture_slot(_lec("9", "TuTh", "12:30", "13:50")), "I&C SCI 6B")]
    assert _lecture_beside([best, same_prof, online], [], []) == (best, None)
    assert _lecture_beside([best, same_prof, online], taken, []) == (same_prof, "I&C SCI 6B")
    assert _lecture_beside([online, best], taken, []) == (online, None)       # no published time: nothing against it
    assert _lecture_beside([best], taken, []) == (None, "I&C SCI 6B")


def test_a_card_leaves_the_only_lecture_hour_of_a_later_course_free_when_it_can():
    from app.agent.tools import _lecture_beside, _lecture_slot
    best, other = _lec("3", "MWF", "10:00", "10:50"), _lec("4", "MWF", "11:00", "11:50", "B")
    later = [(_lecture_slot(_lec("9", "MWF", "10:00", "10:50")), "EECS 31L")]
    assert _lecture_beside([best, other], [], later) == (other, "EECS 31L")
    assert _lecture_beside([best], [], later) == (best, None)                 # nowhere else to go: first come


def test_only_lectures_are_compared_and_discussions_are_left_to_the_student():
    from app.agent.tools import _single_lecture_slots
    rows = {(TERM, "ICS 6B"): {"course_id": "ICS 6B", "sections": [
                _lec("1", "TuTh", "12:30", "13:50"),
                {"section_code": "2", "section_type": "Dis", "days": "MW", "start_time": "17:00", "end_time": "17:50"}]},
            (TERM, "SOC SCI 172AW"): {"course_id": "SOC SCI 172AW", "sections": [
                _lec("3", "TuTh", "12:30", "13:50", "C"), _lec("4", "TuTh", "14:00", "15:20", "D")]}}
    context = {"_prefilter_state": {"graduation_path": {"term": TERM}, "candidates": rows}}
    found = _single_lecture_slots(context, [{"course_id": "ICS 6B"}, {"course_id": "SOC SCI 172AW"}], TERM)
    assert [course for _, course in found] == ["ICS 6B"]
    assert _single_lecture_slots({}, [{"course_id": "ICS 6B"}], TERM) == []


def test_a_moved_lecture_and_a_pick_with_no_free_lecture_are_said_out_loud():
    card = {"course_id": "SOC SCI 172AW", "term": TERM, "primary_code": "70380",
            "lecture_moved": {"instead_of": "Lec C", "avoids": "I&C SCI 6B"},
            "section_groups": [{"letter": "D", "secondaries": [], "primary": {
                "code": "70380", "num": "D", "type": "Lec", "days": "TuTh", "time": "TuTh 2:00- 3:20p"}}]}
    text = render_recommendation_answer([card], "zh")
    assert "为避开 **I&C SCI 6B** 的讲座时间，这里没有选排名更靠前的 Lec C。" in text
    assert "为避开" not in render_recommendation_answer([{**card, "lecture_moved": None}], "zh")
    review = {"courses": ["ICS 46"], "changed": False,
              "not_staged": [{"course_id": "ICS 6B", "code": "time_conflict", "clashes_with": "EECS 31L"}]}
    assert "**ICS 6B** 没有排入：它有名额的讲座都和已排课程的讲座时间重叠（EECS 31L）。" in plan_review_text(review, "zh")
