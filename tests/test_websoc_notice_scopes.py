"""Scope and evidence survive official HTML parsing and candidate exclusion."""
from copy import deepcopy

from app.agent import prefilter
from app.data.websoc_payload import compact_comment_contexts
from app.data.websoc_workflow import parse_websoc_course_html
from app.recommendations.filter_plan import empty_filter_plan
from app.scheduling.service import section_time_status


def test_official_comment_ranges_stay_separate_from_course_and_all_meetings_survive():
    html = """<html><h1>Schedule of Classes search results</h1>
    <p>Department: SWE</p><p>Course Number Range: 43</p><h3>Fall Quarter, 2026</h3><table>
    <tr class="college-title"><td>School of ICS</td></tr>
    <tr class="college-comment"><td>School notice</td></tr>
    <tr class="dept-title"><td>Software Engineering</td></tr>
    <tr class="dept-comment"><td>Department notice</td></tr>
    <tr class="num-range-comment"><td>SWE 200-299: Graduate students only.</td></tr>
    <tr class="ccode-range-comment"><td>Codes 37900-37999: Separate authorization.</td></tr>
    <tr><td class="CourseTitle">SWE 43 INTRO SOFTWARE ENGR</td></tr>
    <tr class="course-comment"><td>Previously offered as INF 43.
    <a href="https://example.edu/notice">Details</a></td></tr>
    <tr><td>37500</td><td>Lec</td><td>A</td><td>4</td><td>TESTER, A.</td>
    <td>In-Person</td><td>TuTh 6:30-7:50p<br>F 8:00-8:50a</td>
    <td>ALP 1300<br>DBH 1100</td><td>249</td><td>216</td><td>232</td><td>A</td>
    <td></td><td></td><td>OPEN</td></tr>
    <tr class="section-comment"><td>Section A notice</td></tr>
    </table></html>"""
    result = parse_websoc_course_html(html, term="2026 Fall", department="SWE", course_number="43",
                                      source_url="https://www.reg.uci.edu/perl/WebSoc")
    assert result["found"] is True
    section = result["sections"][0]
    assert len(section["meetings"]) == 2
    assert section["meetings"][1]["location"] == "DBH 1100"
    assert section_time_status(section, {"days": "F", "start_time": "08:30", "end_time": "09:00"}) == "conflict"
    comments = section["comments"]
    assert comments["school"]["comment"] == "School notice"
    assert comments["department"]["comment"] == "Department notice"
    assert "Graduate" in comments["department"]["course_number_range_comments"][0]
    assert "37900" in comments["department"]["section_code_range_comments"][0]
    assert "Graduate" not in comments["course"]["comment"]
    assert "Previously offered" in comments["course"]["comment"]
    assert section["section_comment"] == "Section A notice"
    assert any('href="https://example.edu/notice"' in block["raw_html"] for block in comments["source_blocks"])
    assert "webURL" not in section and "web_url" not in section


def test_prefilter_exclusion_keeps_non_selectable_notice_and_meeting_evidence():
    context = {"_prefilter_state": {"plan": empty_filter_plan(), "profile": {}, "ge_scope_requested": False}}
    section = {"section_code": "37513", "section_num": "B3", "section_type": "Dis",
               "is_cancelled": True, "cancellation_status": "cancelled", "status": "FULL",
               "meetings": [{"timeIsTBA": True}], "section_comment": "*** CANCELLED ***",
               "comments": {"school": {"comment": "School notice"},
                            "department": {"course_number_range_comments": ["200-299 only"]},
                            "course": {"comment": "Previously offered as INF 43"}}}
    before = deepcopy(section)
    candidate = {"course_id": "SWE 43", "term": "2026 Fall", "sections": [section]}
    result = prefilter._apply([candidate], context)
    assert not result["kept"]
    wire = compact_comment_contexts({"sections": [], "excluded": result["excluded"]})
    evidence = wire["excluded"][0]["excluded_section_evidence"][0]
    assert evidence["selectable"] is False and evidence["is_cancelled"] is True
    assert evidence["section_comment"] == "*** CANCELLED ***"
    assert evidence["meetings"] == section["meetings"]
    assert wire["comment_contexts"][evidence["comment_context_id"]]["course"]["comment"] == "Previously offered as INF 43"
    assert section == before


def test_csv_import_keeps_source_record_with_malformed_first_clock():
    import json
    from scripts.fetch_uci_data import _websoc_section_to_rows
    meetings = [{"days": "M", "startTime": "invalid object", "endTime": {"hour": 10, "minute": 0}},
                {"days": "F", "startTime": {"hour": 14, "minute": 0}, "endTime": {"hour": 14, "minute": 50}}]
    section = {"sectionCode": "37500", "meetings": meetings, "isCancelled": False,
               "sectionComment": "Keep this notice"}
    row, _ = _websoc_section_to_rows(section, {"courseNumber": "43", "courseComment": "Former name"},
                                    {"deptCode": "SWE"}, 2026, "Fall")
    assert json.loads(row["meetings_json"]) == meetings
    assert row["start_time"] == "" and row["end_time"] == "" and row["time_is_tba"] == "true"
    assert row["section_comment"] == "Keep this notice"


def test_empty_offering_notices_survive_prefilter_candidate_cache():
    from app.data.websoc_payload import websoc_comment_metadata
    metadata = websoc_comment_metadata({"schools": [{"schoolComment": "School notice", "departments": [{
        "deptCode": "SWE", "courses": [{"courseId": "SWE43", "courseComment": "Formerly INF 43", "sections": []}]
    }]}]})
    state = {"plan": empty_filter_plan(), "profile": {}, "ge_scope_requested": False,
             "candidates": {}, "excluded": {}}
    context = {"_prefilter_state": state}
    candidate = prefilter._metadata_candidate({"course_id": "SWE 43"}, [], context, "2026 Fall", offering={
        "found": False, "term": "2026 Fall", "offering_status": "unavailable", **metadata,
    })
    result = prefilter._apply([candidate], context)
    prefilter._store(result, context, "2026 Fall")
    cached = context["_recommendation_sections"][("SWE 43", "2026 Fall")]
    assert cached["found"] is False and cached["sections"] == []
    assert cached["course_notices"] == metadata["course_notices"]
    assert cached["comment_contexts"] == metadata["comment_contexts"]
