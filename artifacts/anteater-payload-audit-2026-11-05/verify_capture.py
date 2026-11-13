"""Replay freshly fetched public responses through Solon's actual agent loop.

The model client only captures outgoing requests; no provider model is called.
The original Anteater bytes are fetched separately with curl and never changed.
With --online-pairing, existing enabled pairing logic may read public UCI pages.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUT = Path(__file__).resolve().parent

from app import config
from app.llm import adapter
from app.agent import loop
from app.data import anteater, db, deep_search_history
from app.recommendations.pairing import contract_enabled
from tests.fakes.llm import ScriptedLLMClient, tool_response, tool_call, text_response
import requests

logging.basicConfig(level=logging.ERROR)

CASES = [
    ("2026-fall-swe43", "SWE43", "SWE", "Fall"),
    ("2026-winter-in4matx43", "IN4MATX43", "IN4MATX", "Winter"),
]


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def json_children(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            pointer = path + "/" + key.replace("~", "~0").replace("/", "~1")
            yield key, child, pointer
            yield from json_children(child, pointer)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from json_children(child, path + "/" + str(index))


def captured_time(stem):
    headers = (OUT / f"{stem}-http-headers.txt").read_text()
    date_line = next(line[5:].strip() for line in headers.splitlines() if line.lower().startswith("date:"))
    return parsedate_to_datetime(date_line).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def run_case(stem, code, department, quarter, online_pairing):
    raw_path = OUT / f"{stem}-raw.json"
    raw_bytes = raw_path.read_bytes()
    raw = json.loads(raw_bytes)
    assert raw.get("ok") is True
    before = deepcopy(raw)
    fetched_at = captured_time(stem)
    params = {"year": "2026", "quarter": quarter, "department": department, "courseNumber": "43"}
    endpoint = anteater.WEBSOC_URL + "?" + urlencode(params)
    term = f"2026 {quarter}"
    transport_calls = []
    official_calls = []
    actual_request = requests.sessions.Session.request

    def replay_anteater(url, **kwargs):
        assert url == anteater.WEBSOC_URL, url
        assert kwargs.get("params") == params, kwargs.get("params")
        transport_calls.append({"url": url, "params": deepcopy(kwargs["params"]),
                                "mode": "fresh_http_response_replay", "raw_sha256": hashlib.sha256(raw_bytes).hexdigest()})
        return deepcopy(raw)

    def record_official(session, method, url, **kwargs):
        host = urlsplit(url).hostname
        assert host in {"reg.uci.edu", "www.reg.uci.edu", "websoc.reg.uci.edu"}, host
        assert method.upper() in {"GET", "POST"}, method
        if not online_pairing:
            raise requests.ConnectionError("Official pairing network disabled for this capture")
        entry = {"method": method.upper(), "url": url, "form": deepcopy(kwargs.get("data")),
                 "started_at": datetime.now(timezone.utc).isoformat()}
        official_calls.append(entry)
        try:
            response = actual_request(session, method, url, **kwargs)
            entry.update(status=response.status_code, bytes=len(response.content))
            target = OUT / f"{stem}-official-{len(official_calls)}.html"
            target.write_bytes(response.content)
            entry["response_file"] = target.name
            return response
        except requests.RequestException as exc:
            entry["error"] = type(exc).__name__ + ": " + str(exc)
            raise

    client = ScriptedLLMClient(
        tool_response(tool_call("get_live_sections", {"course_id": code, "term": term,
                                                    "force_refresh": True}, call_id="audit_" + stem)),
        text_response("Tool payload capture complete."),
    )
    messages = [{"role": "system", "content": adapter.AGENT_SYSTEM_PROMPT},
                {"role": "user", "content": f"Display the API records for {code} in {term}."}]
    anteater._live_sections_cache.clear()
    db._pairing_evidence_cache.clear()
    with patch.object(anteater, "_get_json", replay_anteater), \
         patch.object(anteater, "_utc_now", lambda: fetched_at), \
         patch.object(requests.sessions.Session, "request", record_official), \
         patch.object(deep_search_history, "DB_PATH", OUT / "capture-only-history.sqlite3"):
        events = [event async for event in loop.run_agent(
            messages, client=client, model="capture-only-no-provider-request", user_id="payload-audit",
            term=term, allowed_query_terms=[term], query_term_source="explicit", response_language="en",
        )]
    client.assert_exhausted()
    assert len(client.calls) == 2
    tool_messages = [item for item in client.calls[-1].messages if item.get("role") == "tool"]
    assert len(tool_messages) == 1, [item.get("tool_call_id") for item in tool_messages]
    tool_message = tool_messages[0]
    result = json.loads(tool_message["content"])
    assert raw == before and raw_path.read_bytes() == raw_bytes
    assert len(transport_calls) == 1
    assert result.get("source") == "live_anteater_websoc" and result.get("is_live") is True
    assert db._to_term(result["term"]) == db._to_term(term)

    comment_checks, meeting_checks, cancellation_checks = [], [], []
    mapped_comment_paths = set()
    sections = {item["section_code"]: (index, item) for index, item in enumerate(result["sections"])}
    source_sections = []
    contexts = result["comment_contexts"]

    def check_comment(raw_pointer, raw_value, context_id, fields):
        value = contexts[context_id]
        for field in fields:
            value = value[field]
        comment_checks.append({"raw_path": raw_pointer,
            "result_path": "/comment_contexts/" + context_id + "/" + "/".join(fields),
            "equal": value == raw_value, "empty": raw_value in (None, "", []),
            "raw_value_sha256": fingerprint(raw_value), "result_value_sha256": fingerprint(value)})
        mapped_comment_paths.add(raw_pointer)

    for school_index, school in enumerate(raw["data"]["schools"]):
        for dept_index, dept in enumerate(school.get("departments") or []):
            for course_index, course in enumerate(dept.get("courses") or []):
                course_pointer = f"/data/schools/{school_index}/departments/{dept_index}/courses/{course_index}"
                notice = next(item for item in result["course_notices"] if item["course_id"] == course["courseId"])
                context_id = notice["comment_context_id"]
                parent_mappings = [
                    (f"/data/schools/{school_index}/schoolComment", school.get("schoolComment"), ["school", "comment"]),
                    (f"/data/schools/{school_index}/departments/{dept_index}/deptComment", dept.get("deptComment"), ["department", "comment"]),
                    (f"/data/schools/{school_index}/departments/{dept_index}/courseNumberRangeComments", dept.get("courseNumberRangeComments"), ["department", "course_number_range_comments"]),
                    (f"/data/schools/{school_index}/departments/{dept_index}/sectionCodeRangeComments", dept.get("sectionCodeRangeComments"), ["department", "section_code_range_comments"]),
                    (course_pointer + "/courseComment", course.get("courseComment"), ["course", "comment"]),
                ]
                for pointer, value, fields in parent_mappings:
                    if pointer not in mapped_comment_paths:
                        check_comment(pointer, value, context_id, fields)
                for source_index, source in enumerate(course["sections"]):
                    index, converted = sections[source["sectionCode"]]
                    source_pointer = course_pointer + f"/sections/{source_index}"
                    source_sections.append(source)
                    raw_comment = source.get("sectionComment")
                    value = converted["section_comment"]
                    pointer = source_pointer + "/sectionComment"
                    mapped_comment_paths.add(pointer)
                    comment_checks.append({"raw_path": pointer, "result_path": f"/sections/{index}/section_comment",
                        "equal": value == raw_comment, "empty": raw_comment in (None, "", []),
                        "raw_value_sha256": fingerprint(raw_comment), "result_value_sha256": fingerprint(value)})
                    meeting_checks.append({"section_code": source["sectionCode"],
                        "raw_path": source_pointer + "/meetings", "result_path": f"/sections/{index}/meetings",
                        "equal": source.get("meetings") == converted["meetings"],
                        "meeting_count": len(source.get("meetings") or []),
                        "raw_value_sha256": fingerprint(source.get("meetings")),
                        "result_value_sha256": fingerprint(converted["meetings"])})
                    cancellation_checks.append({"section_code": source["sectionCode"],
                        "raw": source.get("isCancelled"), "result": converted["is_cancelled"],
                        "equal": source.get("isCancelled") is converted["is_cancelled"]})
                    assert converted["comment_context_id"] == context_id

    raw_comment_paths = {pointer for key, value, pointer in json_children(raw) if "comment" in key.lower()}
    unmatched = sorted(raw_comment_paths - mapped_comment_paths)
    unneeded_mappings = sorted(mapped_comment_paths - raw_comment_paths)
    source_codes = {item["sectionCode"] for item in source_sections}
    errors = [item for item in comment_checks + meeting_checks + cancellation_checks if not item["equal"]]
    assert not unmatched and not unneeded_mappings and not errors, (unmatched, unneeded_mappings, errors)
    assert source_codes == set(sections)
    assert not any(key.lower().replace("_", "") == "weburl" for key, value, pointer in json_children(result))
    summary = {
        "course_id": code, "term": term, "endpoint": endpoint, "http_status": 200,
        "source_fetched_at_utc": fetched_at, "processed_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "source_sections": len(source_sections), "result_sections": len(sections),
        "source_meetings": sum(item["meeting_count"] for item in meeting_checks),
        "result_meetings": sum(len(item.get("meetings") or []) for index, item in sections.values()),
        "comment_fields_checked": len(comment_checks), "comments_equal": all(item["equal"] for item in comment_checks),
        "all_meetings_deep_equal": all(item["equal"] for item in meeting_checks),
        "cancelled_sections": [item["section_code"] for item in cancellation_checks if item["raw"] is True],
        "cancellation_equal": all(item["equal"] for item in cancellation_checks),
        "unmapped_comment_fields": unmatched, "extra_or_missing_sections": [],
        "comment_context_count": len(contexts), "weburl_added": False,
        "pairing_lookup_status": result.get("pairing_lookup_status"),
        "actual_provider_model_calls": 0, "capture_client_calls": len(client.calls),
        "runtime_flags": {"dynamic_prefilter_enabled": config.dynamic_prefilter_enabled(),
                          "recommendation_ranking_enabled": config.recommendation_ranking_enabled(),
                          "pairing_contract_enabled": contract_enabled()},
        "recommendation_prefilter_active_for_this_query": False,
        "capture_method": "fresh_public_Anteater_HTTP_response_replayed_through_actual_agent_tool_loop",
        "tool_message_matches_next_model_request": tool_message in client.calls[-1].messages,
    }
    dump(OUT / f"{stem}-raw.pretty.json", raw)
    dump(OUT / f"{stem}-ai-result.json", result)
    dump(OUT / f"{stem}-ai-tool-message.json", tool_message)
    dump(OUT / f"{stem}-verification.json", {"summary": summary, "comments": comment_checks,
         "meetings": meeting_checks, "cancellation": cancellation_checks,
         "anteater_transport": transport_calls, "supplementary_official_requests": official_calls,
         "agent_events": events})
    print(json.dumps(summary, ensure_ascii=False))
    return summary


async def main():
    args = argparse.ArgumentParser()
    args.add_argument("--online-pairing", action="store_true")
    options = args.parse_args()
    summaries = []
    for case in CASES:
        summaries.append(await run_case(*case, options.online_pairing))
    dump(OUT / "comparison.json", {"cases": summaries,
        "note": "Public API queried live. Fresh captured bytes replayed into actual Solon agent loop. Model transport intercepted; no real LLM provider request or generated answer."})
    with zipfile.ZipFile(OUT / "raw-vs-ai-result.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(OUT.iterdir()):
            if path.suffix in {".json", ".txt", ".html", ".py"}:
                archive.write(path, arcname=path.name)


if __name__ == "__main__":
    asyncio.run(main())
