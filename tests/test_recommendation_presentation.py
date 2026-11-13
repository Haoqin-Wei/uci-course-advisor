"""Controlled recommendation evidence, with small public-record regressions."""
from copy import deepcopy
import json

import pytest

from app.recommendations.presentation import (
    normalize_ge_evidence,
    normalize_recommendation_card,
    render_recommendation_answer,
)


def _history(course="SOCIOL1", name="CARIAN, E.", scope="course_instructor"):
    counts = {"A": 643, "B": 75, "C": 8, "D": 3, "F": 4, "P": 6, "NP": 1}
    return {"found": True, "status": "ok", "source": "zotistics", "source_url": "https://zotistics.com/",
            "scope": scope, "instructor": name, "resolved_instructor_name": name,
            "query": {"course_id": course, "instructor_name": name, "department": "SOCIOL"},
            "coverage": {"last_grade_update": "2023 Spring"},
            "grades": {"scope": scope, "course_id": course, "instructor_name": name,
                       "letter_counts": counts, "graded": 733, "samples": 740,
                       "avg_gpa": 3.842, "source_reported_gpa": 3.795,
                       "site_percentages": {"A": 86.9}, "class_count": 2,
                       "terms": ["2022 Fall", "2023 Winter"],
                       "term_range": {"first": "2022 Fall", "last": "2023 Winter"}}}


def _card():
    # The captured C1 record: source statistics are kept distinct from scores.
    ranking = {"final_score": 83.0194146624167, "score_version": "sample_adjusted_weighted_v1",
               "effective_weights": {"grade_a_rate": 0.6, "professor_rating": 0.4},
               "features": {
                   "grade_a_rate": {"valid": True, "value": 643 / 733, "sample_count": 733,
                                    "scope": "course_instructor", "course_id": "SOCIOL 1", "instructor_name": "CARIAN, E."},
                   "professor_rating": {"valid": True, "value": 5, "sample_count": 19,
                                        "scope": "instructor", "instructor_name": "CARIAN, E.", "source": "db"}}}
    primary = {"code": "69040", "num": "C", "type": "Lec", "instructors": ["CARIAN, E."],
               "days": "MWF", "time": "MWF 9:00-9:50", "grade_history": [_history()], "ranking": ranking}
    return {"course_id": "SOCIOL 1", "term": "2026 Fall", "primary_code": "69040", "reason": "比 CS 高年级课风险低得多",
            "ge_category": None, "ranking": ranking, "section_groups": [{"primary": primary, "secondaries": []}],
            "requires_secondary": True,
            "prefilter": {"enabled": True, "status": "needs_verification", "warnings": [
                {"code": "secondary_pairing_unknown", "message": "untrusted freeform marker"}]}}


def _verified_ge(card, labels=("GE-II", "GE-VII")):
    # Synthetic trusted annual envelope for presentation-only combinations.
    # Real official HTML/category extraction is covered in test_ge_evidence.
    card.update(ge_categories=list(labels), ge_labels_verified=True,
                ge_provenance={"loader": "uci_relational", "source_file": "data/uci", "is_historical_proxy": False,
                               "source": "uci_catalogue", "status": "verified", "catalogue_year": "2026-27",
                               "source_url": "https://catalogue.uci.edu/informationforadmittedstudents/requirementsforabachelorsdegree/",
                               "retrieved_at": "2026-10-20T00:00:00Z", "parser_version": 1, "content_sha256": "0" * 64},
                ge_evidence_course_id=card["course_id"], ge_evidence_term=card["term"])
    return card


@pytest.mark.parametrize("language", ["zh", "en"])
def test_c1_controlled_output_preserves_source_statistics_without_risk_claims(language):
    card = _card()
    card["model_reason"] = "Historical A rate above normal; much lower risk than upper-division CS"
    card["section_groups"][0]["primary"]["model_reason"] = "hidden model claim"
    before = deepcopy(card)
    normalized = normalize_recommendation_card(card, language)
    answer = render_recommendation_answer([normalized], language)
    assert card == before
    assert "model_reason" not in json.dumps(normalized)
    assert "比 CS 高年级课风险低得多" not in answer + normalized["reason"]
    assert "above normal" not in answer
    assert "much lower risk" not in answer
    for number in ["87.7%", "643/733", "740", "86.9%", "3.842", "3.795", "83.0/100"]:
        assert number in answer
    assert normalized["ranking"] == before["ranking"]
    assert normalized["section_groups"][0]["primary"]["grade_history"] == before["section_groups"][0]["primary"]["grade_history"]
    assert ("含 P/NP 分母" if language == "zh" else "denominator includes P/NP") in answer
    assert ("无 ±" if language == "zh" else "no +/−") in answer
    assert ("不是" if language == "zh" else "not specific to this course") in answer


def test_actual_primary_not_first_alternative_determines_all_displayed_facts():
    card = _card()
    alternative = deepcopy(card["section_groups"][0])
    alternative["primary"].update(code="69000", num="A", instructors=["OTHER, A."], time="11:00-11:50")
    card["section_groups"].insert(0, alternative)
    answer = render_recommendation_answer([card])
    assert "69040" in answer and "CARIAN, E." in answer
    assert "69000" not in answer and "OTHER, A." not in answer and "11:00" not in answer


def test_c2_ge_ii_reason_cannot_override_unknown_structured_evidence():
    card = _card()
    card.update(course_id="PHILOS 3", reason="GE II，科技与社会主题", ge_category=None)
    card["prefilter"]["warnings"].append({"code": "ge_labels_unknown"})
    normalized = normalize_recommendation_card(card)
    assert normalized["ge_evidence"]["status"] == "unknown"
    assert normalized["ge_evidence"]["is_ge"] is None
    assert normalized["ge_category"] is None and normalized["ge_categories"] == []
    answer = render_recommendation_answer([normalized], plan={"course_scope": "ge"})
    assert "GE II" not in answer + normalized["reason"]
    assert "类别已确认 0 门，待核实 1 门" in answer


@pytest.mark.parametrize("labels", [("GE-II", "GE-VII"), ("GE-Va", "GE-Vb"), ()])
def test_verified_multi_label_and_verified_empty_list_do_not_depend_on_singular_label(labels):
    card = _verified_ge(_card(), labels)
    assert card["ge_category"] is None
    evidence = normalize_ge_evidence(card)
    assert evidence["status"] == "verified"
    assert evidence["categories"] == [label[3:].upper() for label in labels]
    assert evidence["is_ge"] is bool(labels)
    assert evidence["source"]["loader"] == "uci_relational"
    normalized = normalize_recommendation_card(card)
    assert normalize_recommendation_card(normalized) == normalized
    assert normalized["presentation"]["language"] == "zh"
    if labels:
        answer = render_recommendation_answer([normalized], plan={"course_scope": "ge"})
        assert "GE 标签来源：[UCI 官方课程目录 2026-27]" in answer
        assert "uci_relational" not in answer and "data/uci" not in answer


@pytest.mark.parametrize("change", [
    {"ge_labels_verified": False}, {"ge_labels_verified": "true"},
    {"ge_categories": ["GE-IX"]}, {"ge_categories": None}, {"ge_provenance": {}},
    {"ge_evidence_course_id": "ECON 13"}, {"ge_evidence_term": "2025 Fall"},
    {"ge_provenance": {"loader": "uci_relational", "is_historical_proxy": True}},
    {"ge_provenance": {"loader": "uci_relational", "source_term": "2026_Spring", "target_term": "2026_Fall"}},
    {"ge_provenance": {"loader": "uci_relational", "target_term": "2027_Winter"}},
])
def test_incomplete_or_mismatched_ge_evidence_remains_unknown(change):
    card = _verified_ge(_card())
    card.update(change)
    evidence = normalize_ge_evidence(card)
    assert evidence["status"] == "unknown"
    assert evidence["is_ge"] is None and evidence["categories"] == []


def test_null_and_missing_ge_metadata_never_mean_confirmed_non_ge():
    card = _card()
    assert normalize_ge_evidence(card)["is_ge"] is None
    assert normalize_ge_evidence({**card, "ge_categories": []})["is_ge"] is None
    assert normalize_ge_evidence(_verified_ge(card, ()))["is_ge"] is False


def test_course_tags_and_personal_degree_contribution_are_independent():
    card = _verified_ge(_card())
    evidence = normalize_ge_evidence(card)
    assert evidence["status"] == "verified"
    assert evidence["personal_contribution"]["status"] == "unknown"
    card["personal_ge_audit"] = {"verified": True, "authorized": True, "source": "registrar_degree_audit",
                                 "unmet_categories": ["II", "VII"], "projected_categories": ["II"]}
    personal = normalize_ge_evidence(card)["personal_contribution"]
    assert personal["remaining_categories"] == ["VII"]
    assert personal["projected_categories"] == ["II"]
    card["personal_ge_audit"]["authorized"] = False
    assert normalize_ge_evidence(card)["personal_contribution"]["status"] == "unknown"


@pytest.mark.parametrize("language", ["zh", "en"])
def test_cross_course_instructor_fallback_is_separate_not_a_same_course_gap(language):
    card = _card()
    wide = _history(None, scope="instructor")
    wide.update(fallback_from_scope="course_instructor", requested_course_id="SOCIOL1",
                pair_query={"course_id": "SOCIOL1", "instructor_name": "CARIAN, E."})
    wide["query"].update(course_id=None, department=None)
    card["section_groups"][0]["primary"]["grade_history"] = [wide]
    course = _history(scope="course", name=None)
    card["zotistics_grades"] = course
    answer = render_recommendation_answer([card], language)
    assert ("教授跨课程历史汇总" if language == "zh" else "Instructor-wide, cross-course history") in answer
    assert ("非本课成绩" if language == "zh" else "not this course's grades") in answer
    assert ("课程整体历史" if language == "zh" else "Course-wide history") in answer
    assert "百分点" not in answer and "percentage-point" not in answer


@pytest.mark.parametrize("mismatch", ["course", "instructor", "ta"])
def test_wrong_course_wrong_instructor_and_discussion_only_grade_records_are_not_displayed(mismatch):
    card = _card()
    primary = card["section_groups"][0]["primary"]
    if mismatch == "course":
        primary["grade_history"][0]["grades"]["course_id"] = "ECON13"
    elif mismatch == "instructor":
        primary["grade_history"][0]["instructor"] = "OTHER, X."
    else:
        primary["type"] = "Dis"
    assert normalize_recommendation_card(card)["presentation"]["grade_evidence"] == []


def test_real_zero_score_and_gpa_are_preserved_while_nan_is_rejected():
    card = _card()
    rank = card["section_groups"][0]["primary"]["ranking"]
    rank["final_score"] = 0
    history = card["section_groups"][0]["primary"]["grade_history"][0]
    history["grades"].update(avg_gpa=0, source_reported_gpa=0)
    answer = render_recommendation_answer([card])
    assert "0.0/100" in answer and "估算 GPA 0.000" in answer and "原站报告 GPA 0.000" in answer
    history["grades"]["source_reported_gpa"] = float("nan")
    assert "原站报告 GPA" not in render_recommendation_answer([card])


def test_pnp_only_is_not_a_zero_a_rate_or_nan_gpa():
    card = _card()
    history = card["section_groups"][0]["primary"]["grade_history"][0]
    history["grades"].update(letter_counts={"A": 0, "B": 0, "C": 0, "D": 0, "F": 0, "P": 8, "NP": 2},
                             graded=0, samples=10, avg_gpa=None, source_reported_gpa=float("nan"))
    answer = render_recommendation_answer([card])
    assert "没有 A–F 记录，A 率未知" in answer
    assert "nan" not in answer.lower() and "原站报告 GPA" not in answer


def test_class_records_and_distinct_terms_are_never_interchanged():
    card = _card()
    history = card["section_groups"][0]["primary"]["grade_history"][0]
    history["grades"].update(class_count=32, terms=["2022Fall", "2022 Fall", "2023 Winter"])
    answer = render_recommendation_answer([card])
    assert "32 个班次记录" in answer and "2 个不同学期" in answer


@pytest.mark.parametrize("language", ["zh", "en"])
def test_gate_and_incomplete_status_do_not_echo_error_or_claim_success(language):
    raw_error = "provider error with model claim: easier than CS and GE II confirmed"
    blocked = render_recommendation_answer([_card()], language,
                                           gate={"enabled": True, "ready": False, "reasons": ["unknown"]})
    partial = render_recommendation_answer([_card()], language, incomplete=True, failure_reason=raw_error)
    assert "SOCIOL" not in blocked
    assert raw_error not in partial
    assert ("尚未完成" if language == "zh" else "incomplete") in partial
    assert ("尚未完成" if language == "zh" else "incomplete") in render_recommendation_answer([], language, incomplete=True)


def test_legacy_boolean_pairing_never_becomes_an_official_registration_assertion():
    answer = render_recommendation_answer([_card()])
    assert "配对要求待核实" in answer
    assert "WebReg 拒绝" not in answer and "必须同时" not in answer


def test_rmp_only_score_explicitly_reports_actual_effective_weights():
    card = _card()
    rank = card["section_groups"][0]["primary"]["ranking"]
    rank["effective_weights"] = {"grade_a_rate": 0, "professor_rating": 1}
    rank["features"]["grade_a_rate"]["valid"] = False
    rank["final_score"] = 69.56521739130434
    answer = render_recommendation_answer([card], "en")
    assert "69.6/100" in answer and "A rate 0%, RMP 100%" in answer


def test_normalization_is_idempotent_for_unknown_cards_and_overwrites_forged_presentation():
    card = _card()
    card["presentation"] = {"version": 1, "reason": "GE II confirmed, much safer than CS"}
    normalized = normalize_recommendation_card(card, "en")
    assert normalize_recommendation_card(normalized, "en") == normalized
    assert "much safer" not in json.dumps(normalized)


@pytest.mark.parametrize("goal", ["gpa_optimization", "interest_exploration", "balanced"])
def test_non_ge_intents_do_not_become_a_ge_progress_workflow(goal):
    answer = render_recommendation_answer([_card()], plan={"goal": goal, "course_scope": "any"})
    assert "类别已确认" not in answer and "个人毕业要求抵扣" not in answer
    assert "CS 高年级" not in answer and "学业审计" not in answer


def _pairing_card(selected=True):
    card = _card()
    card["pairing"] = {"version": 1, "requirement": "required", "required_types": ["Dis"],
                       "allowed_bundles": [{"primary_code": "69040", "secondary_codes": ["69041"]}],
                       "evidence": {"status": "verified", "source": "uci_websoc", "course_id": "SOCIOL1", "term": "2026 Fall"}}
    card["section_groups"][0]["secondaries"] = [{"code": "69041", "type": "Dis", "num": "C1"}]
    card["selection_verification"] = {"status": "satisfied", "primary_code": "69040",
                                      "selected_secondary_codes": ["69041"] if selected else []}
    return card


def test_trusted_pairing_selection_states_are_rendered_without_generic_registration_guarantee():
    card = _pairing_card()
    normalized = normalize_recommendation_card(card)
    assert normalized["selection_verification"]["status"] == "satisfied"
    answer = render_recommendation_answer([card])
    assert "配套班次代码：69041" in answer and "当前选择满足已核实的配对规则" in answer
    assert "不代表其他注册条件已通过" in answer
    assert normalize_recommendation_card(normalized) == normalized
    incomplete = render_recommendation_answer([_pairing_card(False)])
    assert "当前选择未满足已核实的完整配对规则" in incomplete
    missing = _pairing_card()
    missing["selection_verification"]["selected_secondary_codes"] = ["69999"]
    assert normalize_recommendation_card(missing)["selection_verification"]["status"] == "unknown"


def test_v2_current_primary_projection_keeps_other_primary_unknown_and_all_rules():
    card = _card()
    proof = {"status": "verified", "source": "registrar_websoc", "course_id": "SOCIOL 1", "term": "2026 Fall"}
    card["pairing"] = {"version": 2, "course_id": "SOCIOL 1", "term": "2026 Fall", "evidence": proof,
                       "per_primary": {"69000": {"requirement": "unknown"},
                                       "69040": {"requirement": "not_required", "relationship": "verified",
                                                 "relationship_complete": True, "required_components": []}}}
    card["pairing_selected"] = {"requirement": "unknown"}  # Stale saved projection is not authoritative.
    normalized = normalize_recommendation_card(card)
    assert set(normalized["pairing"]["per_primary"]) == {"69000", "69040"}
    assert normalized["pairing"]["per_primary"]["69000"]["requirement"] == "unknown"
    assert normalized["pairing_selected"]["requirement"] == "not_required"
    assert normalized["presentation"]["pairing"]["requirement"] == "not_required"
    assert normalized["selection_verification"]["status"] == "satisfied"
    answer = render_recommendation_answer([card])
    assert "此主班无需配套教学组件" in answer and "配对要求待核实" not in answer
    assert normalize_recommendation_card(normalized) == normalized


@pytest.mark.parametrize("groups,expected", [
    ([{"types": ["Dis", "Lab"], "min": 1, "max": 1}], "Dis 或 Lab"),
    ([{"types": ["Dis"], "min": 1, "max": 1}, {"types": ["Lab"], "min": 1, "max": 1}], "Dis + Lab"),
])
def test_v2_required_component_or_and_groups_are_not_conflated(groups, expected):
    card = _pairing_card()
    card["pairing"] = {"version": 2, "course_id": "SOCIOL 1", "term": "2026 Fall",
                       "evidence": {"status": "verified", "source": "registrar_websoc", "course_id": "SOCIOL 1", "term": "2026 Fall"},
                       "per_primary": {"69040": {"requirement": "required", "required_components": groups,
                                                 "relationship": "partial", "relationship_complete": False,
                                                 "allowed_bundles": [["69041"]]}}}
    answer = render_recommendation_answer([card])
    assert f"需要配套 {expected}" in answer


def test_requested_count_shortfall_is_reported_without_fabricating_extra_courses():
    answer = render_recommendation_answer([_card()], requested_count=3)
    assert "目前可展示 1 门，少于请求的 3 门" in answer


@pytest.mark.parametrize("language", ["zh", "en"])
def test_c3_missing_personal_audit_explains_request_and_action_without_defaulting_it_away(language):
    requested = {"course_scope": "ge", "hard_filters": {"only_unmet_ge": True}}
    applied = {"course_scope": "ge", "hard_filters": {"only_unmet_ge": False}}
    before = deepcopy(requested)
    answer = render_recommendation_answer([_card()], language,
        plan=applied, requested_plan=requested,
        gate={"enabled": True, "ready": False, "reasons": ["ge_audit_unverified"]})
    assert requested == before
    assert "SOCIOL" not in answer
    if language == "zh":
        for text in ("你要求只补尚未完成的 GE", "缺少已授权且已核实的个人 GE 审计", "不能猜测缺项",
                     "筛选未执行不代表你没有提出该要求", "请提供可核实的个人 GE 缺项审计"):
            assert text in answer
    else:
        for text in ("only remaining GE", "authorized, verified personal GE audit", "cannot be established or guessed",
                     "does not cancel your request", "Provide a verifiable audit"):
            assert text in answer


@pytest.mark.parametrize("reason,zh_text,en_text", [
    ("filter_plan_unavailable", "通过校验的筛选计划", "validated filter plan"),
    ("course_scope_not_applied", "GE 课程范围尚未应用", "GE course scope"),
    ("only_unmet_ge_not_applied", "只补剩余 GE", "remaining-GE-only"),
    ("earliest_start_not_applied", "最早上课时间硬性条件尚未应用", "earliest-start time"),
    ("earliest_start_scope_unsupported", "特定课程或星期", "particular courses or weekdays"),
    ("excluded_weekday_not_applied:Fri", "排除周五上课", "exclusion of Friday"),
    ("partial_day_time_scope_unsupported:Tue", "周二特定时段", "restriction on Tuesday"),
])
def test_blocked_hard_conditions_have_specific_bilingual_explanation_and_one_action(reason, zh_text, en_text):
    gate = {"enabled": True, "ready": False, "reasons": [reason]}
    zh, en = (render_recommendation_answer([], language, gate=gate) for language in ("zh", "en"))
    assert zh_text in zh and en_text in en
    assert "请" in zh
    assert len(zh.split("\n\n")) == 3
