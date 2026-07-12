"""Controlled web-search interface for the agent.

The first implementation is intentionally conservative:

- tests use fake/disabled providers so CI stays offline;
- development can use the DuckDuckGo HTML provider without an API key;
- unimplemented external providers return structured unavailable errors;
- results are normalized to small search-result records, never full page
  content, so web evidence cannot pollute local DB-verified data.
"""

from __future__ import annotations

import logging
import time
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlsplit
from typing import Any, Iterable, Optional

import requests

from app import config, observability

logger = logging.getLogger(__name__)


DEFAULT_MAX_RESULTS = 5
HARD_MAX_RESULTS = 10
SNIPPET_MAX_CHARS = 500
TITLE_MAX_CHARS = 180
LOG_VALUE_MAX_CHARS = 160
DUCKDUCKGO_HTML_URL = "https://lite.duckduckgo.com/lite/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)

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
    observability.log_event(
        logger,
        logging.INFO,
        "web_search_started",
        provider=provider,
        query=_log_value(normalized_query),
        reason=_log_value(normalized_reason),
        max_results=limit,
        preferred_domains=domains,
    )

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

        for rank, result in enumerate(normalized, start=1):
            observability.increment(
                "web_search.source_class",
                source_class=result.get("source_class", "unknown"),
            )
            _log_search_result(rank, result)
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

    if provider in {"duckduckgo", "ddg"}:
        try:
            return _duckduckgo_search(
                query=query,
                preferred_domains=preferred_domains,
                max_results=max_results,
            ), None
        except requests.RequestException as e:
            return [], {
                "error_code": "provider_network_error",
                "message": f"duckduckgo search request failed: {type(e).__name__}",
            }
        except Exception as e:
            logger.warning("duckduckgo provider failed: %s: %s", type(e).__name__, e)
            return [], {
                "error_code": "provider_parse_error",
                "message": "duckduckgo search response could not be parsed",
            }

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


def _duckduckgo_search(
    *,
    query: str,
    preferred_domains: list[str],
    max_results: int,
) -> list[dict[str, Any]]:
    hinted_query = _query_with_domain_hints(query, preferred_domains)
    queries = [hinted_query] if hinted_query == query else [hinted_query, query]
    session = requests.Session()
    last_error: Optional[requests.RequestException] = None
    for attempt, search_query in enumerate(queries, start=1):
        observability.log_event(
            logger,
            logging.INFO,
            "web_search_provider_attempt",
            provider="duckduckgo",
            attempt=attempt,
            endpoint=DUCKDUCKGO_HTML_URL,
            search_query=_log_value(search_query),
            preferred_domains=preferred_domains,
            timeout_seconds=config.web_search_timeout_seconds(),
        )
        try:
            response = session.get(
                DUCKDUCKGO_HTML_URL,
                params={"q": search_query},
                headers={"User-Agent": USER_AGENT},
                timeout=config.web_search_timeout_seconds(),
            )
            if response.status_code != 200:
                observability.log_event(
                    logger,
                    logging.WARNING,
                    "web_search_provider_attempt_failed",
                    provider="duckduckgo",
                    attempt=attempt,
                    status_code=response.status_code,
                    search_query=_log_value(search_query),
                )
                raise requests.RequestException(
                    f"unexpected DuckDuckGo status {response.status_code}"
                )
            response.raise_for_status()
        except requests.RequestException as e:
            last_error = e
            observability.log_event(
                logger,
                logging.WARNING,
                "web_search_provider_attempt_failed",
                provider="duckduckgo",
                attempt=attempt,
                error_type=type(e).__name__,
                search_query=_log_value(search_query),
            )
            continue

        parser = _DuckDuckGoHTMLParser()
        parser.feed(response.text)
        parser.close()
        observability.log_event(
            logger,
            logging.INFO,
            "web_search_provider_attempt_parsed",
            provider="duckduckgo",
            attempt=attempt,
            search_query=_log_value(search_query),
            result_count=len(parser.results),
        )
        if parser.results or search_query == query:
            return parser.results[:max_results]
        observability.log_event(
            logger,
            logging.INFO,
            "web_search_provider_fallback",
            provider="duckduckgo",
            from_query=_log_value(search_query),
            to_query=_log_value(query),
            reason="domain-hinted query returned no parseable results",
        )

    if last_error is not None:
        raise last_error
    return []


def _query_with_domain_hints(query: str, preferred_domains: list[str]) -> str:
    if not preferred_domains:
        return query
    if len(preferred_domains) == 1:
        return f"{query} site:{preferred_domains[0]}"
    site_clause = " OR ".join(f"site:{domain}" for domain in preferred_domains[:3])
    return f"{query} ({site_clause})"


class _DuckDuckGoHTMLParser(HTMLParser):
    """Small parser for DuckDuckGo's no-JS HTML result page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, Any]] = []
        self._active: Optional[str] = None
        self._current: Optional[dict[str, Any]] = None
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._last_result: Optional[dict[str, Any]] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr = {k: v or "" for k, v in attrs}
        class_name = attr.get("class", "")
        if tag == "a" and ("result__a" in class_name or "result-link" in class_name):
            self._active = "title"
            self._title_parts = []
            self._current = {"url": _decode_duckduckgo_href(attr.get("href", ""))}
            return
        if (
            ("result__snippet" in class_name or "result-snippet" in class_name)
            and self._last_result is not None
        ):
            self._active = "snippet"
            self._snippet_parts = []
            return
        if "timestamp" in class_name and self._last_result is not None:
            self._active = "timestamp"
            self._snippet_parts = []

    def handle_data(self, data: str) -> None:
        if self._active == "title":
            self._title_parts.append(data)
        elif self._active in {"snippet", "timestamp"}:
            self._snippet_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._active == "title" and self._current is not None:
            if tag != "a":
                return
            title = _collapse_ws(" ".join(self._title_parts))
            url = self._current.get("url")
            if title and url:
                result = {"title": title, "url": url, "snippet": ""}
                self.results.append(result)
                self._last_result = result
            self._current = None
            self._title_parts = []
            self._active = None
            return
        if self._active == "snippet" and self._last_result is not None:
            if tag not in {"a", "td", "div"}:
                return
            self._last_result["snippet"] = _collapse_ws(" ".join(self._snippet_parts))
            self._snippet_parts = []
            self._active = None
            return
        if self._active == "timestamp" and self._last_result is not None:
            if tag != "span":
                return
            published_at = _collapse_ws(" ".join(self._snippet_parts))
            self._last_result["published_at"] = published_at or None
            self._snippet_parts = []
            self._active = None


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
    observability.log_event(
        logger,
        logging.INFO,
        "web_search_provider_attempt_parsed",
        provider="fake",
        preferred_domains=preferred_domains,
        result_count=len(results[:max_results]),
    )
    return results[:max_results]


def _decode_duckduckgo_href(href: str) -> Optional[str]:
    if not href:
        return None
    candidate = href.strip()
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    parsed = urlsplit(candidate)
    if "duckduckgo.com" in (parsed.hostname or ""):
        params = parse_qs(parsed.query)
        uddg = params.get("uddg")
        if uddg:
            return unquote(uddg[0])
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return candidate
    # Very old DDG markup can expose raw URLs in escaped redirect path
    # fragments. Treat anything else as unusable rather than inventing.
    return None


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


def _collapse_ws(value: str) -> str:
    return " ".join(value.split())


def _log_value(value: str) -> str:
    return _truncate(value.replace("\n", " "), LOG_VALUE_MAX_CHARS)


def _log_search_result(rank: int, result: dict[str, Any]) -> None:
    observability.log_event(
        logger,
        logging.INFO,
        "web_search_result",
        rank=rank,
        title=_log_value(result.get("title") or ""),
        url=result.get("url"),
        domain=result.get("domain"),
        snippet=_log_value(result.get("snippet") or ""),
        published_at=result.get("published_at"),
        source_class=result.get("source_class"),
        trust_level=result.get("trust_level"),
        usable_as_fact=result.get("usable_as_fact"),
    )
