"""Lightweight request tracing, structured logs, and in-process metrics.

This module intentionally avoids external services. Private beta needs a
consistent trace ID and enough counters/timings to debug failures from
local logs first; production exporters can be wired later behind this
small API.
"""

from __future__ import annotations

import contextvars
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
_URL_FIELD_NAMES = {"url", "web_search_url", "source_url", "final_url"}


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
            }
            for key, values in _timings.items()
        },
    }


def clear_metrics() -> None:
    _counters.clear()
    _timings.clear()


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    safe_fields = {
        key: _safe_log_value(key, value)
        for key, value in fields.items()
        if value is not None
    }
    payload = " ".join(f"{key}={value!r}" for key, value in sorted(safe_fields.items()))
    logger.log(level, "event=%s trace_id=%s %s", event, get_trace_id(), payload)


def estimate_tokens(text: str | None) -> int:
    """Conservative local estimate when provider usage is unavailable."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


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
    return _safe_value(value)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default
