"""What a turn did before it answered.

The chat UI shows one line while Solon works ("Reading the COMPSCI department
notes · 6s") and folds it to "Thought for 14s" once the answer starts. This
module keeps the facts behind that line: the ordered steps, each step's own
sources, and how long the thinking took. The same record is sent to the page
and stored with the turn, so a reopened conversation shows what the live one
showed.

Text for people is not produced here. Steps carry the tool's existing label in
the turn's language; the page turns it into "Reading ..." / "Read ...".
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

MAX_STEPS = 40
MAX_SOURCES_PER_STEP = 12
MAX_LABEL_CHARS = 200

# Facts about a tool result that the page words for the reader.
_RESULT_FIELDS = ("section_count", "offering_status", "authoritative")
_SOURCE_FIELDS = (
    "method", "url", "final_url", "host", "source_role", "status_code", "ok", "bytes",
    "duration_ms", "depth", "parent_url", "error", "local", "snapshot_synced_at",
    "cached", "cached_at", "provides_evidence",
)


def _is_request(source: dict) -> bool:
    """Whether reading this source sent a request during this turn."""
    return not source.get("local") and not source.get("cached")


def _source_key(source: dict) -> tuple:
    address = str(source.get("final_url") or source.get("url") or "").split("#", 1)[0].rstrip("/").lower()
    return address, source.get("source_role"), bool(source.get("local"))


def clean_sources(sources: Any) -> list[dict]:
    cleaned = []
    for source in sources if isinstance(sources, list) else []:
        if isinstance(source, dict):
            cleaned.append({key: source[key] for key in _SOURCE_FIELDS if key in source})
    return cleaned


def merge_sources(existing: Any, new: Any) -> list[dict]:
    """Every source a turn read, once each, in the order first seen.

    A page requested by one tool call and reused from cache by a later one is
    one source that was requested: the request is what gets reported.
    """
    merged: list[dict] = []
    index: dict[tuple, int] = {}
    for source in [*clean_sources(existing), *clean_sources(new)]:
        key = _source_key(source)
        if key not in index:
            index[key] = len(merged)
            merged.append(source)
        elif _is_request(source) and not _is_request(merged[index[key]]):
            merged[index[key]] = source
    return merged


def count_requests(sources: Any) -> int:
    return sum(1 for source in merge_sources([], sources) if _is_request(source))


class ActivityRecorder:
    """Collects one turn's steps from the events sent to the page."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started = clock()
        self._steps: list[dict] = []
        self._running: list[tuple[dict, float]] = []
        self._answer_started: Optional[float] = None

    def note_terms(self, query_terms: Any, source: Optional[str] = None) -> None:
        terms = [str(term) for term in (query_terms or []) if term][:3]
        if not terms:
            return
        step: dict[str, Any] = {"kind": "term", "terms": terms, "ok": True}
        if source:
            step["source"] = str(source)
        self._append(step)

    def observe(self, event: Any) -> None:
        """Take note of one outgoing event; anything unrelated is ignored."""
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "tool_call_start":
            self._tool_started(event)
        elif kind == "tool_call_done":
            self._tool_finished(event)
        elif kind == "token" and event.get("text") and self._answer_started is None:
            self._answer_started = self._clock()

    def snapshot(self) -> dict:
        """The record as it stands; safe to send and to store."""
        ended = self._answer_started if self._answer_started is not None else self._clock()
        return {
            "thinking_ms": max(0, round((ended - self._started) * 1000)),
            "steps": [dict(step) for step in self._steps],
        }

    def _append(self, step: dict) -> bool:
        if len(self._steps) >= MAX_STEPS:
            return False
        self._steps.append(step)
        return True

    def _tool_started(self, event: dict) -> None:
        # Looking something up again after text has begun is thinking again.
        self._answer_started = None
        step = {
            "kind": "tool",
            "name": str(event.get("name") or ""),
            "label": str(event.get("label") or event.get("name") or "")[:MAX_LABEL_CHARS],
        }
        if self._append(step):
            self._running.append((step, self._clock()))

    def _tool_finished(self, event: dict) -> None:
        name = str(event.get("name") or "")
        position = next((i for i, (step, _) in enumerate(self._running) if step["name"] == name), None)
        if position is None:
            if not self._running:
                return
            position = 0
        step, started = self._running.pop(position)
        step["ok"] = event.get("ok", True) is not False
        step["duration_ms"] = max(0, round((self._clock() - started) * 1000))
        sources = clean_sources(event.get("fetch_summary"))[:MAX_SOURCES_PER_STEP]
        if sources:
            step["sources"] = sources
        for field in _RESULT_FIELDS:
            if field in event:
                step[field] = event[field]


class ObservedQueue:
    """Passes events on to a queue after showing them to a recorder."""

    def __init__(self, queue: Any, recorder: ActivityRecorder) -> None:
        self._queue = queue
        self._recorder = recorder

    async def put(self, event: Any) -> None:
        self._recorder.observe(event)
        await self._queue.put(event)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._queue, name)
