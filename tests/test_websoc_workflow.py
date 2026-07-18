from __future__ import annotations

from pathlib import Path

import pytest
import requests

from app.data import websoc_workflow


FIXTURES = Path(__file__).parent / "fixtures" / "websoc"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_build_websoc_department_url_uses_fixed_registrar_endpoint() -> None:
    url = websoc_workflow.build_websoc_department_url("Fall 2026", "ART")

    assert url.startswith("https://www.reg.uci.edu/perl/WebSoc?")
    assert "YearTerm=2026-92" in url
    assert "Dept=ART" in url
    assert "CancelledCourses=Exclude" in url


def test_parse_art_websoc_comments_extracts_restriction_dates_and_links() -> None:
    result = websoc_workflow.parse_websoc_department_html(
        _fixture("art_department.html"),
        term="Fall 2026",
        department="ART",
        source_url=websoc_workflow.build_websoc_department_url("Fall 2026", "ART"),
        retrieved_at="2026-07-18T11:00:00Z",
    )

    assert result["ok"] is True
    assert result["workflow_id"] == "websoc_department_restrictions"
    assert result["search_criteria"] == {
        "department": "ART",
        "exclude_cancelled_courses": True,
    }
    assert result["registration_ends"] == "Tuesday, September 15, 2026"
    assert "Claire Trevor School of the Arts comments" in result["school_comments"]
    assert "Art department comments" in result["department_comments"]
    assert result["fields"]["drop_deadline"] == "the end of Week 2 by 5:00PM"
    assert result["fields"]["change_deadline"] == "at the end of Week 2 by 5:00PM"
    assert result["fields"]["add_deadline"] == "the end of Week 2 by 5:00PM"
    assert (
        result["fields"]["major_restriction_removed_at"]
        == "Monday, August 24th, 2026 at noon"
    )
    assert result["fields"]["nors_removed_at"] == "Friday, August 21st at noon"
    assert result["fields"]["contact_emails"] == ["artscounselor@uci.edu"]
    assert result["links"] == [
        {
            "text": "http://www.arts.uci.edu/student-affairs-next-quarter-enroll",
            "url": "http://www.arts.uci.edu/student-affairs-next-quarter-enroll",
            "domain": "www.arts.uci.edu",
            "link_role": "enrollment_instructions",
            "source_block": "Claire Trevor School of the Arts comments:",
            "source_block_type": "school",
            "source_url": result["source_url"],
            "is_uci_official": True,
            "allowed_for_deep_read": True,
        }
    ]


def test_parse_ics_websoc_comments_collects_official_deep_read_links() -> None:
    result = websoc_workflow.parse_websoc_department_html(
        _fixture("ics_department.html"),
        term="Fall 2026",
        department="I&C SCI",
        source_url=websoc_workflow.build_websoc_department_url("Fall 2026", "I&C SCI"),
        retrieved_at="2026-07-18T11:00:00Z",
    )

    urls = [link["url"] for link in result["links"]]

    assert result["ok"] is True
    assert result["search_criteria"]["department"] == "I&C SCI"
    assert result["fields"]["major_restriction_removed_at"] is None
    assert result["extraction_status"] == "partial"
    assert "http://ics.uci.edu/course-enrollment-restrictions/" in urls
    assert (
        "http://ics.uci.edu/academics/undergraduate-programs/majors-minors/"
        "undergraduate-student-policies/"
    ) in urls
    assert all(link["is_uci_official"] for link in result["links"])
    assert all(link["allowed_for_deep_read"] for link in result["links"])
    assert any(
        link["source_block_type"] == "department"
        and link["url"] == "http://ics.uci.edu/course-enrollment-restrictions/"
        and link["link_role"] == "ics_undergraduate_restrictions"
        for link in result["links"]
    )


def test_deep_read_fetches_only_selected_official_websoc_links() -> None:
    workflow_result = websoc_workflow.parse_websoc_department_html(
        _fixture("ics_department.html"),
        term="Fall 2026",
        department="I&C SCI",
        source_url=websoc_workflow.build_websoc_department_url("Fall 2026", "I&C SCI"),
        retrieved_at="2026-07-18T11:00:00Z",
    )
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, url: str):
            self.url = url
            self.text = """
            <html><body>
              <h1>ICS Course Enrollment Restrictions</h1>
              <p>Fall 2026 restriction details are posted by course.</p>
            </body></html>
            """

        def raise_for_status(self):
            return None

    class FakeSession:
        def get(self, url, **_kwargs):
            calls.append(url)
            return FakeResponse(url)

    result = websoc_workflow.fetch_linked_official_pages(
        workflow_result,
        session=FakeSession(),
        max_pages=2,
    )

    assert result["ok"] is True
    assert result["selected_count"] == 2
    assert calls == [
        "http://ics.uci.edu/course-enrollment-restrictions/",
        "http://ics.uci.edu/academics/graduate-academic-advising/course-updates/",
    ]
    assert result["pages"][0]["domain"] == "ics.uci.edu"
    assert result["pages"][0]["link_role"] == "ics_undergraduate_restrictions"
    assert "Fall 2026 restriction details" in result["pages"][0]["text_excerpt"]


def test_deep_read_skips_links_when_websoc_comments_already_have_date() -> None:
    workflow_result = websoc_workflow.parse_websoc_department_html(
        _fixture("art_department.html"),
        term="Fall 2026",
        department="ART",
        source_url=websoc_workflow.build_websoc_department_url("Fall 2026", "ART"),
        retrieved_at="2026-07-18T11:00:00Z",
    )

    class FakeSession:
        def get(self, *_args, **_kwargs):
            raise AssertionError("direct WebSoc date should avoid linked-page fetch")

    result = websoc_workflow.fetch_linked_official_pages(
        workflow_result,
        session=FakeSession(),
    )

    assert result["ok"] is True
    assert result["selected_count"] == 0
    assert result["pages"] == []
    assert result["errors"] == []


def test_deep_read_rejects_non_uci_redirect() -> None:
    workflow_result = {
        "ok": True,
        "workflow_id": "websoc_department_restrictions",
        "source_url": "https://www.reg.uci.edu/perl/WebSoc?Dept=ART",
        "fields": {"major_restriction_removed_at": None},
        "links": [
            {
                "text": "Restriction details",
                "url": "https://arts.uci.edu/restrictions",
                "allowed_for_deep_read": True,
                "link_role": "restriction_details",
                "source_block": "Art department comments:",
            }
        ],
    }

    class FakeResponse:
        text = "<html><body>external</body></html>"
        url = "https://example.com/restrictions"

        def raise_for_status(self):
            return None

    class FakeSession:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    result = websoc_workflow.fetch_linked_official_pages(
        workflow_result,
        session=FakeSession(),
    )

    assert result["pages"] == []
    assert result["errors"][0]["error_code"] == "linked_page_left_allowed_domain"


def test_deep_read_rejects_non_textual_linked_page() -> None:
    workflow_result = {
        "ok": True,
        "workflow_id": "websoc_department_restrictions",
        "source_url": "https://www.reg.uci.edu/perl/WebSoc?Dept=ART",
        "fields": {"major_restriction_removed_at": None},
        "links": [
            {
                "text": "Restriction details",
                "url": "https://arts.uci.edu/restrictions.pdf",
                "allowed_for_deep_read": True,
                "link_role": "restriction_details",
                "source_block": "Art department comments:",
            }
        ],
    }

    class FakeResponse:
        text = "%PDF"
        url = "https://arts.uci.edu/restrictions.pdf"
        headers = {"content-type": "application/pdf"}

        def raise_for_status(self):
            return None

    class FakeSession:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    result = websoc_workflow.fetch_linked_official_pages(
        workflow_result,
        session=FakeSession(),
    )

    assert result["pages"] == []
    assert result["errors"][0]["error_code"] == "linked_page_unsupported_content_type"


def test_deep_read_rejects_oversized_linked_page() -> None:
    workflow_result = {
        "ok": True,
        "workflow_id": "websoc_department_restrictions",
        "source_url": "https://www.reg.uci.edu/perl/WebSoc?Dept=ART",
        "fields": {"major_restriction_removed_at": None},
        "links": [
            {
                "text": "Restriction details",
                "url": "https://arts.uci.edu/restrictions",
                "allowed_for_deep_read": True,
                "link_role": "restriction_details",
                "source_block": "Art department comments:",
            }
        ],
    }

    class FakeResponse:
        text = "<html></html>"
        url = "https://arts.uci.edu/restrictions"
        headers = {
            "content-type": "text/html",
            "content-length": str(websoc_workflow.LINK_MAX_BYTES + 1),
        }

        def raise_for_status(self):
            return None

    class FakeSession:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    result = websoc_workflow.fetch_linked_official_pages(
        workflow_result,
        session=FakeSession(),
    )

    assert result["pages"] == []
    assert result["errors"][0]["error_code"] == "linked_page_too_large"


def test_fetch_websoc_department_restrictions_returns_structured_error() -> None:
    class FailingSession:
        def get(self, *_args, **_kwargs):
            raise requests.Timeout("slow")

    result = websoc_workflow.fetch_websoc_department_restrictions(
        term="Fall 2026",
        department="ART",
        session=FailingSession(),
    )

    assert result["ok"] is False
    assert result["workflow_id"] == "websoc_department_restrictions"
    assert result["error_code"] == "websoc_request_failed"
    assert result["source_url"].startswith("https://www.reg.uci.edu/perl/WebSoc?")
    assert "Timeout" in result["message"]


def test_build_websoc_department_params_rejects_missing_department() -> None:
    with pytest.raises(ValueError, match="department is required"):
        websoc_workflow.build_websoc_department_params("Fall 2026", "")
