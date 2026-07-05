"""Controlled web-search interface for the agent.

The first implementation is intentionally conservative:

- tests and local development use a fake provider;
- real external providers are opt-in and currently return a structured
  unavailable error until a concrete provider adapter is added;
- results are normalized to small search-result records, never full page
  content, so web evidence cannot pollute local DB-verified data.
"""

from __future__ import annotations

import logging
import time
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import urlsplit
from typing import Any, Iterable, Optional

from app import config, observability

logger = logging.getLogger(__name__)


DEFAULT_MAX_RESULTS = 5
HARD_MAX_RESULTS = 10
SNIPPET_MAX_CHARS = 500
TITLE_MAX_CHARS = 180
LOG_VALUE_MAX_CHARS = 160

SOURCE_CLASSES = {
    "local_db_verified",
    "official_uci",
    "official_university",
    "government",
    "professor_page",
    "rmp",
    "reddit",
    "commercial",
    "news",
    "unknown",
}

SOURCE_TRUST_LEVELS = {
    "local_db_verified": "high",
    "official_uci": "high",
    "official_university": "medium_high",
    "government": "medium_high",
    "professor_page": "medium",
    "rmp": "medium",
    "news": "medium_low",
    "commercial": "medium_low",
    "reddit": "low",
    "unknown": "low",
}

# Prompt-facing trust order. `external_web`, `forum/social`, and
# `llm_inference` are reasoning buckets rather than source_class values.
SOURCE_TRUST_ORDER = [
    "local_db_verified",
    "official_uci",
    "official_university / government",
    "professor_page",
    "external_web",
    "forum/social",
    "llm_inference",
]

_NEWS_DOMAINS = {
    "insidehighered.com",
    "latimes.com",
    "nytimes.com",
    "chronicle.com",
    "edsource.org",
}
_COMMERCIAL_TLDS = (".com", ".net", ".io", ".ai", ".co")
_FORUM_DOMAINS = {"reddit.com", "redd.it"}
_RMP_DOMAINS = {"ratemyprofessors.com"}

_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_MAX_REQUESTS = 20
_rate_windows: dict[str, list[float]] = {}

_fake_results: list[dict[str, Any]] = []


def classify_url(url: Optional[str]) -> dict[str, Any]:
    """Classify a URL into a source class and trust level.

    Missing or unparsable URLs are explicitly `unknown` and not usable
    as factual evidence. This is separate from the local DB, whose
    source_class is represented in prompt rules but not produced by
    the web-search provider.
    """

    domain = _domain_from_url(url)
    if not domain:
        return _classification(
            "unknown",
            domain=None,
            why="missing or unparsable URL; cannot be used as a factual source",
            usable_as_fact=False,
        )

    path = urlsplit(url or "").path.lower()
    if _domain_matches(domain, _RMP_DOMAINS):
        return _classification("rmp", domain, "RateMyProfessors external rating site")

    if _domain_matches(domain, _FORUM_DOMAINS):
        return _classification(
            "reddit",
            domain,
            "forum/social source; anecdotal only, not authoritative fact",
            usable_as_fact=False,
        )

    if domain == "uci.edu" or domain.endswith(".uci.edu"):
        return _classification("official_uci", domain, "UCI official domain")

    if domain.endswith(".gov"):
        return _classification("government", domain, "government domain")

    if domain.endswith(".edu") and _looks_like_professor_page(path):
        return _classification(
            "professor_page",
            domain,
            "university-hosted faculty/profile page",
        )

    if domain.endswith(".edu"):
        return _classification(
            "official_university",
            domain,
            "official university domain",
        )

    if domain in _NEWS_DOMAINS or domain.startswith("news."):
        return _classification("news", domain, "news or education-news domain")

    if domain.endswith(_COMMERCIAL_TLDS):
        return _classification("commercial", domain, "commercial web domain")

    return _classification(
        "unknown",
        domain,
        "domain is not in a recognized source category",
        usable_as_fact=False,
    )


def set_fake_results(results: Iterable[dict[str, Any]]) -> None:
    """Install deterministic fake search results for tests/dev."""

    global _fake_results
    _fake_results = [deepcopy(r) for r in results]


def clear_web_search_state() -> None:
    """Clear fake provider data and in-process rate-limit state."""

    _fake_results.clear()
    _rate_windows.clear()


def search_web(
    *,
    query: str,
    reason: str,
    preferred_domains: Optional[list[str]] = None,
    max_results: Optional[int] = None,
    subject: str = "anonymous",
) -> dict[str, Any]:
    """Run a controlled web search and return normalized result records."""

    started = observability.now()
    searched_at = _utc_now()
    provider = config.web_search_provider()
    normalized_query = (query or "").strip()
    normalized_reason = (reason or "").strip()
    domains = _normalize_preferred_domains(preferred_domains or [])
    limit = _effective_limit(max_results)

    base = {
        "query": normalized_query,
        "reason": normalized_reason,
        "searched_at": searched_at,
        "max_results": limit,
        "preferred_domains": domains,
        "results": [],
    }

    observability.increment("web_search.requests", provider=provider)

    try:
        if not normalized_query:
            return _error_response(
                base,
                "invalid_request",
                "query is required",
                provider=provider,
                started=started,
            )

        if not normalized_reason or _reason_is_placeholder(normalized_reason):
            return _error_response(
                base,
                "invalid_reason",
                "reason is required and must explain why web search is needed",
                provider=provider,
                started=started,
            )

        if not config.web_search_enabled():
            return _error_response(
                base,
                "web_search_disabled",
                "web search is disabled; set WEB_SEARCH_ENABLED=true to enable it",
                provider=provider,
                started=started,
            )

        rate = _check_rate_limit(subject or "anonymous")
        if not rate["ok"]:
            return _error_response(
                base,
                "rate_limited",
                f"web search rate limit exceeded; retry after {rate['retry_after_seconds']}s",
                provider=provider,
                started=started,
                extra={"retry_after_seconds": rate["retry_after_seconds"]},
            )

        raw_results, provider_error = _provider_search(
            provider=provider,
            query=normalized_query,
            preferred_domains=domains,
            max_results=limit,
        )
        if provider_error:
            return _error_response(
                base,
                provider_error["error_code"],
                provider_error["message"],
                provider=provider,
                started=started,
            )

        normalized = [
            _normalize_result(raw, retrieved_at=searched_at)
            for raw in raw_results[:limit]
        ]
        base["results"] = normalized
        base["ok"] = True
        base["provider"] = provider

        for result in normalized:
            observability.increment(
                "web_search.source_class",
                source_class=result.get("source_class", "unknown"),
            )
        observability.increment("web_search.results", len(normalized), provider=provider)
        observability.log_event(
            logger,
            logging.INFO,
            "web_search_completed",
            provider=provider,
            query=_log_value(normalized_query),
            reason=_log_value(normalized_reason),
            max_results=limit,
            preferred_domains=domains,
            result_count=len(normalized),
        )
        return base
    finally:
        observability.observe_ms(
            "web_search.latency_ms",
            observability.elapsed_ms(started),
            provider=provider,
        )


def _provider_search(
    *,
    provider: str,
    query: str,
    preferred_domains: list[str],
    max_results: int,
) -> tuple[list[dict[str, Any]], Optional[dict[str, str]]]:
    if provider == "fake":
        return _fake_provider_search(
            query=query,
            preferred_domains=preferred_domains,
            max_results=max_results,
        ), None

    if provider in {"", "disabled", "none"}:
        return [], {
            "error_code": "provider_unavailable",
            "message": "no web-search provider is configured",
        }

    if not config.web_search_api_key():
        return [], {
            "error_code": "missing_api_key",
            "message": f"{provider} web-search provider requires WEB_SEARCH_API_KEY",
        }

    return [], {
        "error_code": "provider_unimplemented",
        "message": (
            f"{provider} web-search provider is configured but no adapter is "
            "implemented in this build"
        ),
    }


def _fake_provider_search(
    *,
    query: str,
    preferred_domains: list[str],
    max_results: int,
) -> list[dict[str, Any]]:
    del query  # fake provider is deterministic; tests install exact rows.
    results = [deepcopy(r) for r in _fake_results]
    if preferred_domains:
        allowed = set(preferred_domains)
        filtered = []
        for result in results:
            domain = _domain_from_url(result.get("url"))
            if domain and any(domain == d or domain.endswith(f".{d}") for d in allowed):
                filtered.append(result)
        results = filtered
    return results[:max_results]


def _normalize_result(raw: dict[str, Any], *, retrieved_at: str) -> dict[str, Any]:
    url = (raw.get("url") or "").strip()
    classification = classify_url(url)
    title = _truncate((raw.get("title") or "").strip(), TITLE_MAX_CHARS)
    snippet = _truncate((raw.get("snippet") or "").strip(), SNIPPET_MAX_CHARS)
    domain = classification["domain"]

    return {
        "title": title,
        "url": url or None,
        "domain": domain,
        "snippet": snippet,
        "published_at": raw.get("published_at"),
        "retrieved_at": retrieved_at,
        "source_class": classification["source_class"],
        "trust_level": classification["trust_level"],
        "why_trusted_or_not": classification["why_trusted_or_not"],
        "usable_as_fact": classification["usable_as_fact"],
    }


def _classification(
    source_class: str,
    domain: Optional[str],
    why: str,
    *,
    usable_as_fact: bool = True,
) -> dict[str, Any]:
    if source_class not in SOURCE_CLASSES:
        source_class = "unknown"
        usable_as_fact = False
    return {
        "source_class": source_class,
        "domain": domain,
        "trust_level": SOURCE_TRUST_LEVELS[source_class],
        "why_trusted_or_not": why,
        "usable_as_fact": usable_as_fact,
    }


def _domain_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    raw = url.strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _domain_matches(domain: str, known: set[str]) -> bool:
    return any(domain == item or domain.endswith(f".{item}") for item in known)


def _looks_like_professor_page(path: str) -> bool:
    if not path:
        return False
    markers = (
        "/faculty",
        "/people",
        "/person",
        "/profile",
        "/profiles",
        "/staff",
        "/directory",
        "~",
    )
    return any(marker in path for marker in markers)


def _normalize_preferred_domains(domains: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in domains:
        domain = _domain_from_url(str(raw))
        if not domain or domain in seen:
            continue
        seen.add(domain)
        out.append(domain)
    return out[:5]


def _effective_limit(max_results: Optional[int]) -> int:
    if max_results is None:
        return max(1, min(config.web_search_max_results(), HARD_MAX_RESULTS))
    try:
        raw = int(max_results)
    except (TypeError, ValueError):
        raw = DEFAULT_MAX_RESULTS
    return max(1, min(raw, HARD_MAX_RESULTS))


def _reason_is_placeholder(reason: str) -> bool:
    value = reason.strip().lower()
    return value in {"n/a", "na", "none", "search", "web", "lookup", "查一下", "联网"}


def _check_rate_limit(subject: str) -> dict[str, Any]:
    now = time.time()
    window = _rate_windows.setdefault(subject, [])
    cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
    window[:] = [t for t in window if t >= cutoff]
    if len(window) >= _RATE_LIMIT_MAX_REQUESTS:
        retry_after = int(max(1, _RATE_LIMIT_WINDOW_SECONDS - (now - window[0])))
        return {"ok": False, "retry_after_seconds": retry_after}
    window.append(now)
    return {"ok": True, "retry_after_seconds": 0}


def _error_response(
    base: dict[str, Any],
    error_code: str,
    message: str,
    *,
    provider: str,
    started: float,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    observability.increment("web_search.failures", provider=provider, error_code=error_code)
    observability.log_event(
        logger,
        logging.WARNING,
        "web_search_failed",
        provider=provider,
        error_code=error_code,
        query=_log_value(base.get("query", "")),
        reason=_log_value(base.get("reason", "")),
        elapsed_ms=observability.elapsed_ms(started),
    )
    response = {
        **base,
        "ok": False,
        "provider": provider,
        "error_code": error_code,
        "message": message,
    }
    if extra:
        response.update(extra)
    return response


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "…"


def _log_value(value: str) -> str:
    return _truncate(value.replace("\n", " "), LOG_VALUE_MAX_CHARS)
