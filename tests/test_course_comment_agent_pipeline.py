"""Exercise source-to-model evidence and factual routing; no model accuracy claim."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

from app.agent import loop
from app.data import anteater, db
from app.llm import adapter
from tests.fakes.llm import ScriptedLLMClient, text_response, tool_call, tool_response
from tests.test_anteater_payload_preservation import _swe43_response


QUESTION = (
    "请核实 SWE43 和 IN4MATX43 是什么关系，并说明依据。"
    "假设我在 2026 Winter 修过 IN4MATX43，而且已经获得这门课的学分，"
    "还能在 2026 Fall 再修 SWE43 吗？请根据课程说明回答。"
)


def test_factual_question_delivers_comment_and_alias_without_recommendation_gate(monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "true")
    monkeypatch.setattr(adapter, "LLM_ENABLED", True)
    monkeypatch.setattr(loop, "load_history_hint", lambda *_: ([], None))
    monkeypatch.setattr(loop, "record_run_trace", lambda *_: {})
    raw = _swe43_response()
    original = deepcopy(raw)
    requested = []

    def api(url, **kwargs):
        requested.append(deepcopy(kwargs.get("params")))
        return deepcopy(raw)

    monkeypatch.setattr(anteater, "_get_json", api)
    monkeypatch.setattr(db, "enrich_sections_pairing", lambda cid, term, result, **kw: result)
    metadata_lookups = []

    def metadata(cid):
        metadata_lookups.append(cid)
        return {"found": True, "source": "fixture", "course": {"course_id": "IN4MATX 43"}}

    monkeypatch.setattr(db, "get_course_info", metadata)

    def forbidden_lookup(*args, **kwargs):
        raise AssertionError("The tool term guard must reject the unrelated historical term")

    monkeypatch.setattr(db, "get_sections", forbidden_lookup)
    client = ScriptedLLMClient(
        tool_response(
            tool_call("get_course_info", {"course_id": "IN4MATX43"}, call_id="old-metadata"),
            tool_call("get_live_sections", {"course_id": "SWE43", "term": "2026 Fall"},
                      call_id="new-comment", index=1),
            tool_call("get_sections", {"course_id": "IN4MATX43", "term": "2025 Winter"},
                      call_id="outside-scope", index=2),
        ),
        text_response("Offline transport check only."),
    )
    monkeypatch.setattr(adapter, "_get_client", lambda: client)
    state = {
        "current_term": "2026 Fall", "default_term": "2027 Winter",
        "query_terms": ["2026 Winter", "2026 Fall"], "query_term_source": "explicit",
        "query_intent": "lookup", "query_course_ids": ["SWE43", "IN4MATX43"],
    }
    before = deepcopy(state)

    async def collect():
        return [event async for event in adapter.stream_agent_response(
            QUESTION, state, user_id="course-identity-fixture", term="2026 Fall", recent_turns=[],
        )]

    events = asyncio.run(collect())
    client.assert_exhausted()
    assert state == before
    assert metadata_lookups == ["IN4MATX43"]
    assert requested == [{"year": "2026", "quarter": "Fall", "department": "SWE", "courseNumber": "43"}]
    assert not any(event.get("controlled_recommendation") for event in events)
    assert not any(event["type"] == "cards_proposed" for event in events)
    messages = {row["tool_call_id"]: json.loads(row["content"])
                for row in client.calls[-1].messages if row["role"] == "tool"}
    assert messages["old-metadata"]["found"] is True
    assert "invalid term" in messages["outside-scope"]["error"]
    payload = messages["new-comment"]
    source_course = raw["data"]["schools"][0]["departments"][0]["courses"][0]
    context = next(iter(payload["comment_contexts"].values()))
    assert context["course"]["comment"] == source_course["courseComment"]
    assert any(row["source_text"] == "INF 43" and row["normalized_course_id"] == "IN4MATX 43"
               and row["basis"] == "existing_department_alias_rules"
               for row in payload["course_code_normalizations"])
    assert [row["meetings"] for row in payload["sections"]] == [
        row["meetings"] for row in source_course["sections"]
    ]
    assert raw == original
    assert events[-1]["text"] == "Offline transport check only."
