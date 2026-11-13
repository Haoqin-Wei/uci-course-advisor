"""Course identity evidence must be readable outside recommendation filtering."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from app.agent import prefilter
from app.agent.loop import uses_controlled_recommendation_output
from app.llm.intent_rules import classify_by_rules, is_factual_course_identity_query


ORIGINAL_QUESTION = (
    "请核实 SWE43 和 IN4MATX43 是什么关系，并说明依据。"
    "假设我在 2026 Winter 修过 IN4MATX43，而且已经获得这门课的学分，"
    "还能在 2026 Fall 再修 SWE43 吗？请根据课程说明回答。"
)


@pytest.mark.parametrize("question", [
    ORIGINAL_QUESTION,
    "SWE43 和 IN4MATX43 是什么关系？",
    "SWE43 与 IN4MATX43 是否是同一门课？",
    "SWE43 以前叫什么，课程编号改名了吗？",
    "我修过 IN4MATX43，还可以修 SWE43 吗？",
    "已经修完 SWE43，还能再修这门课吗？",
    "What is the relationship between SWE43 and IN4MATX43?",
    "Compare SWE43 and IN4MATX43: are they the same course?",
    "Was SWE43 previously offered as INF43?",
    "Has SWE43 been renamed?",
    "I have prior credit for IN4MATX43. Can I take SWE43?",
    "I took IN4MATX43. May I still enroll in SWE43?",
    "I completed SWE43. Can I take it again?",
    "I have prior credit for INF43. Can I take SWE43 or IN4MATX43 again?",
    "I completed INF43. Should I take SWE43 again?",
    "我修过 INF43，已经取得学分，还能修 SWE43 还是不能重复拿学分？",
    "不要推荐课程，只核实 SWE43 和 IN4MATX43 是否同一门课。",
    "不需要给我任何推荐，SWE43 与 IN4MATX43 是什么关系？",
    '先别做“推荐”，只解释 SWE43 和 IN4MATX43 的旧课代码关系。',
    "Do not recommend courses. Are SWE43 and IN4MATX43 equivalent?",
    "I don't need course recommendations; was SWE43 formerly offered as INF43?",
    'The label says "recommend courses". Are SWE43 and IN4MATX43 equivalent?',
])
def test_identity_and_prior_credit_facts_do_not_become_recommendations(question):
    result = classify_by_rules(question)
    assert result["intent"] == "single_query"
    assert result["rule_id"] == "course-identity-fact"
    assert is_factual_course_identity_query(question)
    assert not prefilter.is_recommendation_request(question, "explicit")


@pytest.mark.parametrize("question", [
    "查清 SWE43 和 IN4MATX43 的关系，再推荐可以替代的课程。",
    "SWE43 是 IN4MATX43 改名的吗？另外帮我选一门简单GE。",
    "SWE43 和 IN4MATX43 是同一门吗，哪个更适合我？",
    "SWE43 与 IN4MATX43 是否同一门课，应该选哪个？",
    "不要推荐GE，查清 SWE43 的旧课关系后推荐专业课。",
    "Are SWE43 and IN4MATX43 the same course? Then recommend an alternative.",
    "Was SWE43 formerly offered as INF43, and which should I take?",
    "SWE43 和 IN4MATX43 是同一门课吗？我该选 SWE43 还是 ICS45C？",
    "Are SWE43 and IN4MATX43 the same course? Should I take SWE43 or ICS45C?",
])
def test_mixed_identity_and_actual_recommendation_keeps_recommendation_route(question):
    assert not is_factual_course_identity_query(question)
    assert classify_by_rules(question)["intent"] == "course_recommendation"
    # An explicit choice remains a recommendation even in a historical query.
    assert prefilter.is_recommendation_request(question, "history")


@pytest.mark.parametrize("question", [
    "SWE43 和 ICS45C 哪个更适合我？帮我选一个。",
    "compare ICS 33 and SOC SCI 178C",
    "Should I take SWE43 or ICS45C?",
    "推荐简单GE课程",
])
def test_existing_choices_and_recommendations_are_preserved(question):
    assert classify_by_rules(question)["intent"] == "course_recommendation"
    assert prefilter.is_recommendation_request(question)


@pytest.mark.parametrize("question", [
    "这两门课是不是同一门，是否改名了？",
    "修过旧课还能修新课吗？",
    "Are these the same course? Can I take it again?",
    "The course was previously offered under another code.",
    "Can I take SWE43?",  # No prior-credit or identity evidence in the request.
])
def test_missing_course_or_identity_context_is_not_invented(question):
    assert not is_factual_course_identity_query(question)
    result = classify_by_rules(question)
    assert result is None or result["rule_id"] != "course-identity-fact"


def test_factual_route_skips_plan_parser_preserves_both_terms_and_allows_course_metadata(monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "true")
    from app.llm import filter_plan

    async def unexpected_parser(*args, **kwargs):
        pytest.fail("factual course identity queries must not invoke the recommendation plan parser")

    monkeypatch.setattr(filter_plan, "parse_filter_plan", unexpected_parser)
    context = {
        "user_query": ORIGINAL_QUESTION,
        "term": "2026 Fall",
        "default_term": "2027 Winter",
        "allowed_query_terms": ["2026 Winter", "2026 Fall"],
        "query_term_source": "explicit",
    }
    original = deepcopy(context)
    asyncio.run(prefilter.initialize(context, [{"role": "user", "content": ORIGINAL_QUESTION}]))
    assert context == original
    assert not prefilter.active(context)
    assert not uses_controlled_recommendation_output(ORIGINAL_QUESTION, "explicit")
    args = {"course_id": "IN4MATX43"}
    allowed_args, rejection = prefilter.guard_call("get_course_info", args, context)
    assert allowed_args == args
    assert rejection is None
