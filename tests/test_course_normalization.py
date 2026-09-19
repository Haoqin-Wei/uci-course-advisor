from __future__ import annotations

from app.catalog.normalization import iter_course_mentions, parse_course_mention
from app.catalog.types import CourseRef
from app.llm.intent_rules import classify_by_rules
from app.modules.decision_detector import detect_decisions
from app.routers import chat as chat_router


def _refs(text: str) -> list[CourseRef]:
    return [ref for ref, _start, _end in iter_course_mentions(text)]


def test_course_parser_handles_chinese_adjacent_course_id():
    assert _refs("我想选CS122A这门课") == [CourseRef("COMPSCI", "122A")]


def test_course_parser_handles_common_ics_forms():
    assert parse_course_mention("ICS 33") == CourseRef("I&C SCI", "33")
    assert parse_course_mention("I&C SCI 33") == CourseRef("I&C SCI", "33")


def test_course_parser_handles_two_token_social_science_department():
    assert parse_course_mention("SOC SCI 178C") == CourseRef("SOC SCI", "178C")


def test_course_parser_uses_catalog_department_and_special_course_number():
    assert parse_course_mention("AC ENG 22A") == CourseRef("AC ENG", "22A")
    assert parse_course_mention("HUMAN 1AS/A") == CourseRef("HUMAN", "1AS/A")


def test_chat_course_ids_use_canonical_parser_and_colloquial_output():
    assert chat_router._course_ids_in_order(
        "我想选CS122A这门课，也想问 ICS 33 / I&C SCI 33 和 SOC SCI 178C"
    ) == ["CS122A", "ICS33", "SOCSCI178C"]


def test_decision_detector_uses_canonical_course_parser():
    assert detect_decisions("我决定选CS122A这门课") == ["选 CS122A"]
    assert detect_decisions("I'll take I&C SCI 33") == ["Take ICS33"]
    assert detect_decisions("drop SOC SCI 178C") == ["Drop SOCSCI178C"]


def test_legacy_intent_rules_use_canonical_course_parser():
    commitment = classify_by_rules("我决定选CS122A这门课")
    assert commitment["intent"] == "single_query"
    assert commitment["entities"]["course_ids"] == ["CS122A"]

    comparison = classify_by_rules("compare ICS 33 and SOC SCI 178C")
    assert comparison["intent"] == "course_recommendation"
    assert comparison["entities"]["course_ids"] == ["ICS33", "SOCSCI178C"]
