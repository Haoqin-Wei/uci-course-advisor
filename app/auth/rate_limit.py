"""Small in-process fixed-window rate limiter.

This is intentionally simple: it protects the single-process private beta
deployment from accidental abuse without adding Redis/Postgres dependencies.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque

from fastapi import HTTPException, Request, status


@dataclass(frozen=True)
class RateLimit:
    name: str
    limit: int
    window_seconds: int


_BUCKETS: dict[str, Deque[float]] = defaultdict(deque)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def check_rate_limit(request: Request, policy: RateLimit, subject: str = "") -> None:
    now = time.monotonic()
    key = f"{policy.name}:{client_ip(request)}:{subject.lower().strip()}"
    bucket = _BUCKETS[key]
    cutoff = now - policy.window_seconds
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= policy.limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests; slow down and try again later.",
        )
    bucket.append(now)


def clear_rate_limits() -> None:
    _BUCKETS.clear()
