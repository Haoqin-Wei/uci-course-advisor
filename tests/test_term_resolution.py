from datetime import date, datetime, timedelta

import pytest

from app.terms import (
    FixedClock,
    LOS_ANGELES,
    TermCalendarError,
    TermKey,
    calculate_week2_friday_cutoff,
    parse_term_key,
    parse_term_text,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026 Fall", "2026 Fall"),
        ("Fall 2026", "2026 Fall"),
        ("2026Fall", "2026 Fall"),
        ("fall2026", "2026 Fall"),
        ("2026年秋季", "2026 Fall"),
        ("春季 2027", "2027 Spring"),
        ("2026 Summer1", "2026 Summer1"),
        ("Summer 10wk 2026", "2026 Summer10wk"),
        ("2026暑期2", "2026 Summer2"),
    ],
)
def test_canonical_term_parsing(raw: str, expected: str) -> None:
    result = parse_term_key(raw)
    assert result.kind == "single"
    assert result.terms[0].canonical_name == expected


def test_regular_sequence_crosses_calendar_year_after_fall() -> None:
    fall = TermKey(2026, "Fall")
    winter = fall.next_regular()
    spring = winter.next_regular()
    next_fall = spring.next_regular()
    assert [item.canonical_name for item in (fall, winter, spring, next_fall)] == [
        "2026 Fall",
        "2027 Winter",
        "2027 Spring",
        "2027 Fall",
    ]


def test_summer_is_explicit_only_and_not_in_regular_sequence() -> None:
    summer = parse_term_key("2027 Summer1").terms[0]
    assert summer.canonical_name == "2027 Summer1"
    with pytest.raises(ValueError, match="automatic sequence"):
        summer.next_regular()


def test_relative_terms_use_automatic_base_not_conversation_term() -> None:
    automatic = TermKey(2026, "Fall")
    result = parse_term_text("比较上学期和下学期", automatic_term=automatic)
    assert result.kind == "multi"
    assert [term.canonical_name for term in result.terms] == [
        "2026 Spring",
        "2027 Winter",
    ]


def test_current_term_marks_reset_to_auto() -> None:
    result = parse_term_text("回到当前学期", automatic_term=TermKey(2027, "Winter"))
    assert result.kind == "single"
    assert result.reset_to_auto is True
    assert result.terms[0].canonical_name == "2027 Winter"


def test_explicit_multi_term_resolution_preserves_message_order() -> None:
    result = parse_term_text("对比 Fall 2026 与 2027 Spring")
    assert result.kind == "multi"
    assert [term.canonical_name for term in result.terms] == ["2026 Fall", "2027 Spring"]


@pytest.mark.parametrize("raw", ["Spring", "秋季", "2026 Summer", "not-a-term"])
def test_invalid_or_ambiguous_term_is_structured(raw: str) -> None:
    result = parse_term_key(raw)
    assert result.kind == "error"
    assert result.error is not None
    assert result.error.code in {"invalid", "ambiguous"}


def test_relative_term_without_automatic_base_is_structured_error() -> None:
    result = parse_term_text("next quarter", require_term=True)
    assert result.kind == "error"
    assert result.error and result.error.code == "missing_base"


def test_thursday_start_uses_second_teaching_week_friday() -> None:
    cutoff = calculate_week2_friday_cutoff(date(2026, 9, 24))
    assert cutoff == datetime(2026, 10, 9, 17, 0, tzinfo=LOS_ANGELES)


@pytest.mark.parametrize(
    ("instruction_start", "expected_day"),
    [
        (date(2027, 1, 4), date(2027, 1, 15)),
        (date(2027, 3, 29), date(2027, 4, 9)),
    ],
)
def test_monday_start_week2_friday(instruction_start: date, expected_day: date) -> None:
    cutoff = calculate_week2_friday_cutoff(instruction_start)
    assert cutoff.date() == expected_day
    assert (cutoff.hour, cutoff.minute) == (17, 0)


def test_cutoff_comparison_is_exact_to_the_instant() -> None:
    cutoff = calculate_week2_friday_cutoff("2026-09-24")
    before = FixedClock(cutoff - timedelta(seconds=1)).now()
    at = FixedClock(cutoff).now()
    assert before < cutoff
    assert at >= cutoff


def test_dst_is_derived_from_los_angeles_zoneinfo() -> None:
    winter = calculate_week2_friday_cutoff("2026-01-05")
    fall = calculate_week2_friday_cutoff("2026-09-24")
    assert winter.utcoffset() == timedelta(hours=-8)
    assert fall.utcoffset() == timedelta(hours=-7)


def test_official_deadline_takes_precedence() -> None:
    official = datetime(2026, 10, 8, 16, 30, tzinfo=LOS_ANGELES)
    assert calculate_week2_friday_cutoff(
        "2026-09-24", official_deadline=official
    ) == official


def test_invalid_calendar_date_is_rejected() -> None:
    with pytest.raises(TermCalendarError, match="instruction start"):
        calculate_week2_friday_cutoff("2026-02-31")
