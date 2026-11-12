"""A second department must not hand the model the same linked page again.

I&C SCI and COMPSCI both link to the ICS enrollment-restrictions page. Asking
about both in one turn used to put every timeline event in front of the model
twice (2026-11-12 log: ~26.8k prompt tokens).
"""
from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from app.agent import loop as agent_loop
from app.agent import tools as agent_tools
from app.agent.restriction_payload import RestrictionPayloadDeduper
from app.catalog.sync import sync_term
from app.data import websoc_workflow
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response

ICS_LINK = "http://ics.uci.edu/course-enrollment-restrictions/"
PAGE_HTML = (Path(__file__).parent / "fixtures" / "websoc" / "ics_restrictions_live_layout.html").read_text()
TERM = "2026 Fall"


def _department(code: str, name: str) -> dict:
    section = {"sectionCode": "34220" if code == "COMPSCI" else "35000", "sectionType": "Lec", "sectionNum": "A",
               "units": "4", "status": "OPEN", "instructors": ["DILLENCOURT, M."], "maxCapacity": "165",
               "numCurrentlyEnrolled": {"totalEnrolled": "0", "sectionEnrolled": ""}, "isCancelled": False,
               "meetings": [{"timeIsTBA": True}], "finalExam": {}}
    return {
        "deptCode": code, "deptName": name,
        "deptComment": (f"<p><b>RESTRICTIONS:</b> {code} courses will initially be restricted to majors in the "
                        "Donald Bren School of Information and Computer Sciences ONLY. For restriction details "
                        f'and timeline: <a href="{ICS_LINK}">{ICS_LINK}</a></p>'),
        "courseNumberRangeComments": [], "sectionCodeRangeComments": [],
        "courses": [{"deptCode": code, "courseNumber": "161", "courseTitle": "ALGORITHMS",
                     "courseComment": "", "sections": [section]}],
    }


def _sync_two_departments() -> None:
    data = {"schools": [{
        "schoolName": "Donald Bren School of Information and Computer Sciences", "schoolComment": "",
        "departments": [_department("I&C SCI", "Information and Computer Science"),
                        _department("COMPSCI", "Computer Science")],
    }]}
    assert sync_term("2026_Fall", fetcher=lambda term: SimpleNamespace(
        ok=True, status="ok", error=None, data=data)).ok


class _Page:
    status_code = 200
    url = ICS_LINK
    headers = {"content-type": "text/html"}
    text = PAGE_HTML

    def raise_for_status(self):
        return None


class _Session:
    def __init__(self):
        self.calls = []

    def get(self, url, **_kwargs):
        self.calls.append(url)
        return _Page()


def _use_fixture_page(monkeypatch) -> _Session:
    session = _Session()
    real = websoc_workflow.fetch_linked_official_pages
    monkeypatch.setattr(agent_tools.websoc_workflow, "fetch_linked_official_pages",
                        lambda result: real(result, session=session))
    return session


def _lookup(department: str) -> dict:
    return agent_tools.dispatch(
        "get_department_restrictions",
        {"term": TERM, "department": department, "restriction_type": "major_restriction"},
        context={"allowed_query_terms": [TERM]},
    )


def _events(result: dict) -> list[dict]:
    return result["linked_pages"]["pages"][0]["timeline_events"]


def test_first_result_is_passed_through_untouched(monkeypatch) -> None:
    _sync_two_departments()
    _use_fixture_page(monkeypatch)
    first = _lookup("I&C SCI")
    assert len(_events(first)) >= 3

    view, omitted = RestrictionPayloadDeduper().model_view(first)

    assert view is first and omitted is None


def test_second_department_points_back_to_the_copy_already_given(monkeypatch) -> None:
    _sync_two_departments()
    _use_fixture_page(monkeypatch)
    first, second = _lookup("I&C SCI"), _lookup("COMPSCI")
    untouched = deepcopy(second)
    deduper = RestrictionPayloadDeduper()
    deduper.model_view(first)

    view, omitted = deduper.model_view(second)

    assert second == untouched                                   # UI, audit and checks keep the full result
    page = view["linked_pages"]["pages"][0]
    left_out = page["timeline_events_omitted"]
    assert omitted["page_events_omitted"] == len(left_out["event_ids"]) > 0
    assert "I&C SCI, 2026 Fall" in left_out["note"]
    assert omitted["chars_after"] < omitted["chars_before"]
    # Every event is either shown now or was shown, identically, the first time.
    first_by_id = {event["event_id"]: event for event in _events(first)}
    shown = {event["event_id"] for event in page["timeline_events"]}
    assert shown | set(left_out["event_ids"]) == {event["event_id"] for event in _events(second)}
    for event in _events(second):
        if event["event_id"] in left_out["event_ids"]:
            earlier = first_by_id[event["event_id"]]
            assert {**event, "retrieved_at": None} == {**earlier, "retrieved_at": None}
    # The answer to this department's own question stays in full.
    bundle = view["evidence_bundle"]
    kept_ids = {event["event_id"] for event in bundle["events"]}
    assert bundle["primary_event_id"] in kept_ids
    assert set(bundle["related_event_ids"]) <= kept_ids
    assert view["verified_facts"] == second["verified_facts"]
    assert view["fetch_summary"] == second["fetch_summary"]


def test_an_event_that_differs_is_never_left_out() -> None:
    def result(department: str, statement: str) -> dict:
        return {
            "ok": True, "department": department, "term": TERM,
            "linked_pages": {"pages": [{"url": ICS_LINK, "timeline_events": [
                {"event_id": "school_major-1", "statement": "same for everyone", "retrieved_at": department},
                {"event_id": "new_only-1", "statement": statement},
            ]}]},
        }

    deduper = RestrictionPayloadDeduper()
    deduper.model_view(result("I&C SCI", "NOR removed for I&C SCI"))
    view, omitted = deduper.model_view(result("COMPSCI", "NOR removed for COMPSCI"))

    page = view["linked_pages"]["pages"][0]
    assert [event["event_id"] for event in page["timeline_events"]] == ["new_only-1"]
    assert page["timeline_events_omitted"]["event_ids"] == ["school_major-1"]   # read time alone is not a difference
    assert omitted["page_events_omitted"] == 1 and omitted["bundle_events_omitted"] == 0


def test_same_event_on_a_different_page_is_not_treated_as_repeated() -> None:
    def result(url: str) -> dict:
        return {"ok": True, "department": "MATH", "term": TERM, "linked_pages": {"pages": [
            {"url": url, "timeline_events": [{"event_id": "school_major-1", "statement": "removed"}]}]}}

    deduper = RestrictionPayloadDeduper()
    deduper.model_view(result("https://a.uci.edu/x"))
    second = result("https://b.uci.edu/y")

    assert deduper.model_view(second) == (second, None)


def test_failed_results_are_passed_through() -> None:
    failed = {"ok": False, "error_code": "department_required"}
    assert RestrictionPayloadDeduper().model_view(failed) == (failed, None)
    assert RestrictionPayloadDeduper().model_view("not a dict") == ("not a dict", None)


async def _collect(events) -> list[dict]:
    return [event async for event in events]


def test_agent_gives_the_model_the_shared_page_once(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.agent.loop")
    _sync_two_departments()
    session = _use_fixture_page(monkeypatch)
    args = {"term": TERM, "restriction_type": "major_restriction"}
    client = ScriptedLLMClient(
        tool_response(
            tool_call("get_department_restrictions", {**args, "department": "I&C SCI"}, call_id="c1", index=0),
            tool_call("get_department_restrictions", {**args, "department": "COMPSCI"}, call_id="c2", index=1),
        ),
        text_response("Both open to other majors in September."),
    )

    events = asyncio.run(_collect(agent_loop.run_agent(
        [{"role": "user", "content": "ICS"}], client=client, model="fake-model",
        user_id="student_001", term=TERM, response_language="zh",
    )))

    tool_messages = {m["tool_call_id"]: json.loads(m["content"])
                     for m in client.calls[1].messages if m.get("role") == "tool"}
    first, second = tool_messages["c1"], tool_messages["c2"]
    assert first["department"] == "I&C SCI" and second["department"] == "COMPSCI"
    first_page, second_page = first["linked_pages"]["pages"][0], second["linked_pages"]["pages"][0]
    assert "timeline_events_omitted" not in first_page and len(first_page["timeline_events"]) >= 3
    assert second_page["timeline_events_omitted"]["event_ids"]
    assert len(second_page["timeline_events"]) < len(first_page["timeline_events"])
    assert second["evidence_bundle"]["primary_event_id"] in {
        event["event_id"] for event in second["evidence_bundle"]["events"]}
    assert session.calls == [ICS_LINK]                           # and the page itself was requested once
    # The UI still receives each call's complete source list.
    done = [event for event in events if event["type"] == "tool_call_done"]
    assert [len(event["fetch_summary"]) for event in done] == [2, 2]
    assert "event=restriction_payload_deduplicated" in caplog.text
    client.assert_exhausted()
