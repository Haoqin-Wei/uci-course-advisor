from __future__ import annotations

import asyncio
import copy
import json

import pytest

from app.recommendations.filter_plan import (
    empty_filter_plan,
    normalize_ge_category,
    trusted_ge_audit,
    validate_filter_plan,
)


def _audit(**overrides):
    return {"ge_audit": {"verified": True, "authorized": True,
                         "source": "registrar_degree_audit",
                         "unmet_categories": ["GE-II", "GE-Va"], **overrides}}


def _hard(field, value, quote):
    plan = empty_filter_plan()
    if field == "course_scope":
        plan[field] = value
    else:
        plan["hard_filters"][field] = value
    if field == "earliest_start":
        plan["hard_filters"]["earliest_start_is_hard"] = True
    plan["explicit_constraints"] = [{"field": field, "quote": quote, "source": "user_message"}]
    return plan


def test_defaults_do_not_convert_ge_completion_into_exclusion():
    result = validate_filter_plan({}, "GE III修完了，推荐容易拿A的GE", _audit(unmet_categories=["II"]))
    assert result["status"] == "validated"
    assert result["plan"]["course_scope"] == "any"
    assert result["plan"]["hard_filters"] == {
        "only_unmet_ge": False, "earliest_start": None,
        "earliest_start_is_hard": False, "excluded_weekdays": [],
    }


def test_multiple_goals_preserved_without_weights():
    plan = {"goal": "graduation_progress", "secondary_goals": ["gpa_optimization", "interest_exploration"]}
    result = validate_filter_plan(plan, "尽快毕业，同时GPA尽量好")
    assert result["plan"]["secondary_goals"] == plan["secondary_goals"]
    assert result["plan"]["hard_filters"]["only_unmet_ge"] is False


@pytest.mark.parametrize("message", ["推荐简单GE课程", "帮我选GE", "推荐几门通识课", "GE课程有什么推荐",
                                      "只要能满足我尚未完成GE的课", "Recommend good GE courses",
                                      "Which general education classes do you recommend?",
                                      "Only remaining GE courses"])
def test_ge_scope_requires_positive_current_request(message):
    result = validate_filter_plan(_hard("course_scope", "ge", message), message)
    assert result["status"] == "validated"
    assert result["plan"]["course_scope"] == "ge"


@pytest.mark.parametrize("message,quote", [
    ("不要GE，推荐专业课", "不要GE"),
    ("不要给我推荐GE课", "推荐GE课"),
    ("Don't recommend GE courses", "recommend GE courses"),
    ("我的GE全部修完了，推荐容易拿A的课程", "我的GE全部修完了，推荐容易拿A的课程"),
    ("I completed GE and want easy courses", "I completed GE and want easy courses"),
    ("推荐GE或专业课都行", "推荐GE"),
    ("Recommend GE or major courses", "Recommend GE"),
    ("最好推荐GE课", "推荐GE课"),
    ("I prefer GE courses", "GE courses"),
    ("推荐非GE课程", "推荐非GE课程"),
    ("推荐和GE无关的课", "推荐和GE无关的课"),
    ("举例：推荐GE课程", "推荐GE课程"),
])
def test_ge_mentions_negation_mixed_scope_and_preferences_are_not_hard_scope(message, quote):
    result = validate_filter_plan(_hard("course_scope", "ge", quote), message)
    assert result["plan"]["course_scope"] == "any"
    assert result["issues"] == ["course_scope_not_explicit"]


def test_ge_scope_without_evidence_cannot_be_imposed_by_model():
    result = validate_filter_plan({"course_scope": "ge"}, "推荐GE课程")
    assert result["plan"]["course_scope"] == "any"
    assert result["status"] == "downgraded"


def test_explicit_ge_request_after_completion_statement_still_sets_scope():
    message = "我的GE都修完了，推荐容易拿A的GE课程"
    result = validate_filter_plan(_hard("course_scope", "ge", "推荐容易拿A的GE课程"), message)
    assert result["plan"]["course_scope"] == "ge"
    assert result["plan"]["hard_filters"]["only_unmet_ge"] is False


@pytest.mark.parametrize("extra", [{"sql": "DELETE FROM courses"}, {"weights": {"grade_a_rate": 1}},
                                  {"hard_filters": {"unknown_rule": True}},
                                  {"goal": "all_grades"}, {"hard_filters": {"only_unmet_ge": "false"}},
                                  {"hard_filters": {"earliest_start": "25:00"}},
                                  {"hard_filters": {"excluded_weekdays": ["Funday"]}},
                                  {"soft_preferences": {"high_a_rate": 1}}, None, [], "{}"])
def test_invalid_whitelist_schema_is_safe(extra):
    result = validate_filter_plan(extra, "选容易的课")
    assert result == {"plan": empty_filter_plan(), "status": "fallback", "issues": ["invalid_schema"]}


@pytest.mark.parametrize("message", ["只要能满足我尚未完成GE的课", "Only recommend courses satisfying my remaining GE requirements"])
def test_exclusive_unmet_ge_requires_verified_authorized_audit(message):
    plan = _hard("only_unmet_ge", True, message)
    result = validate_filter_plan(plan, message, _audit())
    assert result["plan"]["hard_filters"]["only_unmet_ge"] is True
    assert result["status"] == "validated"
    missing = validate_filter_plan(plan, message)
    assert missing["plan"]["hard_filters"]["only_unmet_ge"] is False
    assert missing["issues"] == ["ge_audit_unverified"]


@pytest.mark.parametrize("message", ["尽快毕业，同时GPA尽量好", "最好只要剩余GE的课", "不只要尚未完成GE的课",
                                      "我没说只要未完成GE的课", "My only goal is GPA and I have remaining GE",
                                      "只要高A率GE，已完成也可以，未完成也可以", "举例：只要未完成GE的课"])
def test_graduation_preference_or_negation_is_not_exclusivity(message):
    result = validate_filter_plan(_hard("only_unmet_ge", True, message), message, _audit())
    assert result["plan"]["hard_filters"]["only_unmet_ge"] is False
    assert "only_unmet_ge_not_explicit" in result["issues"]


@pytest.mark.parametrize("overrides", [{"verified": False}, {"verified": 1}, {"authorized": False},
                                      {"source": "user_claim"}, {"source": ["registrar_degree_audit"]},
                                      {"unmet_categories": None}, {"unmet_categories": ["GE-UNKNOWN"]},
                                      {"projected_categories": "III"}])
def test_audit_unknowns_are_not_inferred_completed(overrides):
    assert trusted_ge_audit(_audit(**overrides)) is None


def test_audit_normalization_projection_and_empty_unmet_list():
    profile = _audit(unmet_categories=["GE-III", "ge-va", "III"], projected_categories=["GE-III"])
    before = copy.deepcopy(profile)
    audit = trusted_ge_audit(profile)
    assert audit["unmet_categories"] == ["III", "VA"]
    assert audit["projected_categories"] == ["III"]
    assert profile == before
    assert trusted_ge_audit(_audit(unmet_categories=[]))["unmet_categories"] == []
    assert trusted_ge_audit({"ge_audit": {"verified": True, "authorized": True,
                                         "source": "registrar_degree_audit"}}) is None


@pytest.mark.parametrize("value,expected", [("GE-III", "III"), (" GE - Va ", "VA"), ("VIII", "VIII"),
                                            ("GE-unknown", None), ("GE-1", None), (None, None), (True, None)])
def test_unknown_ge_category_remains_unknown(value, expected):
    assert normalize_ge_category(value) == expected


@pytest.mark.parametrize("message,clock", [("绝对不能有10点以前的课", "10:00"),
                                            ("不要早于10:30开始", "10:30"),
                                            ("Absolutely no classes before 10am", "10:00"),
                                            ("Classes must start no earlier than 10:30 am", "10:30"),
                                            ("No classes before 10pm", "22:00"),
                                            ("课程必须在10点以后开始", "10:00")])
def test_explicit_earliest_start_value_is_verified(message, clock):
    result = validate_filter_plan(_hard("earliest_start", clock, message), message)
    assert result["plan"]["hard_filters"]["earliest_start_is_hard"] is True
    assert result["plan"]["hard_filters"]["earliest_start"] == clock


@pytest.mark.parametrize("message,quote,clock", [("最好不要早八", "不要早八", "09:00"),
                                                   ("Ideally no classes before 10am", "no classes before 10am", "10:00"),
                                                   ("绝对不能有10点以前的课", "绝对不能有10点以前的课", "11:00"),
                                                   ("绝对不能有10点以后的课", "绝对不能有10点以后的课", "10:00"),
                                                   ("必须上10点以前的课", "必须上10点以前的课", "10:00"),
                                                   ("翻译：绝对不能有10点以前的课", "绝对不能有10点以前的课", "10:00"),
                                                   ("绝对不能有10点以前的课，但这不是硬性要求", "绝对不能有10点以前的课", "10:00")])
def test_soft_hidden_prefix_wrong_value_or_reverse_bound_does_not_exclude(message, quote, clock):
    result = validate_filter_plan(_hard("earliest_start", clock, quote), message)
    assert result["plan"]["hard_filters"]["earliest_start_is_hard"] is False
    assert result["plan"]["hard_filters"]["earliest_start"] is None


def test_original_span_cannot_be_invented_or_restored_from_history():
    plan = _hard("earliest_start", "10:00", "绝对不能有10点以前的课")
    result = validate_filter_plan(plan, "现在推荐容易拿A的课")
    assert result["plan"]["hard_filters"]["earliest_start_is_hard"] is False
    plan["explicit_constraints"][0]["source"] = "conversation_history"
    assert validate_filter_plan(plan, "选课")["status"] == "fallback"


@pytest.mark.parametrize("message,quote", [
    ("周五绝对不能有10点以前的课，其他天随便", "绝对不能有10点以前的课"),
    ("CS161必须10点以后，其他课程不限", "CS161必须10点以后"),
    ("cs161必须10点以后，其他课程不限", "cs161必须10点以后"),
    ("COMPSCI 161必须10点以后，其他课程不限", "COMPSCI 161必须10点以后"),
    ("On Fridays no classes before 10am; other days are unrestricted", "no classes before 10am"),
    ("For CS161, no classes before 10am; other courses are unrestricted", "no classes before 10am"),
    ("这门课必须10点以后，其他课程不限", "这门课必须10点以后"),
    ("绝对不能有10点以前的课，周三除外", "绝对不能有10点以前的课"),
])
def test_local_cutoff_or_exception_cannot_be_promoted_to_global(message, quote):
    result = validate_filter_plan(_hard("earliest_start", "10:00", quote), message)
    assert result["plan"]["hard_filters"]["earliest_start_is_hard"] is False
    assert result["plan"]["hard_filters"]["earliest_start"] is None
    assert result["plan"]["soft_preferences"]["avoid_early_classes"] is False
    assert result["status"] == "downgraded"
    assert result["issues"] == ["earliest_start_scope_unsupported"]


@pytest.mark.parametrize("message", ["所有课程都必须10点以后开始，包括CS161",
                                      "所有天都绝对不能有10点以前的课，包括周五",
                                      "All classes must start no earlier than 10am",
                                      "CS161修完了。绝对不能有10点以前的课"])
def test_explicit_global_cutoff_is_preserved(message):
    result = validate_filter_plan(_hard("earliest_start", "10:00", message), message)
    assert result["plan"]["hard_filters"]["earliest_start_is_hard"] is True
    assert result["plan"]["hard_filters"]["earliest_start"] == "10:00"


def test_mixed_hard_and_soft_constraints_are_independent():
    message = "绝对不能有10点以前的课，最好周五不要上课"
    plan = _hard("earliest_start", "10:00", "绝对不能有10点以前的课")
    plan["hard_filters"]["excluded_weekdays"] = ["Fri"]
    plan["explicit_constraints"].append({"field": "excluded_weekdays", "quote": "周五不要上课", "source": "user_message"})
    result = validate_filter_plan(plan, message)
    assert result["plan"]["hard_filters"]["earliest_start_is_hard"] is True
    assert result["plan"]["hard_filters"]["excluded_weekdays"] == []
    assert result["plan"]["soft_preferences"]["avoid_weekdays"] == ["Fri"]


@pytest.mark.parametrize("message,days", [("绝对不要周五的课", ["Fri"]),
                                           ("我必须周五没课", ["Fri"]),
                                           ("No classes on Tuesdays or Fridays", ["Tue", "Fri"])])
def test_explicit_whole_day_exclusion(message, days):
    result = validate_filter_plan(_hard("excluded_weekdays", days, message), message)
    assert result["plan"]["hard_filters"]["excluded_weekdays"] == days


@pytest.mark.parametrize("message", ["最好不要周五上课", "我必须周五上课", "我周五不能没有课", "周五不要10点之前的课",
                                      "不要周五上午的课", "周五不能有考试", "举例：绝对不要周五的课"])
def test_partial_day_soft_or_opposite_request_is_not_whole_day_exclusion(message):
    result = validate_filter_plan(_hard("excluded_weekdays", ["Fri"], message), message)
    assert result["plan"]["hard_filters"]["excluded_weekdays"] == []


def test_validator_is_reproducible_and_does_not_mutate_input():
    message = "只要能满足我尚未完成GE的课"
    plan, profile = _hard("only_unmet_ge", True, message), _audit()
    before = copy.deepcopy((plan, profile))
    assert validate_filter_plan(plan, message, profile) == validate_filter_plan(plan, message, profile)
    assert (plan, profile) == before


def test_parser_reuses_existing_json_call_and_sends_only_bounded_context(monkeypatch):
    from app.llm import adapter
    from app.llm.filter_plan import parse_filter_plan

    calls = []
    monkeypatch.setattr(adapter, "LLM_ENABLED", True)

    async def call(system, content, json_mode=False):
        calls.append((system, json.loads(content), json_mode))
        return json.dumps({"goal": "gpa_optimization", "soft_preferences": {"high_a_rate": True}})

    monkeypatch.setattr(adapter, "_call_llm", call)
    result = asyncio.run(parse_filter_plan("推荐容易拿A的课", {"summary": "x" * 3000,
                        "recent_turns": [{"role": "user", "content": "hello"}],
                        "transcript": "PRIVATE TRANSCRIPT"}, {**_audit(), "transcript": "PRIVATE GRADES"}))
    assert result["status"] == "validated"
    assert result["metrics"]["llm_calls"] == 1
    assert result["metrics"]["parse_elapsed_ms"] >= 0
    assert result["plan"]["soft_preferences"]["high_a_rate"] is True
    assert calls[0][2] is True
    assert len(calls[0][1]["conversational_context"]["summary"]) == 1600
    assert "PRIVATE" not in json.dumps(calls[0][1])
    assert calls[0][1]["ge_audit_summary"]["unmet_categories"] == ["II", "VA"]


def test_disabled_parser_has_no_llm_call(monkeypatch):
    from app.llm import adapter
    from app.llm.filter_plan import parse_filter_plan

    monkeypatch.setattr(adapter, "LLM_ENABLED", False)

    async def forbidden(*args, **kwargs):
        pytest.fail("disabled parser called LLM")

    monkeypatch.setattr(adapter, "_call_llm", forbidden)
    result = asyncio.run(parse_filter_plan("只要未完成GE的课", profile=_audit()))
    assert result["plan"] == empty_filter_plan()
    assert result["metrics"]["llm_calls"] == 0
    assert result["metrics"]["failure_reason"] == "model_disabled"


@pytest.mark.parametrize("response,reason", [("not JSON", "model_invalid_json"),
                                            ("[]", "model_invalid_schema"),
                                            ('{"sql":"SELECT *"}', "model_invalid_schema")])
def test_invalid_model_output_falls_back_without_logging_original_text(monkeypatch, caplog, response, reason):
    from app.llm import adapter
    from app.llm.filter_plan import parse_filter_plan

    monkeypatch.setattr(adapter, "LLM_ENABLED", True)

    async def call(*args, **kwargs):
        return response

    monkeypatch.setattr(adapter, "_call_llm", call)
    with caplog.at_level("INFO"):
        result = asyncio.run(parse_filter_plan("周五绝对不能上课 PRIVATE ORIGINAL REQUEST"))
    assert result["plan"] == empty_filter_plan()
    assert result["status"] == "fallback"                     # a condition may be in there: stop and ask
    assert result["metrics"]["failure_reason"] == reason
    assert result["metrics"]["llm_calls"] == 2                # one retry
    assert response not in caplog.text
    assert "PRIVATE ORIGINAL REQUEST" not in caplog.text


@pytest.mark.parametrize("error,reason", [(asyncio.TimeoutError(), "model_timeout"),
                                        (RuntimeError("PRIVATE API ERROR"), "model_error")])
def test_parser_failure_does_not_leak_error_or_apply_hard_rules(monkeypatch, caplog, error, reason):
    from app.llm import adapter
    from app.llm.filter_plan import parse_filter_plan

    monkeypatch.setattr(adapter, "LLM_ENABLED", True)

    async def call(*args, **kwargs):
        raise error

    monkeypatch.setattr(adapter, "_call_llm", call)
    with caplog.at_level("INFO"):
        result = asyncio.run(parse_filter_plan("绝对不能有10点以前的课"))
    assert result["plan"] == empty_filter_plan()
    assert result["metrics"]["failure_reason"] == reason
    assert "PRIVATE API ERROR" not in caplog.text


def test_parser_timeout_is_bounded_and_cancels_work(monkeypatch):
    from app.llm import adapter, filter_plan as parser

    cancelled = []
    monkeypatch.setattr(adapter, "LLM_ENABLED", True)
    monkeypatch.setattr(parser, "PARSER_TIMEOUT_SECONDS", 0.01)

    async def call(*args, **kwargs):
        try:
            await asyncio.sleep(2)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(adapter, "_call_llm", call)
    result = asyncio.run(parser.parse_filter_plan("选课"))
    assert cancelled == [True, True]                               # both attempts are bounded and cancelled
    assert result["metrics"]["recovered_from"] == "model_timeout"   # "选课" carries no condition to lose
    assert result["status"] == "validated" and result["plan"] == empty_filter_plan()


def test_parser_validates_model_hard_rules_against_current_not_prior_request(monkeypatch):
    from app.llm import adapter
    from app.llm.filter_plan import parse_filter_plan

    old = "绝对不能有10点以前的课"
    monkeypatch.setattr(adapter, "LLM_ENABLED", True)

    async def call(*args, **kwargs):
        return json.dumps(_hard("earliest_start", "10:00", old))

    monkeypatch.setattr(adapter, "_call_llm", call)
    result = asyncio.run(parse_filter_plan("推荐容易拿A的课", {"history": [{"role": "user", "content": old}]}))
    assert result["status"] == "downgraded"
    assert result["plan"]["hard_filters"]["earliest_start_is_hard"] is False
    assert result["metrics"]["failure_reason"] is None


@pytest.mark.parametrize("message,carries_nothing", [
    ("推荐一下下学期的选课", True), ("帮我排下学期的课", True), ("what should I take next quarter", True),
    ("推荐下学期的课，周五绝对不能上课", False), ("不要早八", False), ("推荐3门课", True),
    ("只推荐我还缺的GE", False), ("no classes before 10am", False), ("下午的课", False), ("", False)])
def test_only_a_sentence_with_no_condition_wording_survives_a_failed_parse(message, carries_nothing):
    from app.recommendations.filter_plan import states_no_condition
    assert states_no_condition(message) is carries_nothing


def test_a_failed_parse_of_a_plain_request_goes_on_with_defaults_after_one_retry(monkeypatch):
    from app.llm import adapter
    from app.llm.filter_plan import parse_filter_plan

    monkeypatch.setattr(adapter, "LLM_ENABLED", True)
    replies = []

    async def call(*args, **kwargs):
        replies.append(1)
        if len(replies) == 1:
            raise asyncio.TimeoutError()
        return '{"type": "term_plan"}'

    monkeypatch.setattr(adapter, "_call_llm", call)
    result = asyncio.run(parse_filter_plan("推荐一下下学期的选课"))
    assert result["status"] == "validated" and result["plan"] == empty_filter_plan() and result["issues"] == []
    assert result["metrics"] == {**result["metrics"], "llm_calls": 2, "failure_reason": None,
                                 "recovered_from": "model_invalid_schema"}

    replies.clear()

    async def second_time_lucky(*args, **kwargs):
        replies.append(1)
        if len(replies) == 1:
            raise asyncio.TimeoutError()
        return "{}"

    monkeypatch.setattr(adapter, "_call_llm", second_time_lucky)
    retried = asyncio.run(parse_filter_plan("推荐下学期的课，周五绝对不能上课"))
    assert retried["status"] == "validated" and "recovered_from" not in retried["metrics"]
