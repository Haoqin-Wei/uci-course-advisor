from __future__ import annotations

import logging
from pathlib import Path

import pytest
import requests

from app.data import websoc_workflow


FIXTURES = Path(__file__).parent / "fixtures" / "websoc"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_build_websoc_department_url_uses_fixed_registrar_endpoint() -> None:
    url = websoc_workflow.build_websoc_department_url("Fall 2026", "ART")
    params = websoc_workflow.build_websoc_department_params("Fall 2026", "ART")

    assert url == "https://www.reg.uci.edu/perl/WebSoc"
    assert params["Submit"] == "Display Web Results"
    assert params["YearTerm"] == "2026-92"
    assert params["ShowComments"] == "on"
    assert params["Breadth"] == "ANY"
    assert params["Dept"] == "ART"
    assert params["CancelledCourses"] == "Exclude"


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
    assert result["fields"]["restriction_update_notes"]
    assert "Monday, August 24th, 2026 at noon" in (
        result["fields"]["restriction_update_notes"][0]
    )
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


def test_live_websoc_fetch_logs_url_response_and_parsed_result(caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.data.websoc_workflow")

    class FakeResponse:
        status_code = 200
        url = websoc_workflow.build_websoc_department_url("Fall 2026", "ART")
        text = _fixture("art_department.html")
        headers = {"content-type": "text/html; charset=utf-8"}

        def raise_for_status(self):
            return None

    class FakeSession:
        def get(self, url, **kwargs):
            assert url == websoc_workflow.WEBSOC_URL
            assert kwargs["timeout"] == websoc_workflow.REQUEST_TIMEOUT_S
            return type(
                "FormResponse",
                (),
                {
                    "status_code": 200,
                    "url": url,
                    "text": _fixture("search_form.html"),
                    "headers": {"content-type": "text/html"},
                    "raise_for_status": lambda self: None,
                },
            )()

        def post(self, url, **kwargs):
            assert url == websoc_workflow.WEBSOC_URL
            assert kwargs["data"]["Submit"] == "Display Web Results"
            assert kwargs["data"]["YearTerm"] == "2026-92"
            assert kwargs["data"]["Dept"] == "ART"
            assert kwargs["data"]["ShowComments"] == "on"
            return FakeResponse()

    result = websoc_workflow.fetch_websoc_department_restrictions(
        term="Fall 2026",
        department="ART",
        session=FakeSession(),
    )

    assert result["fields"]["major_restriction_removed_at"] == (
        "Monday, August 24th, 2026 at noon"
    )
    assert result["source_url"] == websoc_workflow.WEBSOC_URL
    assert result["request_method"] == "POST"
    assert result["request_form"]["Dept"] == "ART"
    assert result["response_term"] == "Fall 2026"
    assert result["validation"] == {"ok": True, "errors": []}
    assert "event=agent_web_fetch_started" in caplog.text
    assert f"url={result['source_url']!r}" in caplog.text
    assert "method='POST'" in caplog.text
    assert "trigger='agent'" in caplog.text
    assert "cache_hit=False" in caplog.text
    assert "event=agent_web_fetch_completed" in caplog.text
    assert "status_code=200" in caplog.text
    assert "event=agent_web_extraction_completed" in caplog.text
    assert "department='ART'" in caplog.text
    assert "comment_block_count=2" in caplog.text
    assert "major_restriction_removed_at" not in caplog.text
    assert "Monday, August 24th, 2026 at noon" not in caplog.text


def test_websoc_form_parser_keeps_exact_term_and_department_values() -> None:
    parsed = websoc_workflow._parse_websoc_form(_fixture("search_form.html"))

    assert parsed.terms == {
        "2026-92": "2026 Fall Quarter",
        "2026-14": "2026 Spring Quarter",
    }
    assert parsed.departments["ART"] == "ART - Art"
    assert parsed.departments["ARTS"] == "ARTS - Arts"
    assert parsed.departments["I&C SCI"] == (
        "I&C SCI - Information and Computer Science"
    )


@pytest.mark.parametrize(
    ("term", "department", "error_code"),
    [
        ("Winter 2026", "ART", "websoc_term_unavailable"),
        ("Fall 2026", "UNKNOWN", "websoc_department_unavailable"),
    ],
)
def test_websoc_rejects_values_missing_from_live_form(term, department, error_code) -> None:
    class FormResponse:
        status_code = 200
        url = websoc_workflow.WEBSOC_URL
        text = _fixture("search_form.html")
        headers = {"content-type": "text/html"}

        def raise_for_status(self):
            return None

    class FakeSession:
        def get(self, *_args, **_kwargs):
            return FormResponse()

        def post(self, *_args, **_kwargs):
            raise AssertionError("invalid form values must be rejected before POST")

    result = websoc_workflow.fetch_websoc_department_restrictions(
        term=term,
        department=department,
        session=FakeSession(),
    )

    assert result["ok"] is False
    assert result["error_code"] == error_code


@pytest.mark.parametrize(
    ("html", "error_code"),
    [
        (
            "<html><body><h1>Schedule of Classes</h1></body></html>",
            "websoc_not_search_results",
        ),
        (
            _fixture("art_department.html").replace("Department: ART", "Department: ARTS"),
            "websoc_department_mismatch",
        ),
        (
            _fixture("art_department.html").replace("Fall Quarter, 2026", "Spring Quarter, 2026"),
            "websoc_term_mismatch",
        ),
    ],
)
def test_parser_rejects_wrong_or_unverified_results(html, error_code) -> None:
    result = websoc_workflow.parse_websoc_department_html(
        html,
        term="Fall 2026",
        department="ART",
        source_url=websoc_workflow.WEBSOC_URL,
    )

    assert result["ok"] is False
    assert result["error_code"] == error_code
    assert result["validation"]["ok"] is False
    assert result["extraction_status"] == "invalid"


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


def test_deep_read_fetches_only_selected_official_websoc_links(caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.data.websoc_workflow")
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
              <p>Major restrictions will be removed on Monday, August 24th, 2026 at noon.</p>
              <p>A B restriction requires an authorization code from the instructor.</p>
              <a href="/academics/policies/">Student policies</a>
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
    assert result["selected_count"] == 1
    assert calls == [
        "http://ics.uci.edu/course-enrollment-restrictions/",
    ]
    assert result["pages"][0]["domain"] == "ics.uci.edu"
    assert result["pages"][0]["link_role"] == "ics_undergraduate_restrictions"
    assert "Major restrictions will be removed" in result["pages"][0]["text_excerpt"]
    assert result["pages"][0]["restriction_fields"][
        "major_restriction_removed_at"
    ] == "Monday, August 24th, 2026 at noon"
    assert result["pages"][0]["restriction_fields"]["authorization_code_notes"]
    assert result["pages"][0]["relevant_passages"]
    assert result["pages"][0]["content_block_count"] > 0
    assert result["pages"][0]["selected_block_count"] > 0
    assert result["pages"][0]["links"][0]["url"] == (
        "http://ics.uci.edu/academics/policies/"
    )
    assert result["restriction_evidence"][0]["fields"][
        "major_restriction_removed_at"
    ] == "Monday, August 24th, 2026 at noon"
    assert "event=websoc_linked_search_selected" in caplog.text
    assert "event=agent_web_fetch_started" in caplog.text
    assert "url='http://ics.uci.edu/course-enrollment-restrictions/'" in caplog.text
    assert "event=agent_web_fetch_completed" in caplog.text
    assert "event=agent_web_extraction_completed" in caplog.text
    assert "Monday, August 24th, 2026 at noon" not in caplog.text
    assert "event=websoc_linked_search_completed" in caplog.text


def test_query_aware_selection_keeps_duplicate_source_blocks() -> None:
    workflow_result = websoc_workflow.parse_websoc_department_html(
        _fixture("ics_department.html"),
        term="Fall 2026",
        department="I&C SCI",
        source_url=websoc_workflow.WEBSOC_URL,
    )
    workflow_result["restriction_type"] = "school_major"
    calls: list[str] = []

    class FakeResponse:
        status_code = 200
        url = "http://ics.uci.edu/course-enrollment-restrictions/"
        headers = {"content-type": "text/html"}
        text = """
        <main><p>9/18/2026</p><p>12:00pm</p>
        <p>School/Major restrictions are removed.</p></main>
        """

        def raise_for_status(self):
            return None

    class FakeSession:
        def get(self, url, **_kwargs):
            calls.append(url)
            return FakeResponse()

    result = websoc_workflow.fetch_linked_official_pages(
        workflow_result,
        session=FakeSession(),
    )

    assert calls == ["http://ics.uci.edu/course-enrollment-restrictions/"]
    assert result["pages"][0]["source_blocks"] == [
        "Donald Bren School of Information and Computer Sciences comments:",
        "Information and Computer Science department comments:",
    ]


def test_query_aware_selection_uses_graduate_or_policy_role() -> None:
    base = websoc_workflow.parse_websoc_department_html(
        _fixture("ics_department.html"),
        term="Fall 2026",
        department="I&C SCI",
        source_url=websoc_workflow.WEBSOC_URL,
    )

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<main><p>Official policy page.</p></main>"

        def __init__(self, url):
            self.url = url

        def raise_for_status(self):
            return None

    class FakeSession:
        def __init__(self):
            self.calls = []

        def get(self, url, **_kwargs):
            self.calls.append(url)
            return FakeResponse(url)

    graduate_session = FakeSession()
    graduate_result = {
        **base,
        "academic_level": "graduate",
        "restriction_type": "school_major",
    }
    websoc_workflow.fetch_linked_official_pages(
        graduate_result,
        session=graduate_session,
    )
    assert graduate_session.calls == [
        "http://ics.uci.edu/academics/graduate-academic-advising/course-updates/"
    ]

    policy_session = FakeSession()
    policy_result = {**base, "restriction_type": "add_drop_change"}
    websoc_workflow.fetch_linked_official_pages(
        policy_result,
        session=policy_session,
    )
    assert policy_session.calls == [
        "http://ics.uci.edu/academics/undergraduate-programs/majors-minors/"
        "undergraduate-student-policies/"
    ]


def test_course_specific_query_uses_one_allowlisted_second_hop() -> None:
    workflow_result = {
        "ok": True,
        "workflow_id": "websoc_department_restrictions",
        "term": "Fall 2026",
        "department": "I&C SCI",
        "course_id": "I&C SCI 139W",
        "restriction_type": "course_specific",
        "source_url": websoc_workflow.WEBSOC_URL,
        "fields": {},
        "links": [
            {
                "text": "Undergraduate restrictions",
                "url": "https://ics.uci.edu/course-enrollment-restrictions/",
                "allowed_for_deep_read": True,
                "link_role": "ics_undergraduate_restrictions",
                "source_block": "ICS comments:",
            }
        ],
    }
    calls: list[str] = []

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/html"}

        def __init__(self, url):
            self.url = url
            if "docs.google.com" in url:
                self.text = """
                <main><table><tr><td>I&amp;C SCI 139W</td>
                <td>9/20/2026 12:00pm</td>
                <td>course restriction is removed</td></tr></table></main>
                """
            else:
                self.text = """
                <main><p>See the
                <a href="https://docs.google.com/spreadsheets/d/official">
                restriction spreadsheet</a> for course details.</p></main>
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
    )

    assert calls == [
        "https://ics.uci.edu/course-enrollment-restrictions/",
        "https://docs.google.com/spreadsheets/d/official",
    ]
    assert result["pages"][1]["depth"] == 2
    assert result["pages"][1]["parent_url"] == calls[0]
    assert result["pages"][1]["timeline_events"][0]["course_scope"] == [
        "I&C SCI 139W"
    ]


def test_course_specific_query_rejects_non_allowlisted_second_hop() -> None:
    workflow_result = {
        "ok": True,
        "workflow_id": "websoc_department_restrictions",
        "term": "Fall 2026",
        "department": "I&C SCI",
        "course_id": "I&C SCI 139W",
        "restriction_type": "course_specific",
        "source_url": websoc_workflow.WEBSOC_URL,
        "fields": {},
        "links": [
            {
                "text": "Undergraduate restrictions",
                "url": "https://ics.uci.edu/course-enrollment-restrictions/",
                "allowed_for_deep_read": True,
                "link_role": "ics_undergraduate_restrictions",
                "source_block": "ICS comments:",
            }
        ],
    }
    calls: list[str] = []

    class FakeResponse:
        status_code = 200
        url = "https://ics.uci.edu/course-enrollment-restrictions/"
        headers = {"content-type": "text/html"}
        text = """
        <main><a href="https://example.com/restriction-spreadsheet">
        restriction spreadsheet</a></main>
        """

        def raise_for_status(self):
            return None

    class FakeSession:
        def get(self, url, **_kwargs):
            calls.append(url)
            return FakeResponse()

    result = websoc_workflow.fetch_linked_official_pages(
        workflow_result,
        session=FakeSession(),
    )

    assert calls == ["https://ics.uci.edu/course-enrollment-restrictions/"]
    assert result["pages"][0]["links"][0]["allowed_for_second_hop"] is False


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


def test_websoc_keeps_comment_links_but_excludes_course_table_links() -> None:
    html = """
    <html><body>
      <h1>Schedule of Classes search results</h1>
      <p>Department: ART</p><p>Fall Quarter, 2026</p>
      <div><b>Arts school comments:</b>
        <a href="https://arts.uci.edu/restrictions">Restriction details</a>
      </div>
      <div><b>Art department comments:</b>
        See <a href="https://arts.uci.edu/restrictions">Restriction details</a>.
      </div>
      <div>Art 1A</div>
      <table><tr><th>Code</th><th>Type</th><th>Sec</th><th>Units</th><th>Instructor</th></tr>
        <tr><td><a href="https://uci.bncollege.com/book">Bookstore</a></td></tr>
      </table>
    </body></html>
    """

    result = websoc_workflow.parse_websoc_department_html(
        html,
        term="Fall 2026",
        department="ART",
        source_url=websoc_workflow.WEBSOC_URL,
    )

    urls = [link["url"] for link in result["links"]]
    assert urls.count("https://arts.uci.edu/restrictions") == 2
    assert "https://uci.bncollege.com/book" not in urls
    assert "Bookstore" not in (result["department_comments"] or "")


def test_deep_read_deduplicates_same_comment_url() -> None:
    duplicate_link = {
        "text": "Restriction details",
        "url": "https://arts.uci.edu/restrictions/",
        "allowed_for_deep_read": True,
        "link_role": "restriction_details",
        "source_block": "Arts comments:",
    }
    workflow_result = {
        "ok": True,
        "workflow_id": "websoc_department_restrictions",
        "source_url": websoc_workflow.WEBSOC_URL,
        "fields": {"major_restriction_removed_at": None},
        "links": [duplicate_link, {**duplicate_link, "url": "http://arts.uci.edu/restrictions"}],
    }
    calls: list[str] = []

    class FakeResponse:
        text = """
        <html><head>
          <style>.restriction { display: block; }</style>
          <script>const restrictionNoise = 'ignore me';</script>
        </head><body>Major restrictions lift on September 1.</body></html>
        """
        url = "https://arts.uci.edu/restrictions/"
        headers = {"content-type": "text/html"}

        def raise_for_status(self):
            return None

    class FakeSession:
        def get(self, url, **_kwargs):
            calls.append(url)
            return FakeResponse()

    result = websoc_workflow.fetch_linked_official_pages(
        workflow_result,
        session=FakeSession(),
        max_pages=3,
    )

    assert result["selected_count"] == 1
    assert calls == ["https://arts.uci.edu/restrictions/"]
    assert "restrictionNoise" not in result["pages"][0]["text_excerpt"]
    assert "Major restrictions lift" in result["pages"][0]["text_excerpt"]


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
    class FormResponse:
        status_code = 200
        url = websoc_workflow.WEBSOC_URL
        text = _fixture("search_form.html")
        headers = {"content-type": "text/html"}

        def raise_for_status(self):
            return None

    class FailingSession:
        def get(self, *_args, **_kwargs):
            return FormResponse()

        def post(self, *_args, **_kwargs):
            raise requests.Timeout("slow")

    result = websoc_workflow.fetch_websoc_department_restrictions(
        term="Fall 2026",
        department="ART",
        session=FailingSession(),
    )

    assert result["ok"] is False
    assert result["workflow_id"] == "websoc_department_restrictions"
    assert result["error_code"] == "websoc_request_failed"
    assert result["source_url"] == "https://www.reg.uci.edu/perl/WebSoc"
    assert result["request_method"] == "POST"
    assert result["request_form"]["YearTerm"] == "2026-92"
    assert result["request_form"]["Dept"] == "ART"
    assert "Timeout" in result["message"]


def test_build_websoc_department_params_rejects_missing_department() -> None:
    with pytest.raises(ValueError, match="department is required"):
        websoc_workflow.build_websoc_department_params("Fall 2026", "")
