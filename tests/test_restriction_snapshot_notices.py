from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.agent import tools as agent_tools
from app.catalog import store
from app.catalog.sync import sections_from_websoc, sync_term
from app.data import websoc_workflow

ICS_LINK = "http://ics.uci.edu/course-enrollment-restrictions/"
SCHOOL_NOTE = "<p><b>IMPORTANT NOTE:</b> Several In4matx courses have been renamed.</p>"
DEPT_NOTE = ('<p><b>RESTRICTIONS:</b> COMPSCI courses will initially be restricted to majors in the Donald Bren '
             'School of Information and Computer Sciences ONLY. For restriction details and timeline: '
             f'<a href="{ICS_LINK}">{ICS_LINK}</a></p>')
RANGE_NOTE = ('<p class="comment-heading">CompSci course numbers 200 - 299:</p>'
              '<p>Graduate courses stay restricted to majors until further notice.</p>')


def listing(dept_comment=DEPT_NOTE, school_comment=SCHOOL_NOTE, ranges=(RANGE_NOTE,)):
    section = {"sectionCode": "34220", "sectionType": "Lec", "sectionNum": "A", "units": "4", "status": "OPEN",
               "instructors": ["DILLENCOURT, M."], "maxCapacity": "165", "numNewOnlyReserved": "20",
               "numCurrentlyEnrolled": {"totalEnrolled": "0", "sectionEnrolled": ""}, "isCancelled": False,
               "meetings": [{"timeIsTBA": True}], "finalExam": {}}
    return {"schools": [{
        "schoolName": "Donald Bren School of Information and Computer Sciences", "schoolComment": school_comment,
        "departments": [{
            "deptCode": "COMPSCI", "deptName": "Computer Science", "deptComment": dept_comment,
            "courseNumberRangeComments": list(ranges), "sectionCodeRangeComments": [],
            "courses": [{"deptCode": "COMPSCI", "courseNumber": "161", "courseTitle": "ALGORITHMS",
                         "courseComment": "", "sections": [section]}]}]}]}


def sync(data=None):
    result = sync_term("2027_Winter", fetcher=lambda term: SimpleNamespace(
        ok=True, status="ok", error=None, data=data or listing()))
    assert result.ok
    return result


def test_store_returns_a_departments_notices_and_new_only_seats():
    assert store.get_department_scope("2027_Winter", "COMPSCI") is None      # nothing stored, no file created
    sync()
    scope = store.get_department_scope("2027 Winter", " compsci ")
    assert scope["school"]["comment"] == SCHOOL_NOTE and scope["department"]["comment"] == DEPT_NOTE
    assert scope["department"]["course_number_range_comments"] == [RANGE_NOTE]
    assert "course" not in scope
    assert store.get_department_scope("2027_Winter", "MATH") is None
    assert store.get_sections("2027_Winter")[0]["new_only_reserved"] == 20


def test_notices_come_from_the_snapshot_without_any_request():
    assert websoc_workflow.department_restrictions_from_snapshot(term="Winter 2027", department="COMPSCI") is None
    sync()

    result = websoc_workflow.department_restrictions_from_snapshot(term="Winter 2027", department="compsci")

    assert result["ok"] is True and result["notice_source"] == "catalog_store_snapshot"
    assert (result["department"], result["response_term"]) == ("COMPSCI", "Winter 2027")
    assert [(b["block_type"], b["title"]) for b in result["comment_blocks"]] == [
        ("school", "Donald Bren School of Information and Computer Sciences comments:"),
        ("department", "Computer Science department comments:")]
    assert "restricted to majors" in result["department_comments"]
    assert "have been renamed" in result["school_comments"]
    # A course-range notice is kept, but not mixed into the department-wide text.
    assert "Graduate courses" not in result["department_comments"]
    assert any("200 - 299" in note and "Graduate courses" in note for note in result["course_range_notices"])
    links = {link["url"]: link for link in result["links"]}
    assert links[ICS_LINK]["source_block_type"] == "department" and links[ICS_LINK]["allowed_for_deep_read"] is True
    assert list(links) == [ICS_LINK]
    snapshot = store.get_term_snapshot("2027_Winter")
    assert result["retrieved_at"] == snapshot["last_synced_at"]             # when it was read, not "now"
    (read,) = result["fetches"]
    assert (read["method"], read["source_role"], read["local"], read["ok"]) == (
        "LOCAL", "catalog_store_snapshot", True, True)
    assert read["status_code"] is None and read["snapshot_synced_at"] == snapshot["last_synced_at"]
    assert result["validation"] == {"ok": True, "errors": []}


@pytest.mark.parametrize("term, department", [("Winter 2027", "MATH"), ("Spring 2027", "COMPSCI"),
                                              ("someday", "COMPSCI"), ("Winter 2027", "")])
def test_unknown_department_or_term_defers_to_the_live_page(term, department):
    sync()
    assert websoc_workflow.department_restrictions_from_snapshot(term=term, department=department) is None


def test_stale_or_empty_notices_defer_to_the_live_page():
    old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat(timespec="seconds")
    store.replace_term_sections("2027_Winter", sections_from_websoc(listing()), source="t", synced_at=old)
    assert websoc_workflow.department_restrictions_from_snapshot(term="Winter 2027", department="COMPSCI") is None
    assert websoc_workflow.department_restrictions_from_snapshot(
        term="Winter 2027", department="COMPSCI", max_age_s=72 * 3600)["ok"] is True

    sync(listing(dept_comment="", school_comment="  ", ranges=()))
    assert websoc_workflow.department_restrictions_from_snapshot(term="Winter 2027", department="COMPSCI") is None


class Page:
    status_code = 200
    url = ICS_LINK
    headers = {"content-type": "text/html"}
    text = "<main><p>12/1/2026</p><p>12:00pm</p><p>School/Major restrictions are removed.</p></main>"

    def raise_for_status(self):
        return None


class Session:
    def __init__(self):
        self.calls = []

    def get(self, url, **_kwargs):
        self.calls.append(url)
        return Page()


def test_linked_pages_follow_from_snapshot_notices_and_are_cached_for_a_day(monkeypatch):
    sync()
    notices = websoc_workflow.department_restrictions_from_snapshot(term="Winter 2027", department="COMPSCI")
    notices["restriction_type"] = "school_major"
    session = Session()

    first = websoc_workflow.fetch_linked_official_pages(notices, session=session)
    assert session.calls == [ICS_LINK]
    assert first["pages"][0]["url"] == ICS_LINK and first["pages"][0]["timeline_events"]
    assert "cached" not in first["fetches"][0]

    second = websoc_workflow.fetch_linked_official_pages(notices, session=session)
    assert session.calls == [ICS_LINK]                                       # no second request
    record = second["fetches"][0]
    assert record["cached"] is True and record["cached_at"] == first["pages"][0]["retrieved_at"]
    assert (record["status_code"], record["duration_ms"], record["url"]) == (200, 0.0, ICS_LINK)
    assert second["pages"][0]["retrieved_at"] == first["pages"][0]["retrieved_at"]   # original read time
    assert second["pages"][0]["timeline_events"] == first["pages"][0]["timeline_events"]

    monkeypatch.setattr(websoc_workflow, "LINKED_PAGE_CACHE_TTL_S", -1)      # a day has passed
    websoc_workflow.fetch_linked_official_pages(notices, session=session)
    assert session.calls == [ICS_LINK, ICS_LINK]


def test_failed_or_rejected_pages_are_never_cached():
    sync()
    notices = websoc_workflow.department_restrictions_from_snapshot(term="Winter 2027", department="COMPSCI")
    notices["restriction_type"] = "school_major"

    class PdfPage(Page):
        headers = {"content-type": "application/pdf"}

    class PdfSession(Session):
        def get(self, url, **_kwargs):
            self.calls.append(url)
            return PdfPage()

    rejected = PdfSession()
    websoc_workflow.fetch_linked_official_pages(notices, session=rejected)
    websoc_workflow.fetch_linked_official_pages(notices, session=rejected)
    assert rejected.calls == [ICS_LINK, ICS_LINK]
    assert websoc_workflow._linked_page_cache == {}


def test_restriction_tool_reads_notices_locally_and_reports_how_each_source_was_read(monkeypatch):
    sync()

    def no_live_page(**_kwargs):
        raise AssertionError("the Registrar page must not be requested while the snapshot is fresh")

    monkeypatch.setattr(agent_tools.websoc_workflow, "fetch_websoc_department_restrictions", no_live_page)
    session = Session()
    real = websoc_workflow.fetch_linked_official_pages
    monkeypatch.setattr(agent_tools.websoc_workflow, "fetch_linked_official_pages",
                        lambda result: real(result, session=session))
    args = {"term": "Winter 2027", "department": "COMPSCI", "restriction_type": "major_restriction"}
    context = {"allowed_query_terms": ["Winter 2027"]}

    result = agent_tools.dispatch("get_department_restrictions", dict(args), context=context)

    assert result["ok"] is True and result["notice_source"] == "catalog_store_snapshot"
    assert session.calls == [ICS_LINK]
    local, page = result["fetch_summary"]
    assert (local["method"], local["local"], local["source_role"]) == ("LOCAL", True, "catalog_store_snapshot")
    assert local["snapshot_synced_at"] and "cached" not in local
    assert page["method"] == "GET" and "cached" not in page and "local" not in page
    assert "comment_blocks" not in result and "fetches" not in result
    assert any("200 - 299" in note for note in result["course_range_notices"])

    again = agent_tools.dispatch("get_department_restrictions", dict(args), context=context)
    assert session.calls == [ICS_LINK]                                       # linked page reused
    assert again["fetch_summary"][1]["cached"] is True and again["fetch_summary"][1]["cached_at"]
