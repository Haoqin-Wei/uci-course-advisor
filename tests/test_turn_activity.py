"""The record behind the thinking line: steps, their sources, thinking time."""
from __future__ import annotations

import asyncio

from app.activity import ActivityRecorder, ObservedQueue, count_requests, merge_sources
from app.data import sessions as sessions_data

WEBSOC = {"method": "LOCAL", "url": "https://www.reg.uci.edu/perl/WebSoc", "ok": True, "local": True,
          "source_role": "catalog_store_snapshot", "snapshot_synced_at": "2026-11-12T04:00:00Z"}
PAGE = {"method": "GET", "url": "http://ics.uci.edu/course-enrollment-restrictions/", "ok": True,
        "source_role": "undergrad_restrictions", "status_code": 200, "bytes": 186662, "duration_ms": 412.0,
        "provides_evidence": True}
PAGE_CACHED = {**PAGE, "cached": True, "cached_at": "2026-11-12T20:00:00Z", "duration_ms": 0.0}


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def _start(name: str, label: str) -> dict:
    return {"type": "tool_call_start", "name": name, "label": label, "args": {"secret": "not kept"}}


def _done(name: str, **extra) -> dict:
    return {"type": "tool_call_done", "name": name, "ok": True, **extra}


def test_steps_keep_their_order_their_own_sources_and_how_long_they_took() -> None:
    clock = Clock()
    recorder = ActivityRecorder(clock=clock)
    clock.tick(1.5)
    recorder.note_terms(["2027 Winter"], "default")
    recorder.observe(_start("get_department_restrictions", "读取 WebSoc 部门说明 · I&C SCI · 2027年冬季"))
    clock.tick(2.0)
    recorder.observe(_done("get_department_restrictions", fetch_summary=[WEBSOC, PAGE]))
    recorder.observe(_start("get_department_restrictions", "读取 WebSoc 部门说明 · COMPSCI · 2027年冬季"))
    clock.tick(0.5)
    recorder.observe(_done("get_department_restrictions", fetch_summary=[WEBSOC, PAGE_CACHED]))
    clock.tick(3.0)
    recorder.observe({"type": "token", "text": "COMPSCI"})
    clock.tick(5.0)                                                   # the answer streaming is not thinking
    recorder.observe({"type": "token", "text": " opens"})

    activity = recorder.snapshot()

    assert activity["thinking_ms"] == 7000
    term, first, second = activity["steps"]
    assert term == {"kind": "term", "terms": ["2027 Winter"], "source": "default", "ok": True}
    assert (first["label"], first["ok"], first["duration_ms"]) == (
        "读取 WebSoc 部门说明 · I&C SCI · 2027年冬季", True, 2000)
    assert [source["url"] for source in first["sources"]] == [WEBSOC["url"], PAGE["url"]]
    assert second["sources"][1]["cached"] is True and second["duration_ms"] == 500
    assert "args" not in first and "secret" not in str(activity)


def test_looking_something_up_after_text_began_counts_as_thinking_again() -> None:
    clock = Clock()
    recorder = ActivityRecorder(clock=clock)
    clock.tick(2)
    recorder.observe({"type": "token", "text": "Let me check."})       # narration before a tool
    clock.tick(1)
    recorder.observe(_start("get_sections", "查询 COMPSCI 161 排课"))
    clock.tick(4)
    recorder.observe(_done("get_sections", section_count=3, offering_status="offered", authoritative=True))
    clock.tick(1)
    recorder.observe({"type": "token", "text": "There are three sections."})

    activity = recorder.snapshot()

    assert activity["thinking_ms"] == 8000
    assert activity["steps"][0]["section_count"] == 3 and activity["steps"][0]["authoritative"] is True


def test_failed_and_unfinished_steps_are_told_apart() -> None:
    recorder = ActivityRecorder(clock=Clock())
    recorder.observe(_start("web_search", "联网搜索 · uci"))
    recorder.observe({"type": "tool_call_done", "name": "web_search", "ok": False})
    recorder.observe(_start("fetch_page", "深读网页"))                 # stream stopped before it finished

    failed, unfinished = recorder.snapshot()["steps"]

    assert failed["ok"] is False
    assert "ok" not in unfinished and "duration_ms" not in unfinished


def test_unrelated_events_and_a_runaway_turn_are_bounded() -> None:
    recorder = ActivityRecorder(clock=Clock())
    recorder.observe({"type": "meta", "cards": []})
    recorder.observe("not an event")
    recorder.observe({"type": "tool_call_done", "name": "never_started"})
    for index in range(60):
        recorder.observe(_start("get_sections", f"查询 {index}"))
        recorder.observe(_done("get_sections", fetch_summary=[PAGE] * 30))

    steps = recorder.snapshot()["steps"]

    assert len(steps) == 40 and len(steps[0]["sources"]) == 12


def test_observed_queue_records_and_passes_everything_on() -> None:
    async def run() -> tuple[list, dict]:
        queue: asyncio.Queue = asyncio.Queue()
        recorder = ActivityRecorder(clock=Clock())
        observed = ObservedQueue(queue, recorder)
        for event in (_start("get_policy", "查询学校政策 · add drop"), _done("get_policy"), {"type": "token", "text": "ok"}):
            await observed.put(event)
        assert observed.qsize() == 3                                    # other queue methods still work
        return [queue.get_nowait() for _ in range(3)], recorder.snapshot()

    events, activity = asyncio.run(run())

    assert [event["type"] for event in events] == ["tool_call_start", "tool_call_done", "token"]
    assert [step["name"] for step in activity["steps"]] == ["get_policy"]


def test_sources_of_every_call_are_kept_and_a_reused_page_still_counts_as_requested() -> None:
    # 2026-11-12: the audit showed only the last tool call, "0 requests, 2 reused",
    # although the first call of the turn had really fetched the page.
    merged = merge_sources([WEBSOC, PAGE], [WEBSOC, PAGE_CACHED])

    assert [source["url"] for source in merged] == [WEBSOC["url"], PAGE["url"]]
    assert "cached" not in merged[1] and merged[1]["status_code"] == 200
    assert count_requests(merged) == 1
    # Order of arrival does not matter: the request wins over the reuse.
    assert "cached" not in merge_sources([PAGE_CACHED], [PAGE])[0]
    assert merge_sources(None, None) == [] and count_requests([WEBSOC, PAGE_CACHED]) == 0


def test_activity_is_stored_with_the_turn_and_read_back() -> None:
    user_id = "demo_001"
    session_id = sessions_data.create_session(user_id)
    activity = {"thinking_ms": 14000, "steps": [{"kind": "term", "terms": ["2027 Winter"], "ok": True}]}

    sessions_data.append_turn(user_id, session_id, "assistant", "answer", activity=activity)
    sessions_data.append_turn(user_id, session_id, "assistant", "no steps", activity={"thinking_ms": 0, "steps": []})

    stored, plain = sessions_data.read_turns(user_id, session_id)[-2:]
    assert stored["activity"] == activity
    assert "activity" not in plain


def test_chat_stream_sends_and_stores_the_whole_turns_activity(monkeypatch) -> None:
    import json

    from fastapi import BackgroundTasks

    from app.llm import adapter
    from app.routers import chat as chat_router
    from app.routers.chat import ChatRequest

    async def fake_agent(*_args, **_kwargs):
        for department, sources in (("I&C SCI", [WEBSOC, PAGE]), ("COMPSCI", [WEBSOC, PAGE_CACHED])):
            label = f"读取 WebSoc 部门说明 · {department} · 2027年冬季"
            yield {"type": "tool_call_start", "name": "get_department_restrictions",
                   "args": {"department": department}, "label": label, "response_language": "zh"}
            yield {"type": "tool_call_done", "name": "get_department_restrictions", "ok": True,
                   "args": {"department": department}, "label": label, "fetch_summary": sources}
        yield {"type": "token", "text": "12 月 18 日中午开放。"}
        yield {"type": "final", "text": "12 月 18 日中午开放。", "iterations": 2, "tool_calls": 2}

    monkeypatch.setattr(adapter, "stream_agent_response", fake_agent)

    async def collect() -> list[dict]:
        request = ChatRequest(message="ICS 和计算机系的课什么时候对外专业开放", session_id="")
        return [json.loads(raw.removeprefix("data: ").strip())
                async for raw in chat_router._stream_chat(request, BackgroundTasks(), user_id="demo_001")]

    events = asyncio.run(collect())

    kinds = [event["type"] for event in events]
    assert kinds[0] == "stage" and kinds.index("stage") < kinds.index("tool_call_start")
    stage = events[0]
    assert stage["stage"] == "term_resolved" and stage["query_terms"] and stage["response_language"] == "zh"
    meta = next(event for event in events if event["type"] == "meta")
    activity = meta["activity"]
    assert [step["kind"] for step in activity["steps"]] == ["term", "tool", "tool"]
    assert activity["steps"][0]["terms"] == stage["query_terms"]
    assert [step["label"].split(" · ")[1] for step in activity["steps"][1:]] == ["I&C SCI", "COMPSCI"]
    assert [len(step["sources"]) for step in activity["steps"][1:]] == [2, 2]      # each step keeps its own
    assert activity["thinking_ms"] >= 0
    # The turn-level list covers both calls; the page was requested once.
    assert [source["url"] for source in meta["web_fetches"]] == [WEBSOC["url"], PAGE["url"]]
    assert count_requests(meta["web_fetches"]) == 1
    stored = sessions_data.read_turns("demo_001", meta["session_id"])[-1]
    assert stored["role"] == "assistant" and stored["activity"] == activity
    assert stored["web_fetches"] == meta["web_fetches"]
