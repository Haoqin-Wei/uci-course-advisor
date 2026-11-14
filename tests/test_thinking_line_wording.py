"""What the thinking line says, checked by running static/js/activity.js in Node."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
ACTIVITY_JS = STATIC / "js" / "activity.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")

WEBSOC = {"url": "https://www.reg.uci.edu/perl/WebSoc", "local": True, "source_role": "catalog_store_snapshot",
          "snapshot_synced_at": "2026-11-12T04:00:00Z", "ok": True}
PAGE = {"url": "http://ics.uci.edu/course-enrollment-restrictions/", "host": "ics.uci.edu", "ok": True,
        "source_role": "undergrad_restrictions", "status_code": 200, "bytes": 186662, "duration_ms": 412.4,
        "provides_evidence": True}
PAGE_CACHED = {**PAGE, "cached": True, "cached_at": "2026-11-12T20:00:00Z", "duration_ms": 0}


def call(function: str, *args):
    script = (
        f"const a = require({json.dumps(str(ACTIVITY_JS))});"
        f"const out = a[{json.dumps(function)}](...{json.dumps(list(args), ensure_ascii=False)});"
        "process.stdout.write(JSON.stringify(out === undefined ? null : out));"
    )
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(done.stdout)


def restriction_steps(zh: bool) -> list[dict]:
    action = "读取 WebSoc 部门说明" if zh else "Reading WebSoc department notes"
    term = "2027年冬季" if zh else "2027 Winter"
    return [
        {"kind": "term", "terms": ["2027 Winter"], "ok": True},
        {"kind": "tool", "name": "get_department_restrictions", "label": f"{action} · I&C SCI · {term}",
         "ok": True, "duration_ms": 2000, "sources": [WEBSOC, PAGE]},
        {"kind": "tool", "name": "get_department_restrictions", "label": f"{action} · COMPSCI · {term}",
         "ok": True, "duration_ms": 500, "sources": [WEBSOC, PAGE_CACHED]},
    ]


def test_settled_line_gives_the_time_and_a_short_account_in_the_users_language() -> None:
    assert call("activityHeaderText", {"thinking_ms": 14200, "steps": restriction_steps(True)}, True) == (
        "已思考 14 秒 · 读取了选课限制说明，抓取了 1 个网页")
    assert call("activityHeaderText", {"thinking_ms": 14200, "steps": restriction_steps(False)}, False) == (
        "Thought for 14s · read the enrollment restriction notes, fetched 1 web page")
    # No tools: only the time. Long turns read in minutes.
    assert call("activityHeaderText", {"thinking_ms": 2300, "steps": restriction_steps(True)[:1]}, True) == "已思考 2 秒"
    assert call("activityHeaderText", {"thinking_ms": 400, "steps": []}, False) == "Thought for 1s"
    assert call("activityDuration", 95_400, True) == "1 分 35 秒"
    assert call("activityDuration", 95_400, False) == "1m 35s"


def test_a_page_fetched_once_and_reused_later_counts_as_one_request() -> None:
    # The old audit only showed the last tool call: "0 requests, 2 reused".
    steps = restriction_steps(True)
    assert call("activityRequestCount", steps) == 1
    assert call("activityRequestCount", list(reversed(steps))) == 1
    assert call("activityRequestCount", steps[2:]) == 0
    sources = call("activityAllSources", steps)
    assert [source["url"] for source in sources] == [WEBSOC["url"], PAGE["url"]]
    assert "cached" not in sources[1]


def test_account_names_at_most_three_kinds_of_work() -> None:
    names = ["search_courses", "get_sections", "get_zotistics_grades", "get_professor_rating", "propose_recommendation"]
    steps = [{"kind": "tool", "name": name, "label": name, "ok": True} for name in names]
    steps.append({"kind": "tool", "name": "get_sections", "label": "again", "ok": True})

    assert call("activitySummary", steps, True) == "搜索了课程，查了排课，查了历史给分等"
    assert call("activitySummary", steps, False) == "searched courses, checked sections, checked grade history, and more"
    assert call("activitySummary", [{"kind": "tool", "name": "brand_new_tool", "ok": True}], False) == "used a tool"


def test_failed_steps_are_flagged_on_the_settled_line() -> None:
    steps = [{"kind": "tool", "name": "web_search", "label": "联网搜索 · uci", "ok": False}]

    # A failed step is counted, not described as work that was done.
    assert call("activityHeaderText", {"thinking_ms": 3000, "steps": steps}, True) == "已思考 3 秒（1 步未完成）"
    assert call("activityHeaderText", {"thinking_ms": 3000, "steps": steps}, False) == "Thought for 3s (1 step failed)"
    interrupted = [{"kind": "tool", "name": "web_search", "label": "联网搜索 · uci"}]
    assert call("activityHeaderText", {"thinking_ms": 3000, "steps": interrupted}, True) == "已思考 3 秒"
    assert call("activityRowText", steps[0], True, False) == {
        "lead": "未完成：联网搜索", "rest": "uci", "state": "failed"}


def test_rows_read_as_what_was_done_and_to_what() -> None:
    term, first, _ = restriction_steps(True)

    assert call("activityRowText", term, True, False) == {"lead": "已确定查询学期", "rest": "2027 Winter", "state": "done"}
    assert call("activityRowText", first, True, False) == {
        "lead": "已读取 WebSoc 部门说明", "rest": "I&C SCI · 2027年冬季", "state": "done"}
    assert call("activityRowText", restriction_steps(False)[1], False, False) == {
        "lead": "Read WebSoc department notes", "rest": "I&C SCI · 2027 Winter", "state": "done"}
    running = {"kind": "tool", "name": "get_sections", "label": "Checking COMPSCI 161 sections · 2027 Winter"}
    assert call("activityRowText", running, False, True)["lead"] == "Checking COMPSCI 161 sections"
    assert call("activityRowText", running, False, False) == {
        "lead": "Interrupted: Checking COMPSCI 161 sections", "rest": "2027 Winter", "state": "interrupted"}


@pytest.mark.parametrize(
    ("label", "done"),
    [
        ("Looking up COMPSCI 161 course details", "Looked up COMPSCI 161 course details"),
        ("Checking offerings and instructors · ICS 33", "Checked offerings and instructors · ICS 33"),
        ("Summarizing professor tags · Pattis", "Summarized professor tags · Pattis"),
        ("Searching courses (COMPSCI)", "Searched courses (COMPSCI)"),
        ("Listing university policy topics", "Listed university policy topics"),
        ("Preparing 5 recommendation cards", "Prepared 5 recommendation cards"),
        ("Calling some_tool", "Called some_tool"),
    ],
)
def test_english_labels_turn_from_doing_to_done(label: str, done: str) -> None:
    assert call("activityDoneLabel", label, False) == done
    assert call("activityRunningLabel", label, False) == label
    assert call("activityDoneLabel", "查询 CS 161 排课", True) == "已查询 CS 161 排课"
    assert call("activityRunningLabel", "查询 CS 161 排课", True) == "正在查询 CS 161 排课"


def test_every_tool_label_the_server_writes_has_a_past_tense() -> None:
    from app.agent.tools import TOOL_SCHEMAS, humanize_tool_call

    args = {"course_id": "COMPSCI 161", "instructor_name": "Pattis", "department": "COMPSCI", "term": "2027 Winter",
            "terms": ["2027 Winter"], "topic": "add drop", "query": "uci", "url": "https://uci.edu",
            "course_a": "ICS 31", "course_b": "ICS 32", "items": [1, 2]}
    for schema in TOOL_SCHEMAS:
        name = schema["function"]["name"]
        label = humanize_tool_call(name, args, "en")
        assert call("activityDoneLabel", label, False) != label, f"{name}: no past tense for {label!r}"


def test_section_lookups_say_what_they_found() -> None:
    base = {"kind": "tool", "name": "get_sections", "label": "查询 CS 161 排课", "ok": True}

    assert call("activityRowText", {**base, "section_count": 3}, True, False)["rest"] == "找到 3 个教学班"
    assert call("activityStepResult", {**base, "offering_status": "not_offered", "authoritative": True}, True) == "官方无匹配"
    assert call("activityStepResult", {**base, "offering_status": "not_offered"}, False) == "No sections found"
    assert call("activityStepResult", {**base, "offering_status": "unavailable"}, True) == "数据不可用"
    assert call("activityStepResult", {**base, "name": "get_policy", "section_count": 3}, True) == ""


def test_sources_say_how_each_was_read() -> None:
    local = call("activitySourceFacts", WEBSOC, True)
    fetched = call("activitySourceFacts", PAGE, True)
    reused = call("activitySourceFacts", PAGE_CACHED, False)

    assert local[0] == "本地课表快照中的院系公告" and local[1].startswith("未发请求，同步于")
    assert fetched == ["本科限制官方页", "HTTP 200", "186,662 B", "412 ms", "用于最终事实"]
    assert reused[1].startswith("Fetched within 24 h, not requested again;") and "412 ms" not in reused


def test_turns_saved_before_steps_were_recorded_still_show_their_sources() -> None:
    legacy = call("activityFromLegacy", [WEBSOC, PAGE])

    assert legacy["thinking_ms"] is None and legacy["steps"][0]["sources"] == [WEBSOC, PAGE]
    assert call("activityHeaderText", legacy, True) == "思考过程 · 读取了来源，抓取了 1 个网页"
    assert call("activityRowText", legacy["steps"][0], False, False) == {"lead": "Read sources", "rest": "", "state": "done"}
    assert call("activityFromLegacy", []) is None


def test_page_loads_the_thinking_line_before_the_chat_script_and_styles_it() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    components = (STATIC / "styles" / "components.css").read_text(encoding="utf-8")

    assert index.index("/static/js/activity.js") < index.index("/static/js/chat.js")
    assert ".thinking-line.is-live .thinking-text" in components and "animation: shimmer" in components
    assert "prefers-reduced-motion" in components[components.index(".thinking-line.is-live"):]
    assert ".tool-chip" not in components and ".tool-fetch" not in components
