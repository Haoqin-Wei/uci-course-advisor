"""One question about a gap in the record after a recommendation, and saving the student's answer."""

from __future__ import annotations

import sqlite3

import pytest

from app import config
from app.academic import store
from app.academic.models import AcademicDetailsUpdate
from app.agent import graduation, prefilter, profile_followup, tools
from app.recommendations.graduation_path import build_graduation_path
from app.recommendations.presentation import render_recommendation_answer
from tests.test_academic_transcript_import import _create_and_login, _payload

TERM = "2027 Winter"
CATALOGUE = {"SWE43": {"ge": []}, "UNISTU10": {"ge": []}, "SOCIOL1": {"ge": ["III", "VII"]},
             "WRITING39B": {"ge": ["IA"]}, "SPANISH1C": {"ge": ["VI"]}}


@pytest.fixture
def student(app_client, monkeypatch):
    monkeypatch.setattr(graduation, "course_catalogue", lambda: CATALOGUE)
    user = _create_and_login(app_client)
    from app.memory import get_memory_manager
    get_memory_manager().update_profile(user["id"], {"major": "Computer Science and Engineering",
                                                     "completed_courses": ["I&C SCI 32"]})
    payload = _payload(printed_at="2026-09-18T15:23:00Z", grade="A")
    payload["declared_major"] = "UNDECLARED (UNAFFILIATED)"
    payload["transfer_credits"] = [{"institution_name": "BRIGHAM YOUNG U", "units": 4.5, "terms_through": "2025 Summer"}]
    assert app_client.post("/api/academic/transcript/import", json=payload).status_code == 200
    return user["id"]


def turn(user_id, term=TERM, language="zh"):
    return {"user_id": user_id, "term": term, "allowed_query_terms": [term], "response_language": language}


def test_each_gap_is_asked_once_in_order_and_one_per_turn(student):
    assert profile_followup.missing_items(student) == ["in_progress", "official_major", "ge_language", "transfer_courses"]
    asked = []
    for _ in range(5):
        context = turn(student)
        question = profile_followup.question_for_turn(context)
        assert profile_followup.question_for_turn(context) is question      # decided once per turn
        asked.append(question and question["item"])
    assert asked == ["in_progress", "official_major", "ge_language", "transfer_courses", None]
    # A new planning term makes "what are you taking now" worth asking again; the others stay asked.
    assert profile_followup.question_for_turn(turn(student, "2027 Spring"))["item"] == "in_progress"
    assert profile_followup.question_for_turn(turn(student, "2027 Spring")) is None


def test_questions_name_what_the_record_shows(student):
    store.mark_detail_prompt(student, "in_progress", term=TERM)
    major = profile_followup.question_for_turn(turn(student))["text"]
    assert "UNDECLARED (UNAFFILIATED)" in major and "官方专业" in major
    store.mark_detail_prompt(student, "ge_language")
    transfer = profile_followup.question_for_turn(turn(student, language="en"))["text"]
    assert "BRIGHAM YOUNG U (4.5 units)" in transfer


def test_a_declined_question_is_never_asked_again_and_the_demo_user_is_never_asked(student):
    assert profile_followup.save_statement(student, declined=["in_progress", "not-an-item"]) == {
        "ok": True, "saved": {}, "declined": ["in_progress"], "not_found": [], "rejected": ["not-an-item"],
        "still_missing": ["in_progress", "official_major", "ge_language", "transfer_courses"]}
    assert store.get_academic_details(student)["prompts"]["in_progress"]["declined"] is True
    assert profile_followup.question_for_turn(turn(student, "2027 Spring"))["item"] == "official_major"
    assert profile_followup.question_for_turn(turn("demo_001")) is None
    assert profile_followup.save_statement("demo_001", official_major="CSE")["error_code"] == "sign_in_required"


def test_saving_adds_to_the_record_and_holds_back_a_course_the_catalogue_does_not_have(student):
    first = profile_followup.save_statement(student, in_progress_courses=["swe43", "social 1"],
                                            in_progress_term="Fall 2026", official_major="cse")
    assert first["saved"] == {"in_progress_courses": ["SWE 43"], "in_progress_term": "2026 Fall", "official_major": "CSE"}
    assert first["not_found"] == ["social 1"] and first["still_missing"] == ["ge_language", "transfer_courses"]
    second = profile_followup.save_statement(student, in_progress_courses=["UNI STU 10", "SOCIOL 1"])
    assert second["saved"]["in_progress_courses"] == ["SWE 43", "UNI STU 10", "SOCIOL 1"]
    corrected = profile_followup.save_statement(student, in_progress_courses=["SWE 43"], replace=True)
    assert corrected["saved"]["in_progress_courses"] == ["SWE 43"]
    # Nothing recognisable is not a reason to wipe what is stored.
    nothing = profile_followup.save_statement(student, in_progress_courses=["FAKE 999"])
    assert nothing["ok"] is False and nothing["not_found"] == ["FAKE 999"]
    assert store.get_academic_details(student)["in_progress_courses"] == ["SWE 43"]
    # The answered questions are closed without having been asked.
    assert profile_followup.question_for_turn(turn(student))["item"] == "ge_language"


def test_no_to_the_language_question_makes_it_a_real_gap_and_yes_takes_it_back(student):
    assert profile_followup.save_statement(student, ge_not_met=["VI"])["saved"] == {"ge_not_met": ["VI"]}
    assert "ge_language" not in profile_followup.missing_items(student)
    assert store.get_academic_details(student)["prompts"]["ge_language"]["asked_at"]
    details = store.update_academic_details(student, AcademicDetailsUpdate(ge_confirmed=["VI", "VII"]))
    assert (details["ge_confirmed"], details["ge_not_met"]) == (["VI", "VII"], [])
    details = store.update_academic_details(student, AcademicDetailsUpdate(ge_not_met=["GE VI"]))
    assert (details["ge_confirmed"], details["ge_not_met"]) == (["VII"], ["VI"])


def test_language_stated_as_open_is_planned_like_any_other_missing_category():
    rows = {"SPANISH1C": {"course_id": "SPANISH 1C", "ge_categories": ["VI"], "sections": [
        {"section_code": "1", "section_type": "Lec", "days": "MWF", "start_time": "10:00", "end_time": "10:50"}]}}
    info = {"SPANISH1C": {"title": "Spanish", "units": 4.0, "numeric": 1, "prerequisite_tree": {},
                          "terms_offered": [], "ge": ["VI"]}}.get
    args = dict(requirements=[], completed=["MATH 2A"], candidates=rows, excluded={}, term=TERM, course_info=info)

    def language(basis):
        path = build_graduation_path(ge_basis=basis, **args)
        return next(entry for entry in path["ge"]["categories"] if entry["category"] == "VI")

    unknown = language({"courses": [], "exam_credits": [], "confirmed": []})
    assert (unknown["status"], unknown["candidates"]) == ("unconfirmed", [])
    stated = language({"courses": [], "exam_credits": [], "confirmed": [], "not_met": ["VI"]})
    assert stated["status"] != "unconfirmed" and [c["course_id"] for c in stated["candidates"]] == ["SPANISH 1C"]


def test_the_tool_saves_through_the_same_call_and_rebuilds_this_turns_candidates(student, monkeypatch):
    monkeypatch.setattr(config, "dynamic_prefilter_enabled", lambda: True)
    rebuilt = []

    def build(context):
        rebuilt.append(list(context["_prefilter_state"]["profile"]["in_progress_courses"]))
        context["_prefilter_state"]["graduation_path"] = {"required_now": [], "reasons": {"X": {}}}

    monkeypatch.setattr(prefilter, "_build_pools", build)
    context = {**turn(student), "_prefilter_state": {
        "enabled": True, "plan": {}, "plan_status": "validated", "unresolved_constraints": [],
        "profile": {"in_progress_courses": []}, "pools": {("stale",): {}}, "candidates": {(TERM, "SWE 43"): {}},
        "excluded": {}, "graduation_path": {"required_now": ["stale"]}}}
    result = tools.dispatch("update_academic_details", {"in_progress_courses": ["SWE 43"]}, context=context)
    assert result["ok"] is True and result["saved"] == {"in_progress_courses": ["SWE 43"]}
    assert rebuilt == [["SWE 43", "IN4MATX 43"]]                        # the renamed course's old ID too
    assert context["_prefilter_state"]["pools"] == {} and context["_prefilter_state"]["candidates"] == {}
    assert result["graduation_path"] == {"required_now": []} and "out of date" in result["recommendation_context"]
    # Outside a recommendation turn there is nothing to rebuild.
    plain = tools.dispatch("update_academic_details", {"official_major": "CSE"}, context=turn(student))
    assert plain["ok"] is True and "recommendation_context" not in plain
    assert tools.humanize_tool_call("update_academic_details", {}, "zh") == "保存学业档案"


def test_the_closing_note_reports_what_was_saved_then_asks_only_after_a_complete_answer(student):
    context = turn(student)
    tools.dispatch("update_academic_details", {"in_progress_courses": ["SWE 43", "social 1"]}, context=context)
    quiet = profile_followup.closing_note(context, ask=False)
    assert "已记录：在读课程 SWE 43" in quiet and "social 1" in quiet and "另外想确认" not in quiet
    asked = profile_followup.closing_note(context, ask=True)
    assert asked.startswith(quiet) and "官方专业" in asked
    assert profile_followup.closing_note(turn("demo_001"), ask=True) is None

    answer = render_recommendation_answer([], "zh", follow_up=asked)
    assert answer.endswith(asked) and answer != asked
    assert render_recommendation_answer([], "zh", follow_up=None) == render_recommendation_answer([], "zh")


def test_a_failed_turn_does_not_carry_the_question(monkeypatch):
    from app.routers import chat
    event = {"follow_up": "另外想确认一件事：……"}
    assert chat._controlled_agent_reply([], "zh", event).endswith(event["follow_up"])
    assert event["follow_up"] not in chat._controlled_agent_reply([], "zh", event, failed=True)


def test_a_details_table_created_before_these_columns_gains_them(isolated_test_environment):
    path = config.academic_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE student_academic_details (user_id TEXT PRIMARY KEY, official_major TEXT,
            official_major_set_at TEXT, in_progress_term TEXT, in_progress_courses_json TEXT NOT NULL DEFAULT '[]',
            ge_confirmed_json TEXT NOT NULL DEFAULT '[]', transfer_courses_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO student_academic_details (user_id, in_progress_courses_json, updated_at) "
                     "VALUES ('u1', ?, '2026-11-19')", ('["SWE 43"]',))
    details = store.get_academic_details("u1")
    assert (details["in_progress_courses"], details["ge_not_met"], details["prompts"]) == (["SWE 43"], [], {})
    assert store.mark_detail_prompt("u1", "official_major", term=TERM)["official_major"]["term"] == TERM


def test_profile_endpoint_lists_what_is_still_missing(app_client, student):
    body = app_client.get("/api/academic/profile").json()
    assert body["details_missing"] == ["in_progress", "official_major", "ge_language", "transfer_courses"]
    assert body["details"]["prompts"] == {} and body["details"]["ge_not_met"] == []


def test_the_controlled_answer_ends_with_the_question_and_the_final_event_carries_it(student):
    from app.agent import loop

    empty = loop.controlled_recommendation_result(turn(student))
    assert "follow_up" not in empty and "另外想确认" not in empty["text"]      # nothing recommended: nothing asked
    assert store.get_academic_details(student)["prompts"] == {}

    context = {**turn(student), "_controlled_recommendation_cards": [{"course_id": "I&C SCI 45C", "term": TERM}]}
    result = loop.controlled_recommendation_result(context)
    assert result["cards"] and result["incomplete"] is False
    assert result["follow_up"].startswith("另外想确认一件事：你这学期正在上哪些课")
    assert result["text"].endswith(result["follow_up"])
    assert loop.controlled_recommendation_result(context)["follow_up"] == result["follow_up"]   # same turn, same question
    next_turn = {**turn(student), "_controlled_recommendation_cards": context["_controlled_recommendation_cards"]}
    assert "官方专业" in loop.controlled_recommendation_result(next_turn)["follow_up"]
