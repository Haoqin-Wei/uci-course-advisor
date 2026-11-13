"""Real-model acceptance run using the current Solon agent adapter.

Only synthetic context is supplied. User data/history is isolated. Provider
credentials stay inside the configured SDK; saved transcripts omit reasoning.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / os.environ.get("SOLON_ACCEPTANCE_RUN", "run-01")
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
ISOLATED = Path(tempfile.mkdtemp(prefix="solon-live-comment-"))
os.environ["MEMORY_ROOT"] = str(ISOLATED / "memory")
os.environ["MEMORY_DB_PATH"] = str(ISOLATED / "memory.sqlite3")
os.environ["ACADEMIC_DB_PATH"] = str(ISOLATED / "academic.sqlite3")
os.environ["TERM_STATE_PATH"] = str(ISOLATED / "term-state.json")

from app import config
from app.llm import adapter
from app.data import anteater, db, deep_search_history, sessions
from app.terms.service import get_term_resolution_service
from app.catalog.normalization import iter_course_mentions
import requests

logging.basicConfig(level=logging.WARNING)

QUESTION = os.environ.get("SOLON_TEST_QUERY", "我能上in4matx43吗？")

calls, http_calls, events = [], [], []


def safe(value):
    if isinstance(value, dict):
        return {key: ("[omitted]" if key == "reasoning_content" else
                      "[redacted]" if any(part in key.lower() for part in ("api_key", "apikey", "authorization", "password", "secret"))
                      else safe(child)) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(child) for child in value]
    if isinstance(value, str) and adapter.DEEPSEEK_API_KEY:
        return value.replace(adapter.DEEPSEEK_API_KEY, "[redacted]")
    return value


def dump(name, value):
    (OUT / name).write_text(json.dumps(safe(value), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


class RecordingCompletions:
    def __init__(self, actual):
        self.actual = actual

    async def create(self, **kwargs):
        if len(calls) >= 16:
            raise RuntimeError("Acceptance-run limit of 16 provider requests reached")
        index = len(calls) + 1
        row = {"index": index, "started_at_utc": datetime.now(timezone.utc).isoformat(),
               "model": kwargs.get("model"), "stream": kwargs.get("stream"),
               "tool_message_count": sum(message.get("role") == "tool" for message in kwargs.get("messages", []))}
        calls.append(row)
        dump(f"model-call-{index:02d}-request.json", kwargs)
        print(json.dumps({"provider_request_started": index, "model": kwargs.get("model"),
                          "tool_messages": row["tool_message_count"]}), flush=True)
        start = time.monotonic()
        try:
            result = await self.actual.create(**kwargs)
        except Exception as exc:
            row.update(error_type=type(exc).__name__, error=str(exc), elapsed_seconds=time.monotonic() - start)
            dump("provider-calls.json", calls)
            raise
        if not kwargs.get("stream"):
            row["elapsed_seconds"] = time.monotonic() - start
            dump(f"model-call-{index:02d}-response.json", result.model_dump(exclude={"choices": {"__all__": {"message": {"reasoning_content"}}}}))
            return result

        async def recorded_stream():
            chunks = []
            try:
                async for chunk in result:
                    chunks.append(safe(chunk.model_dump()))
                    yield chunk
            finally:
                row["elapsed_seconds"] = time.monotonic() - start
                dump(f"model-call-{index:02d}-stream.json", chunks)
                dump("provider-calls.json", calls)
                await result.close()
        return recorded_stream()


async def main():
    if not adapter.LLM_ENABLED:
        raise RuntimeError("Configured real model credentials unavailable")
    from app.memory import get_memory_manager
    from app.memory.sqlite_provider import SQLiteMemoryProvider
    from app.academic.models import TranscriptImportRequest, TranscriptCourseInput
    from app.academic.store import import_transcript
    manager = get_memory_manager()
    manager.set_provider(SQLiteMemoryProvider(ISOLATED / "memory.sqlite3", legacy_base_dir=ISOLATED / "memory"))
    manager.update_profile("synthetic-course-comment-acceptance", {"completed_courses":["ICS32"]}, strict=True)
    import_result = import_transcript("synthetic-course-comment-acceptance", TranscriptImportRequest(
        parser_version="synthetic-test", courses=[TranscriptCourseInput(course_id="I&C SCI 32", department="I&C SCI", course_number="32", grade="B+", units=4)]))
    assert import_result["accepted"] == 1, import_result
    synthetic_profile = db.get_student_profile("synthetic-course-comment-acceptance")["profile"]
    assert synthetic_profile["completed_course_attempts"][0]["grade"] == "B+"
    real = adapter._get_client()
    recording = SimpleNamespace(chat=SimpleNamespace(completions=RecordingCompletions(real.chat.completions)))
    actual_http = requests.sessions.Session.request

    def record_http(session, method, url, **kwargs):
        row = {"method": method.upper(), "url": url, "params": safe(kwargs.get("params")),
               "form": safe(kwargs.get("data")), "started_at_utc": datetime.now(timezone.utc).isoformat()}
        http_calls.append(row)
        index = len(http_calls)
        try:
            response = actual_http(session, method, url, **kwargs)
            suffix = ".json" if "json" in response.headers.get("content-type", "").lower() else ".html"
            filename = f"source-http-{index:02d}" + suffix
            (OUT / filename).write_bytes(response.content)
            row.update(status=response.status_code, bytes=len(response.content), response_file=filename,
                       response_sha256=hashlib.sha256(response.content).hexdigest())
            return response
        except Exception as exc:
            row.update(error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            dump("source-http-requests.json", http_calls)

    # The query explicitly requests both terms. Computing the normal automatic
    # context must not replace or mutate those query targets.
    with patch.object(requests.sessions.Session, "request", record_http):
        automatic = get_term_resolution_service().automatic_state()
    state = {**automatic, "default_term": automatic.get("default_term") or automatic.get("automatic_term"),
             "query_terms": ["2026 Fall"], "query_term_source": "default",
             "query_intent": "lookup", "query_course_ids": [ref.display() for ref, _, _ in iter_course_mentions(QUESTION)],
             "pending_schedule": [], "completed_courses": [], "selected_courses": []}
    previous_turns = []
    if os.environ.get("SOLON_PREVIOUS_ANSWER"):
        previous_turns = [{"role":"user","content":"我能上in4matx43吗？"},
                          {"role":"assistant","content":Path(os.environ["SOLON_PREVIOUS_ANSWER"]).read_text()}]
    default_before = state.get("default_term")
    metadata = {"question": QUESTION, "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "model": adapter.LLM_MODEL, "provider_host": urlsplit(adapter.DEEPSEEK_BASE_URL).hostname,
                "dynamic_prefilter_enabled": config.dynamic_prefilter_enabled(),
                "recommendation_ranking_enabled": config.recommendation_ranking_enabled(),
                "state": state, "synthetic_context": True, "max_wall_seconds": 180,
                "max_provider_requests": 16, "prompt_modified_for_test": False,
                "application_prompt_sha256": hashlib.sha256(adapter.AGENT_SYSTEM_PROMPT.encode()).hexdigest(),
                "source_results_mocked": False}
    dump("run-metadata.json", metadata)
    anteater._live_sections_cache.clear()
    anteater._sections_cache.clear()
    db._pairing_evidence_cache.clear()
    outcome = "incomplete"
    start = time.monotonic()
    try:
        with patch.object(adapter, "_get_client", lambda: recording), \
             patch.object(requests.sessions.Session, "request", record_http), \
             patch.object(deep_search_history, "DB_PATH", ISOLATED / "history.sqlite3"), \
             patch.object(sessions, "MEMORY_ROOT", ISOLATED / "sessions"):
            async with asyncio.timeout(180):
                async for event in adapter.stream_agent_response(
                    QUESTION, state, user_id="synthetic-course-comment-acceptance", term="2026 Fall",
                    profile=synthetic_profile,
                    recent_turns=previous_turns, decisions=[], preferences=[], facts=[],
                ):
                    events.append(safe(event))
                    dump("events.json", events)
                    if event.get("type") in {"tool_call_start", "tool_call_done", "error", "limit_reached"}:
                        print(json.dumps(safe({key: event.get(key) for key in ("type", "name", "args", "ok", "message")}),
                                         ensure_ascii=False), flush=True)
                    if event.get("type") == "final":
                        outcome = "completed"
                        (OUT / "answer.txt").write_text(event.get("text") or "", encoding="utf-8")
                        dump("final-event.json", event)
                        print(json.dumps({"final_answer": event.get("text"), "iterations": event.get("iterations"),
                                          "tool_calls": event.get("tool_calls")}, ensure_ascii=False), flush=True)
    except Exception as exc:
        outcome = "error"
        dump("run-error.json", {"type": type(exc).__name__, "message": str(exc)})
        print(json.dumps(safe({"run_error": type(exc).__name__, "message": str(exc)}), ensure_ascii=False), flush=True)
    finally:
        metadata.update(outcome=outcome, elapsed_seconds=time.monotonic() - start, provider_requests=len(calls),
                        source_http_requests=len(http_calls), default_term_unchanged=state.get("default_term") == default_before)
        dump("run-metadata.json", metadata)
        dump("provider-calls.json", calls)
        dump("events.json", events)
        await real.close()
    print(json.dumps({"outcome": outcome, "provider_requests": len(calls), "elapsed_seconds": metadata["elapsed_seconds"],
                      "artifact_dir": str(OUT)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
