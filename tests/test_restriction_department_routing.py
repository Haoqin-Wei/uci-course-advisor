"""Department handling for restriction questions (regression, 2026-11-12).

"计算机系的课什么时候对外专业开放" used to get a canned "which department?"
reply because the router only knew English department aliases.
"""
from __future__ import annotations

import pytest

from app.agent import loop
from app.agent.workflow_router import (
    build_primary_workflow_plan,
    build_route_hint_message,
    route_solution,
)

TERM = "2027 Winter"


def _plan(message: str) -> dict:
    return build_primary_workflow_plan(route_solution(message, term=TERM))


@pytest.mark.parametrize(
    ("message", "department"),
    [
        ("计算机系的课什么时候对外专业开放", "COMPSCI"),
        ("计算机科学专业的课外专业能选吗", "COMPSCI"),
        ("信息与计算机科学学院的课外专业能选吗", "I&C SCI"),
        ("数学系的专业限制什么时候解除", "MATH"),
        ("统计学系的课什么时候对全校开放", "STATS"),
    ],
)
def test_chinese_department_name_runs_the_primary_tool(message: str, department: str) -> None:
    plan = _plan(message)
    assert not plan.get("clarification")
    assert not plan.get("deferred")
    assert [call["args"]["department"] for call in plan["calls"]] == [department]


def test_longer_chinese_name_is_not_also_read_as_the_shorter_one() -> None:
    route = route_solution("信息与计算机科学学院的课外专业能选吗", term=TERM)
    assert route["departments"] == ["I&C SCI"]


@pytest.mark.parametrize(
    "message",
    [
        "生物化学系的课什么时候对外专业开放",  # real department, not in the short list
        "专业限制什么时候解除",  # no department at all
        "计算机相关的课外专业能选吗",  # not a department name
    ],
)
def test_unrecognised_department_goes_to_the_model_not_a_canned_question(message: str) -> None:
    route = route_solution(message, term=TERM)
    plan = build_primary_workflow_plan(route)
    assert plan["calls"] == []
    assert not plan.get("clarification")
    assert plan["deferred"] == {"reason": "missing_department"}
    hint = build_route_hint_message(route)["content"]
    assert "did not run the primary workflow" in hint
    assert "Do not repeat those primary calls" not in hint


def test_two_departments_still_ask_which_one() -> None:
    plan = _plan("ICS 和计算机系的课什么时候对全校开放")
    assert plan["calls"] == []
    assert plan["clarification"]["reason"] == "ambiguous_department"


def test_recognised_department_hint_still_says_server_ran_the_tool() -> None:
    hint = build_route_hint_message(route_solution("计算机系的课什么时候对外专业开放", term=TERM))["content"]
    assert "Do not repeat those primary calls" in hint


def test_research_summary_counts_only_real_requests() -> None:
    result = {
        "fetch_summary": [
            {"url": "https://www.reg.uci.edu/perl/WebSoc", "ok": True, "local": True},
            {"url": "https://ics.uci.edu/course-enrollment-restrictions/", "ok": True, "cached": True},
            {"url": "https://example.uci.edu/notice", "ok": True},
            {"url": "https://example.uci.edu/broken", "ok": False},
        ]
    }
    summary = loop._agent_web_research_summary("get_department_restrictions", result)
    assert summary == {
        "fetched_urls": ["https://example.uci.edu/notice", "https://example.uci.edu/broken"],
        "success_count": 1,
        "failure_count": 1,
        "reused_count": 2,
    }


def test_research_summary_with_everything_reused_reports_no_fetch() -> None:
    result = {
        "fetch_summary": [
            {"url": "https://www.reg.uci.edu/perl/WebSoc", "ok": True, "local": True},
            {"url": "https://ics.uci.edu/course-enrollment-restrictions/", "ok": True, "cached": True},
        ]
    }
    summary = loop._agent_web_research_summary("get_department_restrictions", result)
    assert summary["fetched_urls"] == []
    assert summary["success_count"] == 0
    assert summary["reused_count"] == 2
