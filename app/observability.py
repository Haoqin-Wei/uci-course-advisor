"""Lightweight request tracing, structured logs, and in-process metrics.

This module intentionally avoids external services. Private beta needs a
consistent trace ID and enough counters/timings to debug failures from
local logs first; production exporters can be wired later behind this
small API.
"""

from __future__ import annotations

import contextvars
import json
import logging
import math
import os
import time
import uuid
from collections import defaultdict
from typing import Any

_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id",
    default="-",
)

_counters: dict[str, float] = defaultdict(float)
_timings: dict[str, list[float]] = defaultdict(list)

_LOG_VALUE_MAX_CHARS = 160
_LOG_URL_MAX_CHARS = 2048
_LOG_COLLECTION_MAX_ITEMS = 10
_URL_FIELD_NAMES = {
    "url",
    "web_search_url",
    "source_url",
    "final_url",
    "fetched_urls",
    "candidate_urls",
}


def new_trace_id() -> str:
    return uuid.uuid4().hex


def get_trace_id() -> str:
    return _trace_id.get()


def set_trace_id(value: str):
    return _trace_id.set(value or new_trace_id())


def reset_trace_id(token) -> None:
    _trace_id.reset(token)


def now() -> float:
    return time.perf_counter()


def elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 2)


def increment(name: str, amount: float = 1, **labels: Any) -> None:
    key = _metric_key(name, labels)
    _counters[key] += amount


def observe_ms(name: str, value: float, **labels: Any) -> None:
    key = _metric_key(name, labels)
    _timings[key].append(float(value))


def snapshot_metrics() -> dict:
    return {
        "counters": dict(_counters),
        "timings": {
            key: {
                "count": len(values),
                "min_ms": min(values) if values else None,
                "max_ms": max(values) if values else None,
                "avg_ms": round(sum(values) / len(values), 2) if values else None,
                "p50_ms": _percentile(values, 0.50),
                "p95_ms": _percentile(values, 0.95),
                "p99_ms": _percentile(values, 0.99),
            }
            for key, values in _timings.items()
        },
    }


def clear_metrics() -> None:
    _counters.clear()
    _timings.clear()


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[index], 2)


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    safe_fields = {
        key: _safe_log_value(key, value)
        for key, value in fields.items()
        if value is not None
    }
    payload = " ".join(f"{key}={value!r}" for key, value in sorted(safe_fields.items()))
    logger.log(level, "event=%s trace_id=%s %s", event, get_trace_id(), payload)


def log_agent_web_fetch_started(
    logger: logging.Logger,
    *,
    url: str,
    method: str,
    tool: str,
    workflow_id: str,
    trigger: str = "agent",
    **fields: Any,
) -> None:
    """Emit the auditable start of a real network request."""
    increment("agent.web_fetch", result="started", tool=tool)
    log_event(
        logger,
        logging.INFO,
        "agent_web_fetch_started",
        url=url,
        method=method,
        tool=tool,
        workflow_id=workflow_id,
        trigger=trigger,
        cache_hit=False,
        **fields,
    )


def log_agent_web_fetch_completed(
    logger: logging.Logger,
    *,
    url: str,
    final_url: str,
    method: str,
    tool: str,
    workflow_id: str,
    status_code: int,
    duration_ms: float,
    content_length: int | None = None,
    content_type: str | None = None,
    trigger: str = "agent",
    **fields: Any,
) -> None:
    """Emit proof that an Agent-triggered HTTP request received a response."""
    increment("agent.web_fetch", result="completed", tool=tool)
    observe_ms("agent.web_fetch_ms", duration_ms, tool=tool)
    log_event(
        logger,
        logging.INFO,
        "agent_web_fetch_completed",
        url=url,
        final_url=final_url,
        method=method,
        tool=tool,
        workflow_id=workflow_id,
        trigger=trigger,
        cache_hit=False,
        status_code=status_code,
        content_length=content_length,
        content_type=content_type,
        duration_ms=duration_ms,
        **fields,
    )


def log_agent_web_fetch_failed(
    logger: logging.Logger,
    *,
    url: str,
    method: str,
    tool: str,
    workflow_id: str,
    duration_ms: float,
    error: str,
    trigger: str = "agent",
    **fields: Any,
) -> None:
    """Emit a compact failed-request audit record."""
    increment("agent.web_fetch", result="failed", tool=tool)
    observe_ms("agent.web_fetch_ms", duration_ms, tool=tool)
    log_event(
        logger,
        logging.WARNING,
        "agent_web_fetch_failed",
        url=url,
        method=method,
        tool=tool,
        workflow_id=workflow_id,
        trigger=trigger,
        cache_hit=False,
        duration_ms=duration_ms,
        error=error,
        **fields,
    )


def estimate_tokens(text: str | None) -> int:
    """Conservative local estimate when provider usage is unavailable."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def estimate_prompt_layers(
    messages: list[dict],
    *,
    tool_schemas: list[dict] | None = None,
) -> dict[str, int]:
    """Conservatively estimate the actual layered input sent to a model."""
    layers = {
        "base_system": 0,
        "runtime_context": 0,
        "memory_summary": 0,
        "recent_turns": 0,
        "tool_schemas": 0,
        "tool_calls_results": 0,
        "current_user": 0,
    }
    last_user_index = max(
        (
            index
            for index, message in enumerate(messages)
            if message.get("role") == "user"
        ),
        default=-1,
    )
    for index, message in enumerate(messages):
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "system":
            for block in content.split("\n\n"):
                if block.startswith("<runtime_context") or block.startswith(
                    "<runtime_rules"
                ):
                    layers["runtime_context"] += estimate_tokens(block)
                elif block.startswith(
                    (
                        "# Persistent student profile",
                        "# Decisions made earlier",
                        "# Earlier conversation summary",
                    )
                ):
                    layers["memory_summary"] += estimate_tokens(block)
                else:
                    layers["base_system"] += estimate_tokens(block)
        elif role == "tool" or message.get("tool_calls"):
            layers["tool_calls_results"] += estimate_tokens(
                content
                + json.dumps(
                    message.get("tool_calls") or [],
                    ensure_ascii=False,
                    default=str,
                )
            )
        elif role == "user" and index == last_user_index:
            layers["current_user"] += estimate_tokens(content)
        else:
            layers["recent_turns"] += estimate_tokens(content)
    if tool_schemas:
        layers["tool_schemas"] = estimate_tokens(
            json.dumps(tool_schemas, ensure_ascii=False, default=str)
        )
    layers["total_input"] = sum(layers.values())
    return layers


def record_llm_usage_estimate(
    *,
    model: str,
    input_text: str,
    output_text: str,
    source: str = "estimated",
) -> dict:
    input_tokens = estimate_tokens(input_text)
    output_tokens = estimate_tokens(output_text)
    input_rate = _float_env("LLM_INPUT_USD_PER_1K", 0.0)
    output_rate = _float_env("LLM_OUTPUT_USD_PER_1K", 0.0)
    cost_usd = round(
        (input_tokens / 1000 * input_rate)
        + (output_tokens / 1000 * output_rate),
        8,
    )
    increment("llm.input_tokens", input_tokens, model=model, source=source)
    increment("llm.output_tokens", output_tokens, model=model, source=source)
    increment("llm.cost_usd", cost_usd, model=model, source=source)
    return {
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "source": source,
    }


def _metric_key(name: str, labels: dict[str, Any]) -> str:
    if not labels:
        return name
    label_text = ",".join(
        f"{key}={_safe_value(value)}"
        for key, value in sorted(labels.items())
        if value is not None
    )
    return f"{name}{{{label_text}}}"


def _safe_value(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _LOG_VALUE_MAX_CHARS:
        return value[: _LOG_VALUE_MAX_CHARS - 3] + "..."
    return value


def _safe_log_value(key: str, value: Any) -> Any:
    if key in _URL_FIELD_NAMES and isinstance(value, str):
        if len(value) > _LOG_URL_MAX_CHARS:
            return value[: _LOG_URL_MAX_CHARS - 3] + "..."
        return value
    if isinstance(value, dict):
        items = list(value.items())
        safe = {
            item_key: _safe_log_value(str(item_key), item_value)
            for item_key, item_value in items[:_LOG_COLLECTION_MAX_ITEMS]
        }
        if len(items) > _LOG_COLLECTION_MAX_ITEMS:
            safe["_truncated_items"] = len(items) - _LOG_COLLECTION_MAX_ITEMS
        return safe
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        safe_items = [
            _safe_log_value(key, item)
            for item in items[:_LOG_COLLECTION_MAX_ITEMS]
        ]
        if len(items) > _LOG_COLLECTION_MAX_ITEMS:
            safe_items.append(f"... ({len(items) - _LOG_COLLECTION_MAX_ITEMS} more)")
        return safe_items
    return _safe_value(value)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default
