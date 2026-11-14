"""Restriction dates that schools write directly into the WebSoc notices.

Regression (2026-11-12). Asked when Economics opens to other majors, Solon
answered "cannot verify" although the School of Social Sciences notice says
"... restricted to School or majors only until MONDAY, DECEMBER 7th by noon".
Only one fixed wording ("Major restrictions ... removed on ...") was read, and
in the stored Winter 2027 term it matched no department at all.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent import loop as agent_loop
from app.agent import tools as agent_tools
from app.catalog import store
from app.catalog.sync import sync_term
from app.data import websoc_workflow
from app.data.restriction_notices import enrollment_year, read_notice_restrictions
from app.data.restriction_timeline import RestrictionQuery, RestrictionType

WINTER = "2027 Winter"
FALL = "2026 Fall"
SOURCE = "https://www.reg.uci.edu/perl/WebSoc"


def _read(text: str, *, term: str = WINTER, scope: str = "school", department: str = "ECON"):
    result = {"source_url": SOURCE, "retrieved_at": "2026-11-13T00:00:00Z",
              "comment_blocks": [{"block_type": scope, "text": text}]}
    query = RestrictionQuery(term=term, department=department, course_id=None,
                             restriction_type=RestrictionType.SCHOOL_MAJOR)
    return read_notice_restrictions(result, query)


def _dates(text: str, **kwargs) -> list[tuple[str, str]]:
    events, _ = _read(text, **kwargs)
    return [(event.restriction_type.value, event.effective_at) for event in events]


@pytest.mark.parametrize(
    ("sentence", "term", "expected"),
    [
        # Social Sciences, Social Ecology: no year, weekday given, line break before the date.
        ("RESTRICTIONS: Due to the volume of majors, enrollment in some School of Social Sciences courses will be "
         "restricted to School or majors only until\nMONDAY, DECEMBER 7th by noon.", WINTER,
         [("school_major", "2026-12-07T12:00:00-08:00")]),
        ("Due to the volume of majors, enrollment in some School of Social Ecology courses will be restricted to "
         "School or majors only until Monday, December 7, 2026 at noon.", WINTER,
         [("school_major", "2026-12-07T12:00:00-08:00")]),
        # Arts: "removed beginning at noon on", the time comes before the date.
        ("RESTRICTIONS: School/Major restrictions for some Arts courses will be removed beginning at noon on "
         "December 10, 2026.", WINTER, [("school_major", "2026-12-10T12:00:00-08:00")]),
        # Chemistry, Math: "lifted on", clock time, stray space before the full stop.
        ("Major restrictions for Chemistry courses will be lifted on Monday, December 21, by 12:00pm .", WINTER,
         [("school_major", "2026-12-21T12:00:00-08:00")]),
        ("Major restrictions will be lifted on December 18th, by 12:00pm.", WINTER,
         [("school_major", "2026-12-18T12:00:00-08:00")]),
        # Biological Sciences: "will lift", with a parenthesis before the date.
        ("These major specific restrictions will lift (with the exception of major specific seminar courses) "
         "FRIDAY, DECEMBER 11th by noon.", WINTER, [("school_major", "2026-12-11T12:00:00-08:00")]),
        # Pharmacy: "not be able to enroll ... until:", and a date with no time stays a date.
        ("MAJOR RESTRICTION: Students who are in majors other than PharmSci will not be able to enroll in major "
         "restricted classes until: December 18, 2026.", WINTER, [("school_major", "2026-12-18")]),
        ("Major restrictions will be lifted on December 4, 2026.", WINTER, [("school_major", "2026-12-04")]),
        # Fall: daylight time, and New Only statements are their own kind.
        ("Major restrictions for some courses will be removed on Monday, August 24th, 2026 at noon.", FALL,
         [("school_major", "2026-08-24T12:00:00-07:00")]),
        ("New Only Restrictions (NORs) will be removed on Friday, August 21st at noon.", FALL,
         [("new_only", "2026-08-21T12:00:00-07:00")]),
        ('"New Only" status will be lifted on Friday, September 4 by 12:00pm.', FALL,
         [("new_only", "2026-09-04T12:00:00-07:00")]),
        ("REPEAT STUDENTS: If you wish to repeat an undergraduate course, please note the repeat restriction will "
         "be lifted on MONDAY, DECEMBER 7th by noon.", WINTER, [("repeat", "2026-12-07T12:00:00-08:00")]),
        # One sentence about two kinds.
        ("RESTRICTIONS: The major and repeat restrictions will lift DECEMBER 18 by noon.", WINTER,
         [("school_major", "2026-12-18T12:00:00-08:00"), ("repeat", "2026-12-18T12:00:00-08:00")]),
    ],
)
def test_each_schools_wording_yields_its_date(sentence: str, term: str, expected: list) -> None:
    assert _dates(sentence, term=term) == expected


def test_a_missing_year_comes_from_the_terms_enrollment_period() -> None:
    assert enrollment_year("2027 Winter", 12) == 2026          # winter enrollment is the autumn before
    assert enrollment_year("2027 Winter", 1) == 2027
    assert enrollment_year("Spring 2027", 2) == 2027 and enrollment_year("Spring 2027", 11) == 2026
    assert enrollment_year("2026 Fall", 8) == 2026
    # The same sentence a year earlier: December 8, 2025 was the Monday.
    assert _dates("... will be restricted to majors only until MONDAY, DECEMBER 8th by noon.", term="2026 Winter") == [
        ("school_major", "2025-12-08T12:00:00-08:00")]
    assert _dates("The repeat restriction for MOST courses will be lifted on Monday, January 5, by 12:00pm.",
                  term="2026 Winter") == [("repeat", "2026-01-05T12:00:00-08:00")]


@pytest.mark.parametrize(
    "sentence",
    [
        "Major restrictions will be removed AFTER the fee deadline.",                        # no date at all
        "Major restrictions will be lifted on TUESDAY, DECEMBER 7th by noon.",               # Dec 7, 2026 is a Monday
        "Major restrictions will be lifted on December 7, 2024 at noon.",                    # a year from another term
        "Major restrictions will be lifted on February 30th.",                               # not a date
        "Math majors will have first priority to enroll in all upper division courses until the restriction is lifted.",
    ],
)
def test_a_date_that_cannot_be_trusted_is_quoted_not_dated(sentence: str) -> None:
    events, statements = _read(sentence)

    assert events == []
    assert statements == [{"scope": "school", "restriction_type": "school_major", "text": sentence,
                           "effective_at": None}]


@pytest.mark.parametrize(
    "sentence",
    [
        "EXCESS UNITS: All students are restricted to 18-units initially, and will automatically lift to 20 units.",
        "Your web registration restriction will be removed after the interview.",
        "The Student Affairs Office will not do this until the University restriction has been lifted, which will "
        "be on Friday, December 18, 2026.",
        'For courses with a "B" or "X" restriction, please contact the instructor of the course.',
        "ADD: The deadline to add courses in the School of Social Sciences is FRIDAY of Week 2 by 5:00PM.",
    ],
)
def test_other_kinds_of_restriction_are_left_alone(sentence: str) -> None:
    assert _read(sentence) == ([], [])


def test_a_departments_own_date_replaces_its_schools_general_one() -> None:
    result = {"source_url": SOURCE, "comment_blocks": [
        {"block_type": "school", "text": "RESTRICTIONS: The major and repeat restrictions will lift DECEMBER 18 by noon."},
        {"block_type": "department", "text": "Major restrictions will be lifted on December 4, 2026."},
    ]}
    query = RestrictionQuery(term=WINTER, department="PHILOS", course_id=None,
                             restriction_type=RestrictionType.SCHOOL_MAJOR)

    events, statements = read_notice_restrictions(result, query)

    assert [(e.restriction_type.value, e.effective_at, e.source_position["block_type"]) for e in events] == [
        ("repeat", "2026-12-18T12:00:00-08:00", "school"),       # the department says nothing about repeats
        ("school_major", "2026-12-04", "department"),
    ]
    assert len(statements) == 3                                   # every sentence is still available word for word


# ── Through the tool, on a stored term ────────────────────────────────────

def _department(code: str, name: str, comment: str, index: int) -> dict:
    section = {"sectionCode": str(62000 + index), "sectionType": "Lec", "sectionNum": "A", "units": "4",
               "status": "OPEN", "instructors": ["STAFF"], "maxCapacity": "100",
               "numCurrentlyEnrolled": {"totalEnrolled": "0", "sectionEnrolled": ""}, "isCancelled": False,
               "meetings": [{"timeIsTBA": True}], "finalExam": {}}
    return {"deptCode": code, "deptName": name, "deptComment": comment,
            "courseNumberRangeComments": [], "sectionCodeRangeComments": [],
            "courses": [{"deptCode": code, "courseNumber": "20A", "courseTitle": "INTRO",
                         "courseComment": "", "sections": [section]}]}


SOCSCI_NOTE = ("<p><b>ADD:</b> The deadline to add courses in the School of Social Sciences is FRIDAY of Week 2 by "
               "5:00PM.</p><p><b>RESTRICTIONS:</b> Due to the volume of majors, enrollment in some School of Social "
               "Sciences courses will be restricted to School or majors only until <b>MONDAY, DECEMBER 7th by "
               "noon.</b></p><p>REPEAT STUDENTS: If you wish to repeat an undergraduate course, please note the "
               "repeat restriction will be lifted on <b>MONDAY, DECEMBER 7th by noon</b>.</p>")
ENGR_NOTE = "<p>Major restrictions will be removed AFTER the fee deadline.</p>"
EDUC_NOTE = ("<p><b>RESTRICTIONS:</b> Upper-Division Education classes will be restricted to majors only until "
             "November 20, 2026 at noon. Lower-Division Education classes will be restricted to majors only until "
             "December 2, 2026 at noon.</p>")


@pytest.fixture
def winter(monkeypatch):
    schools = [
        {"schoolName": "School of Social Sciences", "schoolComment": SOCSCI_NOTE, "departments": [
            _department("ECON", "Economics", "<p>Departmental drop/add policy</p>", 0),
            _department("ART HIS", "Art History", "", 1)]},
        {"schoolName": "Henry Samueli School of Engineering", "schoolComment": ENGR_NOTE, "departments": [
            _department("BME", "Biomedical Engineering", "", 2)]},
        {"schoolName": "School of Education", "schoolComment": EDUC_NOTE, "departments": [
            _department("EDUC", "Education", "", 3)]},
    ]
    assert sync_term("2027_Winter", fetcher=lambda term: SimpleNamespace(
        ok=True, status="ok", error=None, data={"schools": schools})).ok

    def no_request(**_kwargs):
        raise AssertionError("the Registrar page must not be requested for a stored department")

    monkeypatch.setattr(agent_tools.websoc_workflow, "fetch_websoc_department_restrictions", no_request)


def _lookup(department: str) -> dict:
    return agent_tools.dispatch(
        "get_department_restrictions",
        {"term": WINTER, "department": department, "restriction_type": "major_restriction"},
        context={"allowed_query_terms": [WINTER]},
    )


def test_economics_gets_the_date_its_school_notice_states(winter) -> None:
    result = _lookup("ECON")

    bundle = result["evidence_bundle"]
    assert bundle["evidence_status"] == "verified" and bundle["conflicts"] == []
    primary = result["verified_facts"]["primary"]
    assert (primary["restriction_type"], primary["effective_at"]) == ("school_major", "2026-12-07T12:00:00-08:00")
    assert primary["source_url"] == SOURCE and primary["source_role"] == "registrar_websoc_comments"
    summary = result["verified_facts"]["summary_markdown"]
    assert "2026-12-07 12:00" in summary
    # The notice's own limits stay visible: it says "some ... courses".
    assert "enrollment in some School of Social Sciences courses will be restricted" in summary
    assert "未能" not in summary and "例外" not in summary
    # The repeat date is a different kind of restriction, not a second answer.
    assert [event["restriction_type"] for event in bundle["events"]] == ["school_major", "repeat"]
    local = result["fetch_summary"][0]
    assert local["local"] is True and local["provides_evidence"] is True      # the stored notices are the evidence
    assert "comment_blocks" not in result and "school_comments" not in result


def test_a_department_named_in_full_is_looked_up_by_its_registrar_code(winter) -> None:
    # 2026-11-12: the model asked for "ECONOMICS", the lookup failed, and it had to call again with "ECON".
    for given in ("ECONOMICS", "economics", " Economics "):
        result = _lookup(given)
        assert result["ok"] is True and result["department"] == "ECON"
        assert result["notice_source"] == "catalog_store_snapshot"
    assert _lookup("Art History")["department"] == "ART HIS"                  # the term's official name
    assert store.department_names("2027_Winter")["biomedical engineering"] == "BME"
    assert websoc_workflow.registrar_department_code(WINTER, "bme") == "BME"
    assert websoc_workflow.registrar_department_code(WINTER, "No Such Dept") == "NO SUCH DEPT"
    assert store.department_names("2031_Fall") == {}


def test_a_notice_without_a_date_is_quoted_instead_of_cannot_verify(winter) -> None:
    result = _lookup("BME")

    assert result["evidence_bundle"]["evidence_status"] == "unavailable"
    assert result["notice_statements"] == [{
        "scope": "school", "restriction_type": "school_major", "effective_at": None,
        "text": "Major restrictions will be removed AFTER the fee deadline."}]
    summary = result["verified_facts"]["summary_markdown"]
    assert "**WebSoc 学院公告原文：**“Major restrictions will be removed AFTER the fee deadline.”" in summary
    assert "未能从已抓取的官方来源验证" not in summary


def test_several_dates_in_one_notice_are_listed_not_reduced_to_one(winter) -> None:
    result = _lookup("EDUC")

    assert result["evidence_bundle"]["evidence_status"] == "verified"         # a list, not a contradiction
    assert result["evidence_bundle"]["conflicts"] == []
    summary = result["verified_facts"]["summary_markdown"]
    assert "写了 2 个时间" in summary
    upper, lower = [line for line in summary.split("\n") if line.startswith("- ")]
    assert upper.startswith("- 2026-11-20 12:00") and "Upper-Division Education classes" in upper
    assert lower.startswith("- 2026-12-02 12:00") and "Lower-Division Education classes" in lower


def test_the_servers_own_lookup_also_hands_the_model_the_notice_sentences(winter) -> None:
    record = {"tool": "get_department_restrictions", "result": _lookup("BME")}

    compact = agent_loop._compact_workflow_record(record)["result"]

    assert compact["notice_statements"][0]["text"] == "Major restrictions will be removed AFTER the fee deadline."


def test_agent_prompt_tells_the_model_to_use_the_notice_sentences() -> None:
    from app.llm.adapter import AGENT_SYSTEM_PROMPT

    flat = " ".join(AGENT_SYSTEM_PROMPT.split())
    assert "`notice_statements` are the WebSoc notice's own sentences about restrictions" in flat
    assert "never answer that nothing is listed" in flat
