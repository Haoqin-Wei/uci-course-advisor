"""One restriction result is narrowed to the department that was asked about.

The fixture is the ICS enrollment-restrictions page for Winter 2027 as a
browser showed it on 2026-11-12, reduced to its main content: nine school-wide
notes followed by a timeline table for each of seven departments. Looking up
one department used to hand the model all seven timelines, twice.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from app.agent.restriction_payload import SCHOOL_WIDE, RestrictionPayloadDeduper, focus_on_department
from app.data.restriction_timeline import extract_main_content_blocks, parse_restriction_timeline
from tests.fakes.restriction_pages import ICS_LINK, fixture_html, lookup, serve_page, sync_departments

TERM = "2027 Winter"
PAGE = fixture_html("ics_restrictions_winter2027_page.html")
ON_PAGE = ("COMPSCI", "CSE", "GDIM", "I&C SCI", "IN4MATX", "STATS", "SWE")


@pytest.fixture
def page(monkeypatch):
    sync_departments("2027_Winter", *ON_PAGE, "MATH")
    return serve_page(monkeypatch, PAGE)


def _section(event: dict):
    return event["source_position"]["section"]


def _bundle_events(result: dict) -> list[dict]:
    return result["evidence_bundle"]["events"]


def test_every_entry_records_the_department_section_it_sits_under() -> None:
    events = parse_restriction_timeline(
        extract_main_content_blocks(PAGE), term=TERM, department="COMPSCI",
        source_url=ICS_LINK, source_role="official_link",
    )

    sections = [event.source_position["section"] for event in events]
    assert set(sections) == {None, *ON_PAGE}
    first_section = next(index for index, section in enumerate(sections) if section)
    assert first_section > 0 and all(section is None for section in sections[:first_section])
    assert all(section for section in sections[first_section:])
    # Above the sections an entry is still attributed to the queried department.
    assert events[0].department == "COMPSCI" and "lifted from courses except" in events[0].statement
    assert {event.department for event in events if event.source_position["section"] == "SWE"} == {"SWE"}


def test_a_department_gets_its_own_timeline_and_the_school_wide_notes(page) -> None:
    result = lookup(TERM, "COMPSCI")
    untouched = deepcopy(result)
    all_events = _bundle_events(result)
    assert {_section(event) for event in all_events} == {None, *ON_PAGE}

    view, info = focus_on_department(result)

    assert result == untouched                                   # UI, audit and checks keep the full result
    shown = _bundle_events(view)
    assert {_section(event) for event in shown} == {None, "COMPSCI"}
    own = [event for event in all_events if _section(event) == "COMPSCI"]
    assert [event["event_id"] for event in shown if _section(event)] == [event["event_id"] for event in own]
    assert [event for event in shown if _section(event)] == own  # shown exactly as parsed
    general = [event for event in shown if _section(event) is None]
    assert general and {event["department"] for event in general} == {SCHOOL_WIDE}
    assert SCHOOL_WIDE in view["evidence_bundle"]["school_wide_note"]
    bundle = view["evidence_bundle"]
    assert bundle["primary_event_id"] in {event["event_id"] for event in shown}
    left_out = bundle["other_departments_omitted"]
    assert set(left_out["events_by_department"]) == set(ON_PAGE) - {"COMPSCI"}
    assert sum(left_out["events_by_department"].values()) == len(all_events) - len(shown)
    assert info["events_kept"] == len(shown) and info["events_other_departments"] == len(all_events) - len(shown)
    # The page's own copy of the same events is no longer repeated.
    page_view = view["linked_pages"]["pages"][0]
    assert "timeline_events" not in page_view
    assert page_view["timeline_event_count"] == len(result["linked_pages"]["pages"][0]["timeline_events"])
    assert view["verified_facts"] == result["verified_facts"]
    assert view["fetch_summary"] == result["fetch_summary"]
    assert info["focus_chars_after"] * 4 < info["focus_chars_before"]


def test_entries_are_matched_by_section_not_by_the_names_in_their_text(page) -> None:
    # The page's SWE table has a row that says "All IN4MATX courses ..."; it is
    # an SWE entry all the same, and COMPSCI rows talk about SWE 141 and CSE.
    swe_view, _ = focus_on_department(lookup(TERM, "SWE"))
    informatics_view, _ = focus_on_department(lookup(TERM, "IN4MATX"))

    stray = [event for event in _bundle_events(swe_view)
             if _section(event) == "SWE" and "All IN4MATX courses" in event["statement"]]
    assert stray
    assert {_section(event) for event in _bundle_events(informatics_view)} == {None, "IN4MATX"}
    assert {_section(event) for event in _bundle_events(swe_view)} == {None, "SWE"}


def test_ics_means_the_ics_department_not_the_whole_school(page) -> None:
    view, _ = focus_on_department(lookup(TERM, "I&C SCI"))

    assert {_section(event) for event in _bundle_events(view)} == {None, "I&C SCI"}
    assert "COMPSCI" in view["evidence_bundle"]["other_departments_omitted"]["events_by_department"]


def test_a_course_question_also_gets_entries_that_name_the_course_elsewhere(page) -> None:
    # COMPSCI 121 is cross-listed with SWE 141 and appears in the SWE table too.
    plain, _ = focus_on_department(lookup(TERM, "COMPSCI"))
    with_course, _ = focus_on_department(lookup(TERM, "COMPSCI", course_id="COMPSCI 121"))

    assert {_section(event) for event in _bundle_events(plain)} == {None, "COMPSCI"}
    borrowed = [event for event in _bundle_events(with_course) if _section(event) not in (None, "COMPSCI")]
    assert borrowed and {_section(event) for event in borrowed} == {"SWE"}
    assert all("121" in str(event["course_scope"]) + str(event["exceptions"]) for event in borrowed)


def test_a_department_without_its_own_section_is_left_whole(page) -> None:
    result = lookup(TERM, "MATH")
    assert result["ok"] is True and len(_bundle_events(result)) > 20

    assert focus_on_department(result) == (result, None)


def test_a_page_without_department_sections_is_left_whole(monkeypatch) -> None:
    sync_departments("2027_Winter", "COMPSCI")
    serve_page(monkeypatch, "<main><p>12/1/2026</p><p>12:00pm</p>"
                            "<p>School/Major restrictions are removed.</p></main>")
    result = lookup(TERM, "COMPSCI")
    assert result["evidence_bundle"]["primary_event_id"]

    assert focus_on_department(result) == (result, None)


def test_nothing_is_narrowed_without_a_primary_event(page) -> None:
    result = lookup(TERM, "COMPSCI")
    result["evidence_bundle"]["primary_event_id"] = None

    assert focus_on_department(result) == (result, None)
    assert focus_on_department({"ok": False}) == ({"ok": False}, None)


def test_second_department_reuses_the_school_wide_notes_already_given(page) -> None:
    deduper = RestrictionPayloadDeduper()
    first, first_info = deduper.model_view(lookup(TERM, "I&C SCI"))
    second, second_info = deduper.model_view(lookup(TERM, "COMPSCI"))

    assert "events_omitted" not in first["evidence_bundle"] and first_info["school_wide_events"] >= 1
    general = [event["event_id"] for event in _bundle_events(first) if _section(event) is None]
    assert second["evidence_bundle"]["events_omitted"]["event_ids"] == general
    assert {_section(event) for event in _bundle_events(second)} == {"COMPSCI"}
    assert second_info["bundle_events_omitted"] == len(general)
    assert page.calls == [ICS_LINK]                              # the page itself was requested once
