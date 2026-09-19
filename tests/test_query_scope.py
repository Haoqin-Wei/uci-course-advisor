from datetime import datetime

from app.terms import LOS_ANGELES
from app.terms.query_scope import next_recent_focus_terms, resolve_query_scope


NOW = datetime(2026, 7, 31, 9, 0, tzinfo=LOS_ANGELES)


def resolve(
    message: str,
    *,
    default: str = "2025 Fall",
    automatic: str = "2026 Fall",
    focus: dict | None = None,
):
    return resolve_query_scope(message, default, automatic, focus, NOW)


def test_missing_term_uses_conversation_default() -> None:
    scope = resolve("ICS 33 有开吗？")
    assert scope.canonical_terms == ("2025 Fall",)
    assert scope.source == "default"
    assert scope.course_ids == ("ICS33",)


def test_explicit_single_and_comparison_do_not_mutate_any_input() -> None:
    focus = {"terms": ["2025 Winter"]}
    before = dict(focus)
    single = resolve("2024 Fall 有没有 ICS 33？", focus=focus)
    comparison = resolve("比较 2024 Fall 和 2025 Fall", focus=focus)
    assert single.canonical_terms == ("2024 Fall",)
    assert single.source == "explicit"
    assert comparison.canonical_terms == ("2024 Fall", "2025 Fall")
    assert comparison.source == "comparison"
    assert focus == before


def test_compact_shared_quarter_comparison_resolves_both_terms() -> None:
    scope = resolve("帮我查25和26winter开econ167了吗", default="2026 Winter")

    assert scope.canonical_terms == ("2025 Winter", "2026 Winter")
    assert scope.source == "comparison"
    assert scope.error is None
    assert scope.course_ids == ("ECON167",)


def test_unpaired_two_digit_year_never_falls_back_to_default() -> None:
    scope = resolve("帮我查25winter开econ167了吗", default="2026 Winter")

    assert scope.canonical_terms == ()
    assert scope.ambiguous is True
    assert scope.error and scope.error.code == "ambiguous"


def test_relative_term_rules_use_their_documented_bases() -> None:
    assert resolve("下学期呢？").canonical_terms == ("2027 Winter",)
    assert resolve("上个 quarter").canonical_terms == ("2026 Spring",)
    assert resolve("当前学期").canonical_terms == ("2026 Fall",)
    assert resolve("今年有吗？").canonical_terms == ("2026 Fall",)
    assert resolve("去年 Spring").canonical_terms == ("2025 Spring",)
    assert resolve("next year Winter").canonical_terms == ("2027 Winter",)


def test_followup_uses_only_structured_recent_focus() -> None:
    focus = {"terms": ["2024 Fall", "2025 Fall"], "course_ids": ["ICS33"]}
    pair = resolve("对比一下这两个学期", focus=focus)
    last = resolve("那门课在这个学期呢？", focus=focus)
    assert pair.canonical_terms == ("2024 Fall", "2025 Fall")
    assert pair.source == "comparison"
    assert last.canonical_terms == ("2025 Fall",)
    assert last.source == "followup"


def test_missing_year_is_inferred_but_missing_discussion_requires_clarification() -> None:
    quarter = resolve("show Fall courses")
    focus = resolve("比较这两个学期", focus=None)
    assert quarter.canonical_terms == ("2026 Fall",)
    assert quarter.inferred_year is True
    assert focus.ambiguous is True
    assert focus.error and focus.error.code == "ambiguous"


def test_summer_is_not_supported_in_chat() -> None:
    for message in ("2026 Summer1 有什么课？", "summer courses", "2026 暑期"):
        scope = resolve(message)
        assert scope.canonical_terms == ()
        assert scope.error.code == "invalid"


def test_q1_q2_q3_structured_focus_keeps_pair_without_changing_default() -> None:
    default = "2025 Fall"
    q1 = resolve("2024 Fall 有没有 ICS 33？", default=default)
    focus1 = {
        "terms": list(next_recent_focus_terms("", q1, None)),
        "course_ids": list(q1.course_ids),
    }
    q2 = resolve("那 2025 Fall 呢？", default=default, focus=focus1)
    focus2 = {
        "terms": list(next_recent_focus_terms("那 2025 Fall 呢？", q2, focus1)),
        "course_ids": list(q2.course_ids or tuple(focus1["course_ids"])),
    }
    q3 = resolve("对比一下这两个学期", default=default, focus=focus2)

    assert q1.canonical_terms == ("2024 Fall",)
    assert q2.canonical_terms == ("2025 Fall",)
    assert focus2 == {
        "terms": ["2024 Fall", "2025 Fall"],
        "course_ids": ["ICS33"],
    }
    assert q3.canonical_terms == ("2024 Fall", "2025 Fall")
    assert q3.source == "comparison"
    assert default == "2025 Fall"
