"""
Deterministic intent classification rules.

When a user message matches a high-confidence pattern, we skip the LLM
classifier entirely and route directly. This eliminates the
non-determinism that caused "我决定选 CS122A" to bounce between
course_recommendation / single_query / off_topic across consecutive
calls in our tests.

Patterns are checked in order, first match wins. Three categories:

  1. Course commitments / decisions / drops mentioning a specific
     course → single_query  (focused analysis of THE course they
     chose, not a fresh round of suggestions)
  2. "What about X?" / "Tell me about X" / specific course or
     professor questions → single_query
  3. "Recommend me…" / "推荐…" / "easy GE" / multi-course
     comparisons → course_recommendation

When nothing matches → returns None and the caller falls through to
the LLM classifier (which now also has few-shot examples for
ambiguous inputs).

Public API:
    classify_by_rules(message) -> Optional[dict]
"""
from __future__ import annotations

import re
from typing import Optional

from app.catalog.departments import colloquial_course_id
from app.catalog.normalization import iter_course_mentions


_COURSE_LOOKAHEAD_CHARS = 80
_COURSE_LOOKBEHIND_CHARS = 80


_COURSE_PREFIX_RULES: list[tuple[re.Pattern, str, str]] = [
    # ─────────────────────────────────────────────────────
    # 1. COMMITMENTS — user has decided on a specific course
    #    → single_query (focused analysis of THIS course)
    # ─────────────────────────────────────────────────────

    # Chinese: 我决定/打算/准备/要 + 选/修/上/报/学 + course_id
    # NOTE: 决定/打算/准备 are multi-char tokens — they must be in an
    # alternation group, not a [character class] (which is single-char).
    (re.compile(
        r"我\s*(?:决定|打算|准备|要)\s*[选修上报学]\s*",
     ), "single_query", "zh-commitment"),

    # Chinese: 就选 / 敲定 / 定下 + course_id
    (re.compile(
        r"(?:就[选修上]|敲定|定下|定了)\s*",
     ), "single_query", "zh-commitment-2"),

    # Chinese: 放弃 / 不选 / 不修 / 不上 + course_id
    (re.compile(
        r"(?:放弃|不[选修上])\s*",
     ), "single_query", "zh-drop"),

    # English: I'll/I will/I'm going to + take/enroll/drop + course_id
    (re.compile(
        r"\b(?:I'?ll|I will|I'?m going to|going to)\s+"
        r"(?:take|enroll in|sign up for|register for|drop|skip)\s+",
        re.IGNORECASE,
     ), "single_query", "en-commitment"),

    # English: going with / decided on / decided to take + course_id
    (re.compile(
        r"\b(?:going\s+with|decided\s+(?:on|to\s+(?:take|enroll\s+in)))\s+",
        re.IGNORECASE,
     ), "single_query", "en-commitment-2"),

    # English: drop/dropping/skip/skipping + course_id
    (re.compile(
        r"\b(?:drop|dropping|skip|skipping)\s+",
        re.IGNORECASE,
     ), "single_query", "en-drop"),
]


_COURSE_SUFFIX_RULES: list[tuple[re.Pattern, str, str]] = [
    # Chinese: course_id + 怎么样 / 难不难 / 好不好 / 给分 / 评价
    (re.compile(
        r"\s*(?:怎么样|难不难|好不好|怎样|如何|给分|评价)",
     ), "single_query", "zh-course-question"),
]


_COURSE_LOOKBEHIND_RULES: list[tuple[re.Pattern, str, str]] = [
    # English: How is/How's/What is + course_id
    (re.compile(
        r"(?:how(?:'?s|\s+is)|what(?:'?s|\s+is)|what about|tell me about)\s+$",
        re.IGNORECASE,
     ), "single_query", "en-course-question"),
]


# Order matters for non-course rules: more specific patterns first.
_RULES: list[tuple[re.Pattern, str, str]] = [
    # ─────────────────────────────────────────────────────
    # 2. SPECIFIC-PROFESSOR queries
    #    → single_query
    # ─────────────────────────────────────────────────────

    # English: How is/about professor X (proper-cased name)
    (re.compile(
        r"\b(?:how(?:'?s|\s+is)|tell me about|how about)\s+(?:professor\s+|prof\s+|Dr\.?\s+)"
        r"[A-Z][a-z]{2,}",
     ), "single_query", "en-prof-question"),

    # English: Professor X + rating/review/grade
    (re.compile(
        r"\bprofessor\s+[A-Z][a-z]{2,}\b.*\b(?:rating|review|grade|tough|easy|grading)",
        re.IGNORECASE,
     ), "single_query", "en-prof-rating"),

    # ─────────────────────────────────────────────────────
    # 3. RECOMMENDATION REQUESTS
    #    → course_recommendation
    # ─────────────────────────────────────────────────────

    # Chinese: 推荐 / 建议 / 帮我选 / 帮我看看 / 排课
    (re.compile(
        r"(?:推荐|建议|帮我[选看]|帮我排|怎么选|选什么|排课)",
     ), "course_recommendation", "zh-recommend"),

    # Chinese: 下学期 / 春季 / 秋季 + 课/选/上
    (re.compile(
        r"(?:下学期|下一?个?学期|春季|秋季|冬季|夏季|spring|fall|winter|summer)\s*"
        r"(?:学期)?\s*(?:[选修上]什么|课|class)",
        re.IGNORECASE,
     ), "course_recommendation", "zh-term-recommend"),

    # English: Recommend / suggest + courses/me/some
    (re.compile(
        r"\b(?:recommend|suggest)\s+(?:some|a|me|courses?|classes?)",
        re.IGNORECASE,
     ), "course_recommendation", "en-recommend"),

    # English: What courses/classes should I … / What should I take
    (re.compile(
        r"\bwhat\s+(?:courses?|classes?)\s+(?:should|can|to)\s+I",
        re.IGNORECASE,
     ), "course_recommendation", "en-what-to-take"),

    (re.compile(
        r"\bwhat\s+should\s+I\s+(?:take|enroll|sign up for)",
        re.IGNORECASE,
     ), "course_recommendation", "en-should-take"),

    # English: easy/good/best + GE / electives / courses
    (re.compile(
        r"\b(?:easy|good|best|great|fun)\s+(?:GE|general[\s-]?education|elective|courses?|classes?)",
        re.IGNORECASE,
     ), "course_recommendation", "en-easy-courses"),

]


def _extract_course_ids(text: str) -> list[str]:
    """Collect all course IDs in the text (for the entities field)."""
    seen: set[str] = set()
    out: list[str] = []
    for ref, _start, _end in iter_course_mentions(text or ""):
        course_id = colloquial_course_id(ref.department, ref.course_number)
        if course_id in seen:
            continue
        seen.add(course_id)
        out.append(course_id)
    return out


def _has_course_after(text: str, offset: int) -> bool:
    return bool(iter_course_mentions(text[offset:offset + _COURSE_LOOKAHEAD_CHARS]))


def _has_course_before(text: str, offset: int) -> bool:
    start = max(0, offset - _COURSE_LOOKBEHIND_CHARS)
    return bool(iter_course_mentions(text[start:offset]))


def _classify_course_pattern(text: str) -> Optional[tuple[str, str]]:
    for pattern, intent, rule_id in _COURSE_PREFIX_RULES:
        for match in pattern.finditer(text):
            if _has_course_after(text, match.end()):
                return intent, rule_id

    for ref, start, end in iter_course_mentions(text):
        del ref
        suffix = text[end:end + _COURSE_LOOKAHEAD_CHARS]
        for pattern, intent, rule_id in _COURSE_SUFFIX_RULES:
            if pattern.match(suffix):
                return intent, rule_id

        prefix = text[max(0, start - _COURSE_LOOKBEHIND_CHARS):start]
        for pattern, intent, rule_id in _COURSE_LOOKBEHIND_RULES:
            if pattern.search(prefix):
                return intent, rule_id

    course_ids = _extract_course_ids(text)
    if len(course_ids) >= 2:
        if re.search(r"\b(?:compare|vs|or)\b", text, re.IGNORECASE):
            return "course_recommendation", "en-compare"
        if re.search(r"(?:还是|和|或|与)", text):
            return "course_recommendation", "zh-compare"

    return None


def classify_by_rules(message: str) -> Optional[dict]:
    """
    Return a classification result dict, or None if no rule matches.

    Result shape mirrors what classify_intent_llm returns so the caller
    can use them interchangeably:
      {
        "intent": "single_query" | "course_recommendation",
        "confidence": 1.0,
        "entities": {"course_ids": [...], "professor_names": [], ...},
        "source": "rule",
        "rule_id": "zh-commitment",     # for log diagnostics
      }
    """
    if not message or not isinstance(message, str):
        return None
    text = message.strip()
    if not text:
        return None

    course_pattern = _classify_course_pattern(text)
    if course_pattern:
        intent, rule_id = course_pattern
        course_ids = _extract_course_ids(text)
        return {
            "intent": intent,
            "confidence": 1.0,
            "entities": {
                "course_ids": course_ids,
                "professor_names": [],
                "term": None,
                "major": None,
                "difficulty_preference": None,
                "recommendation_goal": None,
            },
            "source": "rule",
            "rule_id": rule_id,
        }

    for pattern, intent, rule_id in _RULES:
        if pattern.search(text):
            course_ids = _extract_course_ids(text)
            return {
                "intent": intent,
                "confidence": 1.0,
                "entities": {
                    "course_ids": course_ids,
                    "professor_names": [],
                    "term": None,
                    "major": None,
                    "difficulty_preference": None,
                    "recommendation_goal": None,
                },
                "source": "rule",
                "rule_id": rule_id,
            }

    return None
