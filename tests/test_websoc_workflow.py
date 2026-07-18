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
        for link in result["links"]
    )


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
