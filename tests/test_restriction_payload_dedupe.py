"""A second department must not hand the model the same events again.

I&C SCI and COMPSCI both link to the ICS enrollment-restrictions page. Asking
about both in one turn used to put every timeline event in front of the model
twice (2026-11-12 log: ~26.8k prompt tokens).
"""
from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy

from app.agent import loop as agent_loop
from app.agent.restriction_payload import SCHOOL_WIDE, RestrictionPayloadDeduper
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response
from tests.fakes.restriction_pages import ICS_LINK, fixture_html, serve_page, sync_departments

TERM = "2027 Winter"


def _page_result(department: str, nor_statement: str, url: str = ICS_LINK) -> dict:
    """A result on a page layout the department filter does not recognise."""
    return {
        "ok": True, "department": department, "term": TERM,
        "linked_pages": {"pages": [{"url": url, "timeline_events": [
            {"event_id": "school_major-1", "statement": "same for everyone", "retrieved_at": department},
            {"event_id": "new_only-1", "statement": nor_statement},
        ]}]},
    }


def _bundle_result(department: str, primary: str) -> dict:
    return {
        "ok": True, "department": department, "term": TERM,
        "evidence_bundle": {
            "primary_event_id": primary, "related_event_ids": [], "conflicts": [],
            "events": [{"event_id": "p1-school_major-1", "statement": "removed 12/18"},
                       {"event_id": "p1-new_only-2", "statement": "NOR removed 12/1"}],
        },
    }


def test_first_result_is_passed_through_untouched() -> None:
    first = _page_result("I&C SCI", "NOR removed")

    view, omitted = RestrictionPayloadDeduper().model_view(first)

    assert view is first and omitted is None


def test_identical_page_events_point_back_to_the_first_result() -> None:
    deduper = RestrictionPayloadDeduper()
    deduper.model_view(_page_result("I&C SCI", "NOR removed for I&C SCI"))
    second = _page_result("COMPSCI", "NOR removed for COMPSCI")
    untouched = deepcopy(second)

    view, omitted = deduper.model_view(second)

    assert second == untouched                                   # UI, audit and checks keep the full result
    page = view["linked_pages"]["pages"][0]
    assert [event["event_id"] for event in page["timeline_events"]] == ["new_only-1"]   # differs, so it stays
    left_out = page["timeline_events_omitted"]
    assert left_out["event_ids"] == ["school_major-1"]           # read time alone is not a difference
    assert "I&C SCI, 2027 Winter" in left_out["note"]
    assert omitted["page_events_omitted"] == 1 and omitted["bundle_events_omitted"] == 0


def test_events_the_answer_rests_on_are_repeated_in_full() -> None:
    deduper = RestrictionPayloadDeduper()
    deduper.model_view(_bundle_result("I&C SCI", primary="p1-school_major-1"))

    view, omitted = deduper.model_view(_bundle_result("COMPSCI", primary="p1-school_major-1"))

    bundle = view["evidence_bundle"]
    assert [event["event_id"] for event in bundle["events"]] == ["p1-school_major-1"]
    assert bundle["events_omitted"]["event_ids"] == ["p1-new_only-2"]
    assert omitted["bundle_events_omitted"] == 1


def test_same_event_on_a_different_page_is_not_treated_as_repeated() -> None:
    deduper = RestrictionPayloadDeduper()
    deduper.model_view(_page_result("MATH", "removed", url="https://a.uci.edu/x"))
    second = _page_result("MATH", "removed", url="https://b.uci.edu/y")

    assert deduper.model_view(second) == (second, None)


def test_failed_results_are_passed_through() -> None:
    failed = {"ok": False, "error_code": "department_required"}
    assert RestrictionPayloadDeduper().model_view(failed) == (failed, None)
    assert RestrictionPayloadDeduper().model_view("not a dict") == ("not a dict", None)


async def _collect(events) -> list[dict]:
    return [event async for event in events]


def test_agent_gives_the_model_each_department_once(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.agent.loop")
    sync_departments("2027_Winter", "I&C SCI", "COMPSCI")
    session = serve_page(monkeypatch, fixture_html("ics_restrictions_winter2027_page.html"))
    args = {"term": TERM, "restriction_type": "major_restriction"}
    client = ScriptedLLMClient(
        tool_response(
            tool_call("get_department_restrictions", {**args, "department": "I&C SCI"}, call_id="c1", index=0),
            tool_call("get_department_restrictions", {**args, "department": "COMPSCI"}, call_id="c2", index=1),
        ),
        text_response("Both open to other majors on December 18."),
    )

    events = asyncio.run(_collect(agent_loop.run_agent(
        [{"role": "user", "content": "ICS"}], client=client, model="fake-model",
        user_id="student_001", term=TERM, response_language="zh",
    )))

    tool_messages = {m["tool_call_id"]: m["content"]
                     for m in client.calls[1].messages if m.get("role") == "tool"}
    first, second = json.loads(tool_messages["c1"]), json.loads(tool_messages["c2"])

    def departments(result: dict) -> set:
        return {event["department"] for event in result["evidence_bundle"]["events"]}

    assert departments(first) == {"I&C SCI", SCHOOL_WIDE}         # its own timeline plus the general notes
    assert departments(second) == {"COMPSCI"}                    # general notes were already given
    assert second["evidence_bundle"]["events_omitted"]["event_ids"]
    for result in (first, second):
        assert "timeline_events" not in result["linked_pages"]["pages"][0]
        assert result["evidence_bundle"]["primary_event_id"] in {
            event["event_id"] for event in result["evidence_bundle"]["events"]}
        assert "STATS" in result["evidence_bundle"]["other_departments_omitted"]["events_by_department"]
    assert len(tool_messages["c1"]) + len(tool_messages["c2"]) < 45_000    # was about 200,000 characters
    assert session.calls == [ICS_LINK]                           # and the page itself was requested once
    # The UI still receives each call's complete source list.
    done = [event for event in events if event["type"] == "tool_call_done"]
    assert [len(event["fetch_summary"]) for event in done] == [2, 2]
    assert caplog.text.count("event=restriction_payload_trimmed") == 2
    client.assert_exhausted()
