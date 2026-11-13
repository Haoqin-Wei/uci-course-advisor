"""Drive the restriction tool against a stored term and a canned official page."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.agent import tools as agent_tools
from app.catalog.sync import sync_term
from app.data import websoc_workflow

ICS_LINK = "http://ics.uci.edu/course-enrollment-restrictions/"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "websoc"


def fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text()


def _department(code: str, index: int) -> dict:
    section = {"sectionCode": str(34000 + index), "sectionType": "Lec", "sectionNum": "A", "units": "4",
               "status": "OPEN", "instructors": ["DILLENCOURT, M."], "maxCapacity": "165",
               "numCurrentlyEnrolled": {"totalEnrolled": "0", "sectionEnrolled": ""}, "isCancelled": False,
               "meetings": [{"timeIsTBA": True}], "finalExam": {}}
    return {
        "deptCode": code, "deptName": f"{code} department",
        "deptComment": (f"<p><b>RESTRICTIONS:</b> {code} courses will initially be restricted to majors in the "
                        "Donald Bren School of Information and Computer Sciences ONLY. For restriction details "
                        f'and timeline: <a href="{ICS_LINK}">{ICS_LINK}</a></p>'),
        "courseNumberRangeComments": [], "sectionCodeRangeComments": [],
        "courses": [{"deptCode": code, "courseNumber": "161", "courseTitle": "ALGORITHMS",
                     "courseComment": "", "sections": [section]}],
    }


def sync_departments(term_id: str, *codes: str) -> None:
    """Store a term whose departments all point to the ICS restrictions page."""
    data = {"schools": [{
        "schoolName": "Donald Bren School of Information and Computer Sciences", "schoolComment": "",
        "departments": [_department(code, index) for index, code in enumerate(codes)],
    }]}
    assert sync_term(term_id, fetcher=lambda term: SimpleNamespace(
        ok=True, status="ok", error=None, data=data)).ok


class PageSession:
    """Answers every linked-page request with the same HTML and counts them."""

    def __init__(self, html: str):
        self.html = html
        self.calls: list[str] = []

    def get(self, url, **_kwargs):
        self.calls.append(url)
        return SimpleNamespace(status_code=200, url=ICS_LINK, headers={"content-type": "text/html"},
                               text=self.html, raise_for_status=lambda: None)


def serve_page(monkeypatch, html: str) -> PageSession:
    session = PageSession(html)
    real = websoc_workflow.fetch_linked_official_pages
    monkeypatch.setattr(agent_tools.websoc_workflow, "fetch_linked_official_pages",
                        lambda result: real(result, session=session))
    return session


def lookup(term: str, department: str, **extra) -> dict:
    return agent_tools.dispatch(
        "get_department_restrictions",
        {"term": term, "department": department, "restriction_type": "major_restriction", **extra},
        context={"allowed_query_terms": [term]},
    )
