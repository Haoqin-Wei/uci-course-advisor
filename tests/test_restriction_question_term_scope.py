"""Enrollment-restriction questions must not become offering-pattern history lookups.

Reproduced on 2026-11-11 with the live model: "计算机系的课什么时候对外专业开放"
was classified as an offering pattern, so the allowed query terms became the
six completed terms and the term being enrolled in was rejected by the guard.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.agent import workflow_router
from app.data.restriction_timeline import (
    RestrictionType, classify_restriction_type, is_enrollment_restriction_question)
from app.terms.intent import PROMPT, apply_semantic_plan, refine_query_scope
from app.terms.query_scope import resolve_query_scope

NOW = datetime(2026, 11, 11, 23, tzinfo=ZoneInfo("America/Los_Angeles"))
KWARGS = {"default_term": "2026 Fall", "current_term": "2026 Fall", "focus": None}
PATTERN = {"intent": "offering_pattern"}
HISTORY = ("2024 Fall", "2025 Winter", "2025 Spring", "2025 Fall", "2026 Winter", "2026 Spring")

RESTRICTION_QUESTIONS = [
    "计算机系的课什么时候对外专业开放",          # the reported message
    "CS 课什么时候对全校开放",
    "ICS 33 外专业能选吗",
    "非本专业可以选 CS 161 吗",
    "专业限制什么时候解除",
    "When do CS courses open to non-majors?",
    "When can non-majors enroll in ICS 46?",
    "When are major restrictions lifted for COMPSCI?",
]
PATTERN_QUESTIONS = [
    "ICS 33 一般在哪几个季度有",
    "Does ICS 45C run every quarter?",
    "这门课是不是每年都有",
]
NOT_RESTRICTION = [
    "ICS 33 通常什么时候开", "MATH 2B 什么时候开课", "推荐几门 Winter 2027 的计算机课",
    "想学一些其他专业的课，有什么推荐", "这门课对新手开放吗？难不难",
    "Which quarters is ICS 45C usually offered?", "Is CS 161 offered in Spring?",
]


def resolve(message):
    return resolve_query_scope(message, "2026 Fall", "2026 Fall", None, NOW, current_term="2026 Fall")


@pytest.mark.parametrize("message", RESTRICTION_QUESTIONS)
def test_restriction_question_keeps_the_default_term_even_if_the_model_says_pattern(message):
    scope = resolve(message)
    assert scope.source == "default" and scope.intent != "offering_pattern"
    assert is_enrollment_restriction_question(message)

    refined = apply_semantic_plan(scope, PATTERN, message=message, **KWARGS)

    assert refined == scope                                  # still the default term, no history window
    assert refined.canonical_terms != HISTORY
    assert workflow_router.route_solution(message)["intents"] == ["department_restriction"]


@pytest.mark.parametrize("message", PATTERN_QUESTIONS)
def test_real_offering_pattern_questions_still_get_the_history_window(message):
    scope = resolve(message)
    assert not is_enrollment_restriction_question(message)
    refined = apply_semantic_plan(scope, PATTERN, message=message, **KWARGS)
    assert refined.canonical_terms == HISTORY and refined.intent == "offering_pattern"
    # Callers that do not pass the message keep the previous behaviour.
    assert apply_semantic_plan(scope, PATTERN, **KWARGS).canonical_terms == HISTORY


@pytest.mark.parametrize("message", NOT_RESTRICTION)
def test_ordinary_questions_are_not_treated_as_restriction_questions(message):
    assert is_enrollment_restriction_question(message) is False


def test_deterministic_history_wording_is_untouched():
    scope = resolve("ICS 33 通常什么时候开")
    assert scope.source == "history" and scope.canonical_terms == HISTORY


def test_live_classifier_answer_is_overruled_end_to_end(monkeypatch):
    from app.llm import adapter

    monkeypatch.setattr(adapter, "LLM_ENABLED", True)

    async def says_pattern(*_args, **_kwargs):
        return json.dumps(PATTERN)

    monkeypatch.setattr(adapter, "_call_llm", says_pattern)
    message = RESTRICTION_QUESTIONS[0]
    scope = resolve(message)
    refined = asyncio.run(refine_query_scope(scope, message=message, recent_turns=[], uci_now=NOW, **KWARGS))
    assert refined == scope

    pattern_message = PATTERN_QUESTIONS[0]
    refined = asyncio.run(refine_query_scope(
        resolve(pattern_message), message=pattern_message, recent_turns=[], uci_now=NOW, **KWARGS))
    assert refined.canonical_terms == HISTORY


def test_prompt_and_type_classification_cover_opening_to_other_majors():
    assert "NOT an offering_pattern" in PROMPT
    assert classify_restriction_type("计算机系的课什么时候对外专业开放") is RestrictionType.SCHOOL_MAJOR
    assert classify_restriction_type("When do CS courses open to non-majors?") is RestrictionType.SCHOOL_MAJOR
    assert classify_restriction_type("New Only 限制什么时候解除") is RestrictionType.NEW_ONLY   # earlier rule still first
