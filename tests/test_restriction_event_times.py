"""When an entry takes effect: the time the page gives, or the day alone.

Regression (2026-11-12). On the ICS page the December 18 row reads
"12:00pm: WebReg reopens ..." followed by the paragraph that lifts the
School/Major restriction. The time was only recognised on a line of its own,
so it was dropped, and a missing time was written as 00:00: the answer said
the restriction ends at midnight, twelve hours before the page does.
"""
from __future__ import annotations

import pytest

from app.data.restriction_timeline import (
    RestrictionEvent,
    RestrictionType,
    _find_conflicts,
    _format_effective_at,
    extract_main_content_blocks,
    is_date_only,
    parse_restriction_timeline,
)
from tests.fakes.restriction_pages import ICS_LINK, fixture_html, lookup, serve_page, sync_departments

TERM = "2027 Winter"
PAGE = fixture_html("ics_restrictions_winter2027_page.html")
NOON_DEC_18 = "2026-12-18T12:00:00-08:00"


def _parse(html: str, department: str = "COMPSCI") -> list[RestrictionEvent]:
    return parse_restriction_timeline(
        extract_main_content_blocks(html), term=TERM, department=department,
        source_url=ICS_LINK, source_role="official_link",
    )


def _row(*cells: str) -> str:
    return "<main><table><tr><td>12/18/2026</td></tr><tr><td>" + "".join(cells) + "</td></tr></table></main>"


LIFTED = "<p>School/Major restrictions are removed from most School of ICS courses:</p>"


@pytest.mark.parametrize(
    "opening",
    [
        "<p><strong>12:00pm:</strong><br>WebReg reopens for continuing registration</p>",   # COMPSCI row
        "<strong>12:00pm:</strong><p></p><p>WebReg reopens for continuing registration.</p>",  # CSE row
        "<p>12:00pm:</p>",                                                                    # time on its own
    ],
)
def test_a_time_that_opens_the_row_applies_to_the_entries_in_it(opening: str) -> None:
    events = _parse(_row(opening, LIFTED))

    lifted = next(event for event in events if event.source_position["tag"] == "p")
    assert lifted.statement.startswith("School/Major restrictions are removed")
    assert lifted.effective_at == NOON_DEC_18
    assert {event.effective_at for event in events} == {NOON_DEC_18}


def test_a_time_that_opens_a_paragraph_applies_outside_tables_too() -> None:
    events = _parse("<main><p>12/18/2026</p><p>12:00pm: WebReg reopens for continuing registration</p>"
                    + LIFTED + "</main>")

    assert [event.effective_at for event in events] == [NOON_DEC_18]


def test_a_later_time_in_the_same_row_takes_over() -> None:
    events = _parse(_row(
        "<strong>9:00am:</strong><p></p><p>Upper-Division Standing removed from most Upper-Division CS courses.</p>",
        "<p><strong>12:00pm:</strong></p>",
        "<p>Repeat restriction removed from all School of ICS courses.</p>",
    ))

    by_statement = {event.statement: event.effective_at for event in events if event.source_position["tag"] == "p"}
    assert by_statement == {
        "Upper-Division Standing removed from most Upper-Division CS courses.": "2026-12-18T09:00:00-08:00",
        "Repeat restriction removed from all School of ICS courses.": NOON_DEC_18,
    }


def test_one_rows_time_does_not_carry_into_the_next_date() -> None:
    events = _parse(
        "<main><table><tr><td>12/4/2026</td></tr><tr><td><strong>9:00am:</strong>"
        "<p>School/Major restrictions are removed from CS 145.</p></td></tr>"
        "<tr><td>12/18/2026</td></tr><tr><td>" + LIFTED + "</td></tr></table></main>"
    )

    assert {event.effective_at for event in events} == {"2026-12-04T09:00:00-08:00", "2026-12-18"}


def test_a_day_without_a_time_stays_a_day() -> None:
    events = _parse(_row(LIFTED))

    assert {event.effective_at for event in events} == {"2026-12-18"}       # not 2026-12-18T00:00
    assert is_date_only("2026-12-18") and not is_date_only(NOON_DEC_18) and not is_date_only(None)
    assert _format_effective_at("2026-12-18") == "2026-12-18（来源未注明具体时间）"
    assert "00:00" not in _format_effective_at("2026-12-18")
    assert _format_effective_at(NOON_DEC_18).startswith("2026-12-18 12:00")
    assert _format_effective_at("Monday, August 24th, 2026 at noon") == "Monday, August 24th, 2026 at noon"


def _event(event_id: str, effective_at: str) -> RestrictionEvent:
    return RestrictionEvent(event_id=event_id, restriction_type=RestrictionType.SCHOOL_MAJOR, action="removed",
                            effective_at=effective_at, term=TERM, department="COMPSCI")


def test_a_day_and_a_time_on_that_day_do_not_conflict() -> None:
    day, noon = _event("a", "2026-12-18"), _event("b", NOON_DEC_18)

    assert _find_conflicts([day, noon], noon) == []
    assert _find_conflicts([day, noon], day) == []
    other_day = _find_conflicts([_event("c", "2026-12-17"), noon], noon)
    assert other_day and other_day[0]["values"] == ["2026-12-17", NOON_DEC_18]
    other_time = _find_conflicts([_event("d", "2026-12-18T09:00:00-08:00"), noon], noon)
    assert other_time and len(other_time[0]["values"]) == 2


def test_real_page_gives_noon_for_every_departments_lifting(monkeypatch) -> None:
    departments = ("COMPSCI", "CSE", "GDIM", "I&C SCI", "IN4MATX", "STATS", "SWE")
    sync_departments("2027_Winter", *departments)
    serve_page(monkeypatch, PAGE)

    for department in departments:
        result = lookup(TERM, department)
        primary = result["verified_facts"]["primary"]
        assert primary["restriction_type"] == "school_major" and primary["action"] == "removed", department
        assert primary["effective_at"] == NOON_DEC_18, department
        assert "2026-12-18 12:00" in result["verified_facts"]["summary_markdown"], department


def test_real_page_never_invents_midnight() -> None:
    events = _parse(PAGE)

    stamped = [event.effective_at for event in events if event.effective_at]
    assert stamped and not [value for value in stamped if "T00:00" in value]
    # The first row of a table (November 16) has a date and no time on the page.
    first_day = [event.effective_at for event in events if (event.effective_at or "").startswith("2026-11-16")]
    assert first_day and set(first_day) == {"2026-11-16"}


def test_agent_prompt_forbids_adding_a_time_to_a_bare_date() -> None:
    from app.llm.adapter import AGENT_SYSTEM_PROMPT

    assert "date without a time" in " ".join(AGENT_SYSTEM_PROMPT.split())
