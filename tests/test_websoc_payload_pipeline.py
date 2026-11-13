"""Source payloads survive tool JSON, offline storage and schedule restore.

The compact SWE 43 capture is shared with the source-adapter regressions.
Additional meetings and malformed clocks below are explicit synthetic cases.
No test reads local capture artifacts or performs an external request.
"""
from copy import deepcopy
import csv
import json

from app.agent import tools
from app.catalog.loaders.uci_relational import UCIRelationalLoader
from app.catalog.term import Term
from app.data import anteater, db, sessions
from app.scheduling.service import section_meetings, section_time_status
from scripts import fetch_uci_data
from tests.test_anteater_payload_preservation import _swe43_response


TERM = "2026 Fall"
COURSE = "SWE 43"


def _source_parts(raw):
    school = raw["data"]["schools"][0]
    department = school["departments"][0]
    return school, department, department["courses"][0]


def _replay_api(monkeypatch, raw):
    calls = []
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "false")
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "false")
    monkeypatch.setattr(db, "get_term_coverage", lambda *_: {"coverage_status": "unavailable"})

    def replay(url, **kwargs):
        calls.append((url, deepcopy(kwargs.get("params"))))
        return deepcopy(raw)

    monkeypatch.setattr(anteater, "_get_json", replay)
    return calls


def _write_sections(tmp_path, rows):
    with (tmp_path / "sections.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fetch_uci_data.SECTIONS_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    _, sections = UCIRelationalLoader(tmp_path).load(Term(2026, "Fall"))
    return [db._section_record_to_dict(item) for item in sections]


def _keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)


def test_captured_source_reaches_live_tool_json_without_losing_any_comment_scope(monkeypatch):
    raw = _swe43_response()
    school, department, course = _source_parts(raw)
    # Even if a source supplies this obsolete field, the tool must omit it.
    course["sections"][0]["webURL"] = "obsolete section link"
    before = deepcopy(raw)
    calls = _replay_api(monkeypatch, raw)

    result = tools._tool_get_live_sections(COURSE, context={"term": TERM}, force_refresh=True)
    wire = json.loads(json.dumps(result, ensure_ascii=False))

    assert len(calls) == 1
    assert calls[0][1] == {"year": "2026", "quarter": "Fall", "department": "SWE", "courseNumber": "43"}
    assert wire["found"] is True and wire["is_live"] is True
    assert db._to_term(wire["term"]) == db._to_term(TERM)
    assert len(wire["comment_contexts"]) == 1
    for source, item in zip(course["sections"], wire["sections"]):
        comments = wire["comment_contexts"][item["comment_context_id"]]
        assert comments["school"]["comment"] == school["schoolComment"]
        assert comments["department"]["comment"] == department["deptComment"]
        assert comments["department"]["course_number_range_comments"] == department["courseNumberRangeComments"]
        assert comments["department"]["section_code_range_comments"] == department["sectionCodeRangeComments"]
        assert comments["course"]["comment"] == course["courseComment"]
        assert "https://ics.uci.edu/" in comments["school"]["comment"]
        assert item["section_comment"] == source["sectionComment"]
        assert item["meetings"] == source["meetings"]
        assert item["is_cancelled"] is True and item["cancellation_status"] == "cancelled"
        assert item["status"] == "FULL"  # FULL does not erase cancellation.
    assert not any(key.lower().replace("_", "") == "weburl" for key in _keys(wire))
    assert raw == before


def test_malformed_first_clock_does_not_drop_other_meetings_at_the_live_tool_boundary(monkeypatch):
    raw = _swe43_response()
    _, _, course = _source_parts(raw)
    source = course["sections"][0]
    source.pop("isCancelled")
    source["status"] = "OPEN"
    source["meetings"] = [
        {"days": "M", "startTime": {"hour": 8}, "endTime": {"hour": 9, "minute": 0}},
        {"days": "F", "startTime": {"hour": 13, "minute": 0},
         "endTime": {"hour": 13, "minute": 50}, "bldg": ["SSL 100"], "timeIsTBA": False},
        {"days": "TBA", "timeIsTBA": True},
    ]
    _replay_api(monkeypatch, raw)

    result = tools._tool_get_live_sections(COURSE, context={"term": TERM}, force_refresh=True)
    item = json.loads(json.dumps(result))["sections"][0]

    assert item["meetings"] == source["meetings"]
    assert item["is_cancelled"] is None and item["cancellation_status"] == "unknown"
    normalized = section_meetings(item)
    assert normalized[0]["time_is_tba"] is True and normalized[0]["start_time"] is None
    assert normalized[1]["start_time"] == "13:00" and normalized[1]["location"] == "SSL 100"
    existing = {"days": "F", "start_time": "13:20", "end_time": "14:00"}
    assert section_time_status(item, existing) == "conflict"


def test_offline_import_loader_and_db_json_preserve_the_same_source_evidence(tmp_path):
    raw = _swe43_response()
    school, department, course = _source_parts(raw)
    source = course["sections"][0]
    source["meetings"].append({"days": "TBA", "timeIsTBA": True, "bldg": ["ONLINE"]})
    row, _ = fetch_uci_data._websoc_section_to_rows(source, course, department, 2026, "Fall", school=school)

    restored = json.loads(json.dumps(_write_sections(tmp_path, [row])))[0]

    assert restored["section_code"] == source["sectionCode"]
    assert restored["meetings"] == source["meetings"]
    assert restored["is_cancelled"] is True and restored["status"] == "FULL"
    assert restored["section_comment"] == source["sectionComment"]
    assert restored["comments"]["school"]["comment"] == school["schoolComment"]
    assert restored["comments"]["department"]["course_number_range_comments"] == department["courseNumberRangeComments"]
    assert restored["comments"]["department"]["section_code_range_comments"] == []
    assert restored["comments"]["course"]["comment"] == course["courseComment"]
    assert section_meetings(restored)[1]["time_is_tba"] is True


def test_versioned_csv_distinguishes_explicit_empty_meetings_from_null_legacy_shape(tmp_path):
    school, department, course = _source_parts(_swe43_response())
    rows = []
    for code, meetings in (("37501", []), ("37502", None)):
        source = {"sectionCode": code, "sectionNum": "A", "sectionType": "Lec", "meetings": meetings}
        row, _ = fetch_uci_data._websoc_section_to_rows(source, course, department, 2026, "Fall", school=school)
        # Stale flat fields cannot override an explicit empty source payload.
        # A None payload retains the supported legacy representation instead.
        row.update(days="M", start_time="11:00", end_time="12:00")
        rows.append(row)
    restored = {row["section_code"]: row for row in _write_sections(tmp_path, rows)}

    assert restored["37501"]["meetings"] == []
    assert section_meetings(restored["37501"])[0]["time_is_tba"] is True
    assert restored["37502"]["meetings"] is None
    assert section_meetings(restored["37502"])[0]["start_time"] == "11:00"
    assert all(row["is_cancelled"] is None and row["cancellation_status"] == "unknown"
               for row in restored.values())


def test_schedule_refresh_and_restore_keep_all_known_blocks_plus_the_unknown_arrangement(app_client, monkeypatch):
    raw = _swe43_response()
    _, _, course = _source_parts(raw)
    source = course["sections"][0]
    source.update(sectionNum="A", sectionType="Lec", status="OPEN", isCancelled=False,
                  sectionComment="<p>Two scheduled meetings and one arrangement to be announced.</p>")
    source["meetings"] = [
        {"days": "MW", "startTime": {"hour": 11, "minute": 0},
         "endTime": {"hour": 12, "minute": 20}, "bldg": ["DBH 1100"]},
        {"days": "F", "startTime": {"hour": 13, "minute": 0},
         "endTime": {"hour": 13, "minute": 50}, "bldg": ["SSL 100"]},
        {"days": "TBA", "timeIsTBA": True, "bldg": ["ONLINE"]},
    ]
    calls = _replay_api(monkeypatch, raw)
    monkeypatch.setattr(db, "get_course_info", lambda *_: {"found": True, "course": {"title": "Software Engineering"}})
    monkeypatch.setattr(db, "get_sections", lambda *_: {"found": False, "sections": []})
    sid = sessions.create_session("demo_001", title="Complete meeting snapshot")
    sessions.update_session_state("demo_001", sid, {"pending_schedule": [
        {"course_id": COURSE, "term": TERM, "section": "A", "status": "pending"}]})

    response = app_client.post("/api/schedule/refresh", json={"session_id": sid})
    assert response.status_code == 200
    payload = response.json()
    restored = app_client.get("/api/schedule", params={"session_id": sid})
    assert restored.status_code == 200
    assert len(calls) == 1  # Restoring the saved snapshot does not re-fetch API data.
    assert restored.json()["events"] == payload["events"]
    assert [(event["day"], event["start"], event["location"], event["meeting_index"])
            for event in payload["events"]] == [
                ("Mon", "11:00", "DBH 1100", 0), ("Wed", "11:00", "DBH 1100", 0),
                ("Fri", "13:00", "SSL 100", 1)]
    for result in (payload, restored.json()):
        entry = result["pending_schedule"][0]
        assert entry["materialization_status"] == "tba"
        snapshot = entry["materialized_section"]
        assert snapshot["meetings"] == source["meetings"]
        assert snapshot["section_comment"] == source["sectionComment"]
        assert snapshot["comments"]["course"]["comment"] == course["courseComment"]
        assert any(item["type"] == "time_unknown" for item in result["schedule_validation"]["unknowns"])


def test_refresh_keeps_cancelled_full_section_evidence_but_creates_no_calendar_block(app_client, monkeypatch):
    raw = _swe43_response()
    _replay_api(monkeypatch, raw)
    monkeypatch.setattr(db, "get_sections", lambda *_: {"found": False, "sections": []})
    monkeypatch.setattr(db, "get_course_info", lambda *_: {"found": False})
    sid = sessions.create_session("demo_001", title="Cancelled source evidence")
    sessions.update_session_state("demo_001", sid, {"pending_schedule": [
        {"course_id": COURSE, "term": TERM, "section": "B3", "status": "pending"}]})

    response = app_client.post("/api/schedule/refresh", json={"session_id": sid})
    assert response.status_code == 200
    payload = response.json()

    assert payload["events"] == []
    assert len(payload["pending_schedule"]) == 1
    entry = payload["pending_schedule"][0]
    assert entry["materialized_section"]["is_cancelled"] is True
    assert entry["materialized_section"]["status"] == "FULL"
    assert "CANCELLED" in entry["materialized_section"]["section_comment"]
    assert any("marked cancelled" in notice for notice in entry["notices"])
    assert any(item["type"] == "section_unavailable" for item in payload["schedule_validation"]["conflicts"])
