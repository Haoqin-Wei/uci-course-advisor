"""Offline regressions for preserving actual WebSoc evidence at tool boundaries."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.catalog.types import CourseRef, SectionRecord
from app.data import anteater, db
from app.data.websoc_payload import compact_comment_contexts, websoc_comment_metadata


def _swe43_response():
    """Concise excerpts of the public Fall 2026 SWE43 response saved 2026-11-03.

    Source: /v2/rest/websoc?year=2026&quarter=Fall&department=SWE&courseNumber=43.
    The three cancelled sections really report status FULL, not CANCELLED.
    """
    sections = []
    for code, number, hour in (("37513", "B3", 12), ("37515", "B5", 14), ("37512", "B2", 15)):
        sections.append({
            "sectionCode": code,
            "sectionNum": number,
            "sectionType": "Dis",
            "status": "FULL",
            "isCancelled": True,
            "sectionComment": f"<p>***  CANCELLED  *** Course Code {code}</p>",
            "meetings": [{"timeIsTBA": False, "bldg": ["TBA "], "days": "W",
                          "startTime": {"hour": hour, "minute": 0},
                          "endTime": {"hour": hour, "minute": 50}}],
            "maxCapacity": "60",
            "numCurrentlyEnrolled": {"totalEnrolled": "0", "sectionEnrolled": ""},
            "numWaitlistCap": "0",
            "numOnWaitlist": "",
            "updatedAt": "2026-11-03T22:56:25.815Z",
            "webURL": "",
        })
    return {"ok": True, "data": {"schools": [{
        "schoolName": "Donald Bren School of Information and Computer Sciences",
        "schoolComment": '<p>For ICS updates, visit <a href="https://ics.uci.edu/course-enrollment-restrictions/">restrictions</a>.</p>',
        "departments": [{
            "deptCode": "SWE", "deptName": "Software Engineering",
            "deptComment": "<p>Most SWE courses will be initially restricted.</p>",
            "courseNumberRangeComments": ["<p>SWE course numbers 200 - 299: graduate restrictions.</p>"],
            "sectionCodeRangeComments": [],
            "courses": [{
                "deptCode": "SWE", "courseId": "SWE43", "courseNumber": "43",
                "courseTitle": "INTRO SOFTWARE ENGR",
                "courseComment": '<p>Previously offered as INF 43, &quot;Intro Software Engr&quot;. Students with prior credit for INF 43, &quot;Intro Software Engr&quot; may not take SWE 43, &quot;Intro Software Engr&quot;.</p>',
                "sections": sections,
            }],
        }],
    }]}}


def test_real_cancelled_sections_and_all_comment_scopes_survive_conversion():
    raw = _swe43_response()
    flat = anteater._flatten_websoc_sections(raw["data"])
    converted = [db._api_live_section_to_dict(sec, retrieved_at="2026-11-03T23:00:00Z") for sec in flat]

    assert [sec["section_code"] for sec in converted] == ["37513", "37515", "37512"]
    for original, sec in zip(flat, converted):
        assert sec["status"] == "FULL"
        assert sec["is_cancelled"] is True
        assert sec["cancellation_status"] == "cancelled"
        assert sec["section_comment"] == original["sectionComment"]
        assert sec["meetings"] == original["meetings"]
        assert "Previously offered as INF 43" in sec["comments"]["course"]["comment"]
        assert sec["comments"]["course"]["course_id"] == "SWE43"
        assert sec["comments"]["school"]["name"].startswith("Donald Bren")
        assert "https://ics.uci.edu/" in sec["comments"]["school"]["comment"]
        assert sec["comments"]["department"]["code"] == "SWE"
        assert sec["comments"]["department"]["course_number_range_comments"] == [
            "<p>SWE course numbers 200 - 299: graduate restrictions.</p>"
        ]
        assert sec["comments"]["department"]["section_code_range_comments"] == []
        assert "webURL" not in sec and "web_url" not in sec


@pytest.mark.parametrize("raw, expected, status", [
    (True, True, "cancelled"), (False, False, "not_cancelled"),
    (None, None, "unknown"), ("false", None, "unknown"),
    ("true", None, "unknown"), (0, None, "unknown"), (1, None, "unknown"),
])
def test_cancellation_requires_actual_source_boolean(raw, expected, status):
    sec = db._api_section_to_dict({"status": "FULL", "isCancelled": raw})
    assert sec["is_cancelled"] is expected
    assert sec["cancellation_status"] == status


def test_missing_cancellation_and_comments_do_not_establish_negative_facts():
    sec = db._api_section_to_dict({"sectionCode": "12345", "status": "OPEN"})
    assert sec["is_cancelled"] is None
    assert sec["cancellation_status"] == "unknown"
    assert sec["comments"] is None
    assert sec["section_comment"] is None
    assert sec["meetings"] is None
    assert sec["time_is_tba"] is True


@pytest.mark.parametrize("first", [
    {"days": "M", "startTime": {"hour": 9}, "endTime": {"hour": 10, "minute": 0}},
    {"days": "M", "startTime": {"hour": "9", "minute": 0}, "endTime": {"hour": 10, "minute": 0}},
    {"days": "M", "startTime": {"hour": 9, "minute": 60}, "endTime": {"hour": 10, "minute": 0}},
    {"days": "M", "startTime": {"hour": True, "minute": 0}, "endTime": {"hour": 10, "minute": 0}},
    {"days": "M", "startTime": {"hour": 9, "minute": 0}, "endTime": {"hour": 8, "minute": 0}},
    "malformed source meeting",
])
def test_bad_first_meeting_remains_unknown_without_dropping_later_meetings(first):
    later = {"days": "F", "timeIsTBA": False, "startTime": {"hour": 13, "minute": 0},
             "endTime": {"hour": 13, "minute": 50}, "bldg": ["ALP 2600"]}
    raw = {"sectionCode": "37500", "isCancelled": False, "meetings": [first, later]}
    converted = db._api_section_to_dict(raw)
    assert converted["meetings"] == [first, later]
    assert converted["time_is_tba"] is True
    assert converted["start_time"] is None
    assert converted["end_time"] is None
    assert converted["time_display"] is None


def test_explicit_tba_flag_prevents_a_verified_time_display():
    raw = {"meetings": [{"days": "M", "timeIsTBA": True,
                         "startTime": {"hour": 9, "minute": 0},
                         "endTime": {"hour": 10, "minute": 0}}]}
    converted = db._api_section_to_dict(raw)
    assert converted["meetings"] == raw["meetings"]
    assert converted["time_is_tba"] is True
    assert converted["time_display"] is None


def test_complete_meetings_and_scoped_comments_are_independent_copies():
    raw = _swe43_response()
    course = raw["data"]["schools"][0]["departments"][0]["courses"][0]
    second_meeting = {"timeIsTBA": True, "days": "TBA", "startTime": None,
                      "endTime": None, "bldg": ["ONLINE"]}
    course["sections"][0]["meetings"].append(second_meeting)
    flat = anteater._flatten_websoc_sections(raw["data"])
    sec = db._api_section_to_dict(flat[0])
    assert sec["meetings"] == course["sections"][0]["meetings"]
    assert sec["start_time"] == "12:00"  # first-meeting compatibility fields
    assert sec["meetings"][1]["timeIsTBA"] is True
    sec["meetings"][0]["days"] = "M"
    sec["comments"]["course"]["comment"] = "changed"
    assert flat[0]["meetings"][0]["days"] == "W"
    flat[0]["comments"]["department"]["course_number_range_comments"].clear()
    assert flat[1]["comments"]["department"]["course_number_range_comments"]
    assert course["sections"][0]["meetings"][0]["days"] == "W"


@pytest.mark.parametrize("live", [False, True])
def test_fetch_and_db_envelopes_preserve_comments_meetings_and_requested_term(monkeypatch, live):
    raw = _swe43_response()
    monkeypatch.setattr(anteater, "_get_json", lambda *_args, **_kwargs: deepcopy(raw))
    monkeypatch.setattr(db, "get_term_coverage", lambda _term: {"coverage_status": "unavailable"})
    monkeypatch.setattr(db, "get_catalog", lambda _term: None)
    monkeypatch.setattr(db.websoc_workflow, "fetch_websoc_course_offering", lambda **_kwargs: {
        "ok": False, "offering_status": "unavailable", "error_code": "network_error",
    })
    # This regression concerns source payloads, not optional pairing lookups.
    monkeypatch.setattr(db, "_pairing_if_enabled", lambda _course, _term, result: result)
    result = (db.get_live_sections("SWE43", "2026 Fall", force_refresh=True) if live
              else db.get_sections("SWE43", "2026 Fall"))
    assert db._to_term(result["term"]) == db._to_term("2026 Fall")
    assert result["course_id"] == "SWE 43"
    assert result["source"] == ("live_anteater_websoc" if live else "api")
    assert len(result["sections"]) == 3
    assert all(s["is_cancelled"] is True for s in result["sections"])
    assert all(s["comments"]["course"]["course_id"] == "SWE43" for s in result["sections"])
    assert all(s["meetings"][0]["timeIsTBA"] is False for s in result["sections"])


def test_local_section_serialization_retains_optional_source_payload():
    existing = SectionRecord(section_id="2026_Fall_37513", course=CourseRef("SWE", "43"),
                             section_code="37513", term_id="2026_Fall", is_cancelled=True)
    # Supports older records that predate the newly optional dataclass fields.
    values = {name: getattr(existing, name) for name in existing.__dataclass_fields__}
    values.update(seats_open=existing.seats_open,
                  meetings=[{"days": "W", "timeIsTBA": True}],
                  comments={"course": {"course_id": "SWE43", "comment": "original"}},
                  section_comment="cancelled source note")
    result = db._section_record_to_dict(SimpleNamespace(**values))
    assert result["is_cancelled"] is True
    assert result["cancellation_status"] == "cancelled"
    assert result["meetings"] == values["meetings"]
    assert result["comments"] == values["comments"]
    assert result["section_comment"] == "cancelled source note"


def test_tool_comment_compaction_is_lossless_deduplicated_and_nonmutating():
    sections = [db._api_section_to_dict(sec) for sec in
                anteater._flatten_websoc_sections(_swe43_response()["data"])]
    original = {"term": "2026 Fall", "source": "api", "sections": sections}
    before = deepcopy(original)
    compacted = compact_comment_contexts(original)
    assert len(compacted["comment_contexts"]) == 1
    assert compacted["term"] == original["term"]
    assert compacted["source"] == original["source"]
    for full, compact in zip(sections, compacted["sections"]):
        assert "comments" not in compact
        assert compacted["comment_contexts"][compact["comment_context_id"]] == full["comments"]
        assert compact["section_comment"] == full["section_comment"]
        assert compact["meetings"] == full["meetings"]
    assert original == before
    assert compact_comment_contexts(compacted) == compacted


def test_tool_comment_compaction_shares_one_bank_across_nested_courses():
    section = db._api_section_to_dict(anteater._flatten_websoc_sections(_swe43_response()["data"])[0])
    another_course = deepcopy(section)
    another_course["comments"]["course"]["course_id"] = "SWE113"
    original = {"term": "2026 Fall", "courses": [
        {"course_id": "SWE43", "sections": [section, deepcopy(section)]},
        {"course_id": "SWE113", "sections": [another_course]},
    ]}
    compacted = compact_comment_contexts(original)
    assert len(compacted["comment_contexts"]) == 2
    ids = [sec["comment_context_id"] for course in compacted["courses"] for sec in course["sections"]]
    assert ids[0] == ids[1] and ids[0] != ids[2]
    for full_course, compact_course in zip(original["courses"], compacted["courses"]):
        for full, compact in zip(full_course["sections"], compact_course["sections"]):
            assert compacted["comment_contexts"][compact["comment_context_id"]] == full["comments"]
    assert compact_comment_contexts(compacted) == compacted


def test_agent_model_tool_json_keeps_scoped_evidence_and_all_meetings(monkeypatch):
    import asyncio
    import json

    from app import config
    from app.agent import loop as agent_loop
    from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response

    raw = _swe43_response()
    course = raw["data"]["schools"][0]["departments"][0]["courses"][0]
    course["sections"][0]["meetings"].append({
        "days": "F", "timeIsTBA": True, "bldg": ["TBA"],
        "startTime": None, "endTime": None,
    })
    course["sections"][0]["webURL"] = "https://canvas.eee.uci.edu/example/assignments/syllabus"
    monkeypatch.setattr(anteater, "_get_json", lambda *_args, **_kwargs: deepcopy(raw))
    monkeypatch.setattr(db, "get_term_coverage", lambda _term: {"coverage_status": "unavailable"})
    monkeypatch.setattr(config, "dynamic_prefilter_enabled", lambda: False)
    monkeypatch.setattr(config, "recommendation_ranking_enabled", lambda: False)
    monkeypatch.setattr("app.recommendations.pairing.contract_enabled", lambda: False)
    client = ScriptedLLMClient(
        tool_response(tool_call("get_live_sections", {
            "course_id": "SWE43", "term": "Fall 2026", "force_refresh": True,
        }, call_id="payload_evidence")),
        text_response("These source records contain cancelled sections."),
    )
    messages = [{"role": "user", "content": "Show SWE43's Fall 2026 section evidence."}]

    async def collect():
        return [event async for event in agent_loop.run_agent(
            messages, client=client, model="fake-model", user_id="student_001", term="Fall 2026",
        )]

    asyncio.run(collect())
    payload = json.loads(next(row["content"] for row in messages if row.get("role") == "tool"))
    assert db._to_term(payload["term"]) == db._to_term("Fall 2026")
    assert len(payload["comment_contexts"]) == 1
    first = payload["sections"][0]
    assert first["is_cancelled"] is True
    assert first["cancellation_status"] == "cancelled"
    assert len(first["meetings"]) == 2
    assert first["meetings"][1]["timeIsTBA"] is True
    evidence = payload["comment_contexts"][first["comment_context_id"]]
    assert "Previously offered as INF 43" in evidence["course"]["comment"]
    assert "200 - 299" in evidence["department"]["course_number_range_comments"][0]
    assert "CANCELLED" in first["section_comment"]
    assert "webURL" not in first and "web_url" not in first
    client.assert_exhausted()


@pytest.mark.parametrize("live", [False, True])
def test_zero_section_course_notices_reach_tool_result_without_another_request(monkeypatch, live):
    from app import config
    from app.agent import tools as agent_tools

    raw = _swe43_response()
    course = raw["data"]["schools"][0]["departments"][0]["courses"][0]
    course["sections"] = []
    calls = []

    def fetch(*_args, **kwargs):
        calls.append(kwargs.get("params"))
        return deepcopy(raw)

    monkeypatch.setattr(anteater, "_get_json", fetch)
    monkeypatch.setattr(db, "get_term_coverage", lambda _term: {"coverage_status": "unavailable"})
    monkeypatch.setattr(db, "get_catalog", lambda _term: None)
    monkeypatch.setattr(db.websoc_workflow, "fetch_websoc_course_offering", lambda **_kwargs: {
        "ok": False, "offering_status": "unavailable", "error_code": "network_error",
    })
    monkeypatch.setattr(config, "dynamic_prefilter_enabled", lambda: False)
    monkeypatch.setattr("app.recommendations.pairing.contract_enabled", lambda: False)
    tool = agent_tools._tool_get_live_sections if live else agent_tools._tool_get_sections
    result = tool("SWE43", context={"term": "2026 Fall"})
    assert result["found"] is False
    assert result["sections"] == []
    assert db._to_term(result["term"]) == db._to_term("2026 Fall")
    assert len(result["course_notices"]) == 1
    notice = result["course_notices"][0]
    context = result["comment_contexts"][notice["comment_context_id"]]
    assert notice["course_id"] == "SWE43"
    assert notice["source_url"] == anteater.WEBSOC_URL
    assert db._to_term(notice["term"]) == db._to_term("2026 Fall")
    assert notice["retrieved_at"]
    assert "Previously offered as INF 43" in context["course"]["comment"]
    assert context["department"]["course_number_range_comments"]
    if not live:
        assert result["offering_status"] == "unavailable"  # Secondary absence is not official no-offering proof.
    repeated = tool("SWE43", context={"term": "2026 Fall"})
    assert repeated["course_notices"] == result["course_notices"]
    assert len(calls) == 1


def test_ordinary_empty_sections_keep_list_contract_and_metadata(monkeypatch):
    raw = _swe43_response()
    raw["data"]["schools"][0]["departments"][0]["courses"][0]["sections"] = []
    monkeypatch.setattr(anteater, "_get_json", lambda *_args, **_kwargs: raw)
    result = anteater.fetch_sections("SWE", "43", "2026", "Fall")
    assert isinstance(result, list)
    assert result == []
    assert result.course_notices[0]["course_id"] == "SWE43"
    assert result.comment_contexts
    assert deepcopy(result).course_notices == result.course_notices


@pytest.mark.parametrize("scope", ["school", "department"])
def test_parent_notices_survive_empty_tree_branches(scope):
    school = {"schoolName": "ICS", "schoolComment": "School-wide source statement", "departments": []}
    if scope == "department":
        school["departments"] = [{"deptCode": "SWE", "deptName": "Software Engineering",
                                  "deptComment": "Department source statement", "courses": []}]
    metadata = websoc_comment_metadata({"schools": [school]})
    assert len(metadata["course_notices"]) == 1
    notice = metadata["course_notices"][0]
    assert notice["scope"] == scope
    context = metadata["comment_contexts"][notice["comment_context_id"]]
    assert context["school"]["comment"] == "School-wide source statement"
    if scope == "department":
        assert context["department"]["comment"] == "Department source statement"


def test_empty_source_response_does_not_add_empty_notice_envelope(monkeypatch):
    monkeypatch.setattr(anteater, "_get_json", lambda *_args, **_kwargs: {"ok": True, "data": {"schools": []}})
    live = anteater.fetch_live_sections(year="2026", quarter="Fall", department="SWE", course_number="43")
    assert live["sections"] == []
    assert "comment_contexts" not in live and "course_notices" not in live
    ordinary = anteater.fetch_sections("SWE", "43", "2026", "Fall")
    assert ordinary == [] and not ordinary.comment_contexts and not ordinary.course_notices
