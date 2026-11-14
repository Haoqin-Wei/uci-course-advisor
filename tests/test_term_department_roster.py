"""The model picks department codes from the queried term's own roster.

Psychology was PSYCH, then PSCI, and is PSY in Winter 2027. A student who
writes "psycho" was sent to PSYCH, every lookup failed without saying why,
and the turn ran out of rounds after a minute.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent import loop, tools, workflow_router
from app.catalog import term_departments
from app.catalog.sync import sync_term
from app.data import websoc_workflow

TERM = "2027 Winter"
TERM_ID = "2027_Winter"


def _department(code: str, name: str, index: int) -> dict:
    section = {"sectionCode": str(10000 + index), "sectionType": "Lec", "sectionNum": "A", "units": "4",
               "status": "OPEN", "instructors": ["STAFF"], "maxCapacity": "100",
               "numCurrentlyEnrolled": {"totalEnrolled": "0", "sectionEnrolled": ""}, "isCancelled": False,
               "meetings": [{"timeIsTBA": True}], "finalExam": {}}
    return {
        "deptCode": code, "deptName": name, "deptComment": "",
        "courseNumberRangeComments": [], "sectionCodeRangeComments": [],
        "courses": [{"deptCode": code, "courseNumber": "1", "courseTitle": "INTRO",
                     "courseComment": "", "sections": [section]}],
    }


@pytest.fixture
def winter(tmp_path, monkeypatch):
    path = tmp_path / "catalog.db"
    monkeypatch.setenv("CATALOG_DB_PATH", str(path))
    monkeypatch.setenv("CATALOG_AUTO_SYNC_ENABLED", "false")
    departments = [("PSY", "Psychology"), ("PSMD", "Psychiatry"),
                   ("ECON", "Economics"), ("ART HIS", "Art History")]
    data = {"schools": [{"schoolName": "School", "schoolComment": "", "departments": [
        _department(code, name, index) for index, (code, name) in enumerate(departments)]}]}
    assert sync_term(TERM_ID, fetcher=lambda term: SimpleNamespace(
        ok=True, status="ok", error=None, data=data)).ok
    return path


def test_roster_lists_this_terms_codes_and_names(winter):
    assert term_departments.roster(TERM) == {
        "ART HIS": "Art History", "ECON": "Economics",
        "PSMD": "Psychiatry", "PSY": "Psychology",
    }


def test_prompt_block_gives_the_model_code_and_name(winter):
    block = term_departments.prompt_block([TERM])
    assert 'term="2027 Winter"' in block
    assert "PSY=Psychology" in block and "PSMD=Psychiatry" in block
    assert "never use a code from memory" in block


def test_no_roster_means_no_block_and_no_refusal(winter):
    assert term_departments.prompt_block(["2026 Fall"]) == ""
    assert term_departments.unknown_department("2026 Fall", "PSYCH") is None


def test_official_name_wins_over_a_retired_alias(winter):
    # The alias table still says psychology → PSYCH.
    assert websoc_workflow.registrar_department_code(TERM, "psychology") == "PSY"
    assert websoc_workflow.registrar_department_code(TERM, "Art History") == "ART HIS"
    assert websoc_workflow.registrar_department_code(TERM, "econ") == "ECON"


def test_retired_code_is_refused_with_the_roster_and_no_request(winter, monkeypatch):
    def no_network(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("a request was made for a department the term does not have")

    monkeypatch.setattr(websoc_workflow, "search_department_restrictions", no_network, raising=False)
    monkeypatch.setattr(tools, "_tool_get_department_restrictions", no_network)
    context = {"term": TERM, "allowed_query_terms": [TERM]}
    result = tools.dispatch(
        "get_department_restrictions", {"term": TERM, "department": "PSYCH"}, context=context,
    )
    assert result["ok"] is False
    assert result["error_code"] == "department_not_in_term"
    assert "PSY=Psychology" in result["departments_this_term"]


@pytest.mark.parametrize("name, args", [
    ("search_courses", {"term": TERM, "department": "PSYCH"}),
    ("get_sections", {"term": TERM, "course_id": "PSYCH 7A"}),
])
def test_other_lookups_refuse_the_retired_code_too(winter, name, args):
    result = tools.dispatch(name, args, context={"term": TERM, "allowed_query_terms": [TERM]})
    assert result["error_code"] == "department_not_in_term"


def test_a_listed_department_is_not_refused(winter):
    assert tools._department_not_in_term(
        "get_sections", {"term": TERM, "course_id": "PSY 21"}, {"term": TERM}) is None
    assert tools._department_not_in_term(
        "search_courses", {"term": TERM, "department": "psychology"}, {"term": TERM}) is None


def test_router_defers_a_retired_alias_to_the_model(winter):
    route = {
        "route_type": "workflow", "workflow_ids": ["websoc_department_restrictions"],
        "term": TERM, "departments": ["PSYCH"], "course_ids": [],
    }
    plan = workflow_router.build_primary_workflow_plan(route)
    assert plan["calls"] == []
    assert plan["deferred"] == {"reason": "department_not_in_term"}


def test_router_still_runs_a_listed_department(winter):
    route = {
        "route_type": "workflow", "workflow_ids": ["websoc_department_restrictions"],
        "term": TERM, "departments": ["ECON"], "course_ids": [],
    }
    plan = workflow_router.build_primary_workflow_plan(route)
    assert plan["calls"][0]["args"]["department"] == "ECON"


def test_same_failed_lookup_has_one_identity_whatever_the_restriction_type():
    first = loop._failed_call_key(
        "get_department_restrictions",
        {"term": TERM, "department": "PSYCH", "restriction_type": "major_restriction"})
    second = loop._failed_call_key(
        "get_department_restrictions", {"term": TERM, "department": "PSYCH"})
    other = loop._failed_call_key(
        "get_department_restrictions", {"term": TERM, "department": "PSY"})
    assert first == second != other


def test_repeated_failure_tells_the_model_to_change_course():
    result = loop._repeated_failure_result({"error_code": "department_not_in_term"})
    assert result["ok"] is False
    assert result["error_code"] == "repeated_failed_call"
    assert result["previous_error"] == {"error_code": "department_not_in_term"}
