"""Stateless page fetching and extraction for model-directed deep search."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
import ipaddress
import re
import socket
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from app import config, observability
from app.data.web_search import USER_AGENT, classify_url


MAX_REDIRECTS = 3
MAX_PAGE_BYTES = 1_000_000
MAX_SUMMARY_CHARS = 1_200
MAX_PASSAGES = 8
MAX_PASSAGE_CHARS = 700
MAX_LINKS = 250

_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "ref_src",
}
_BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "metadata.google.internal.",
    "instance-data",
}
_BLOCK_TAGS = {
    "address",
    "article",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "main",
    "nav",
    "p",
    "section",
    "td",
    "th",
    "tr",
}
_IGNORED_TAGS = {"script", "style", "noscript", "svg", "template"}

_fake_pages: dict[str, dict[str, Any]] = {}


def normalize_url(url: str) -> str:
    """Return a stable URL identity used by run-level deduplication."""

    raw = (url or "").strip()
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        if not scheme or not host:
            return raw

        port = parsed.port
        default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        port_suffix = "" if port is None or default_port else f":{port}"
        host_for_netloc = f"[{host}]" if ":" in host else host
        netloc = f"{host_for_netloc}{port_suffix}"

        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        if path != "/":
            path = path.rstrip("/") or "/"

        query_items = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not _is_tracking_param(key)
        ]
        query = urlencode(sorted(query_items), doseq=True)
        return urlunsplit((scheme, netloc, path, query, ""))
    except (TypeError, ValueError):
        return raw


def validate_public_url(url: str, *, resolve_dns: bool = False) -> dict[str, Any]:
    """Validate a public HTTP(S) URL and optionally resolve its host."""

    try:
        raw_parsed = urlsplit((url or "").strip())
    except ValueError:
        return _invalid_url((url or "").strip(), "invalid_url", "URL could not be parsed")
    if raw_parsed.username or raw_parsed.password:
        return _invalid_url((url or "").strip(), "userinfo_not_allowed", "URL user information is not allowed")

    normalized = normalize_url(url)
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError:
        return _invalid_url(normalized, "invalid_port", "URL contains an invalid port")

    if parsed.scheme not in {"http", "https"}:
        return _invalid_url(normalized, "unsupported_scheme", "only http and https URLs are allowed")
    if not parsed.hostname:
        return _invalid_url(normalized, "missing_host", "URL host is required")
    host = parsed.hostname.lower().rstrip(".")
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        return _invalid_url(normalized, "blocked_host", "local and metadata hosts are not allowed")

    literal = _parse_ip(host)
    if literal is not None and not literal.is_global:
        return _invalid_url(normalized, "non_public_ip", "private or non-public IP addresses are not allowed")

    if resolve_dns and literal is None:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    host,
                    port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except OSError as exc:
            return _invalid_url(normalized, "dns_resolution_failed", f"host could not be resolved: {type(exc).__name__}")
        if not addresses:
            return _invalid_url(normalized, "dns_resolution_failed", "host resolved to no addresses")
        for address in addresses:
            resolved_ip = _parse_ip(address)
            if resolved_ip is None or not resolved_ip.is_global:
                return _invalid_url(normalized, "non_public_ip", "host resolves to a private or non-public IP")

    return {"ok": True, "normalized_url": normalized, "domain": host}


def set_fake_pages(pages: Iterable[dict[str, Any]]) -> None:
    """Install deterministic pages used by offline tests."""

    _fake_pages.clear()
    for page in pages:
        normalized = normalize_url(str(page.get("url") or ""))
        if normalized:
            _fake_pages[normalized] = deepcopy(page)


def clear_deep_search_state() -> None:
    _fake_pages.clear()


def fetch_page(url: str) -> dict[str, Any]:
    """Fetch one public page and return bounded, model-facing fields."""

    requested_at = _utc_now()
    validation = validate_public_url(url, resolve_dns=False)
    if not validation["ok"]:
        return {
            **validation,
            "source_url": url,
            "retrieved_at": requested_at,
        }

    normalized = validation["normalized_url"]
    fake = _fake_pages.get(normalized)
    if fake is not None:
        return _extract_response(
            source_url=url,
            final_url=str(fake.get("final_url") or fake.get("url") or normalized),
            body=str(fake.get("html") or fake.get("text") or ""),
            content_type=str(fake.get("content_type") or "text/html; charset=utf-8"),
            status_code=int(fake.get("status_code") or 200),
            retrieved_at=requested_at,
        )

    if not config.web_search_enabled():
        return _fetch_error(
            url,
            requested_at,
            "page_fetch_disabled",
            "page fetching is disabled with web search",
            normalized_url=normalized,
        )

    return _fetch_live_page(url, retrieved_at=requested_at)


def _fetch_live_page(url: str, *, retrieved_at: str) -> dict[str, Any]:
    current_url = url
    session = requests.Session()
    for redirect_count in range(MAX_REDIRECTS + 1):
        validation = validate_public_url(current_url, resolve_dns=True)
        if not validation["ok"]:
            return {
                **validation,
                "source_url": url,
                "retrieved_at": retrieved_at,
            }

        try:
            response = session.get(
                current_url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,application/xhtml+xml"},
                timeout=config.web_search_timeout_seconds(),
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            return _fetch_error(
                url,
                retrieved_at,
                "page_request_failed",
                f"page request failed: {type(exc).__name__}",
                final_url=current_url,
                normalized_url=validation["normalized_url"],
            )

        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("location")
            if not location:
                return _fetch_error(url, retrieved_at, "invalid_redirect", "redirect has no location", final_url=current_url)
            if redirect_count >= MAX_REDIRECTS:
                return _fetch_error(url, retrieved_at, "too_many_redirects", "page exceeded redirect limit", final_url=current_url)
            current_url = urljoin(current_url, location)
            continue

        if response.status_code >= 400:
            return _fetch_error(
                url,
                retrieved_at,
                "page_http_error",
                f"page returned HTTP {response.status_code}",
                final_url=current_url,
                status_code=response.status_code,
            )

        content_type = response.headers.get("content-type", "")
        if not _is_textual_content_type(content_type):
            return _fetch_error(
                url,
                retrieved_at,
                "unsupported_content_type",
                f"page content type is not supported: {content_type or 'unknown'}",
                final_url=current_url,
                status_code=response.status_code,
            )

        declared_length = _safe_int(response.headers.get("content-length"))
        if declared_length is not None and declared_length > MAX_PAGE_BYTES:
            return _fetch_error(url, retrieved_at, "page_too_large", "page exceeds size limit", final_url=current_url)

        body_result = _read_bounded_body(response)
        if not body_result["ok"]:
            return _fetch_error(url, retrieved_at, "page_too_large", "page exceeds size limit", final_url=current_url)

        encoding = response.encoding or "utf-8"
        body = body_result["content"].decode(encoding, errors="replace")
        return _extract_response(
            source_url=url,
            final_url=getattr(response, "url", None) or current_url,
            body=body,
            content_type=content_type,
            status_code=response.status_code,
            retrieved_at=retrieved_at,
        )

    return _fetch_error(url, retrieved_at, "too_many_redirects", "page exceeded redirect limit")


def _extract_response(
    *,
    source_url: str,
    final_url: str,
    body: str,
    content_type: str,
    status_code: int,
    retrieved_at: str,
) -> dict[str, Any]:
    final_validation = validate_public_url(final_url, resolve_dns=False)
    if not final_validation["ok"]:
        return {
            **final_validation,
            "source_url": source_url,
            "final_url": final_url,
            "retrieved_at": retrieved_at,
        }
    if status_code >= 400:
        return _fetch_error(
            source_url,
            retrieved_at,
            "page_http_error",
            f"page returned HTTP {status_code}",
            final_url=final_url,
            status_code=status_code,
        )
    if not _is_textual_content_type(content_type):
        return _fetch_error(
            source_url,
            retrieved_at,
            "unsupported_content_type",
            f"page content type is not supported: {content_type}",
            final_url=final_url,
        )
    if len(body.encode("utf-8")) > MAX_PAGE_BYTES:
        return _fetch_error(source_url, retrieved_at, "page_too_large", "page exceeds size limit", final_url=final_url)

    parsed = _parse_page(body, final_url=final_url)
    classification = classify_url(final_url)
    result = {
        "ok": True,
        "source_url": source_url,
        "final_url": final_url,
        "normalized_url": final_validation["normalized_url"],
        "domain": classification["domain"],
        "retrieved_at": retrieved_at,
        "status_code": status_code,
        "content_type": content_type.split(";", 1)[0].strip().lower(),
        "title": parsed["title"],
        "summary": parsed["summary"],
        "key_passages": parsed["key_passages"],
        "links": parsed["links"],
        "source_class": classification["source_class"],
        "trust_level": classification["trust_level"],
        "trust_reason": classification["why_trusted_or_not"],
        "usable_as_fact": classification["usable_as_fact"],
    }
    observability.increment("deep_search.pages_fetched", source_class=result["source_class"])
    return result


def _parse_page(body: str, *, final_url: str) -> dict[str, Any]:
    if "<" not in body and ">" not in body:
        blocks = [_collapse_ws(line) for line in body.splitlines() if _collapse_ws(line)]
        return {
            "title": None,
            "summary": _build_summary(blocks),
            "key_passages": _build_key_passages(blocks),
            "links": [],
        }

    parser = _PageHTMLParser(final_url)
    parser.feed(body or "")
    parser.close()
    parser.flush_block()
    return {
        "title": _truncate(_collapse_ws(" ".join(parser.title_parts)), 240) or None,
        "summary": _build_summary(parser.blocks),
        "key_passages": _build_key_passages(parser.blocks),
        "links": parser.links[:MAX_LINKS],
    }


class _PageHTMLParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.blocks: list[str] = []
        self.links: list[dict[str, Any]] = []
        self._block_parts: list[str] = []
        self._ignored_depth = 0
        self._in_title = False
        self._active_link: Optional[dict[str, Any]] = None
        self._link_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in _IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self.flush_block()
        if tag == "a":
            attr = {key.lower(): value or "" for key, value in attrs}
            href = attr.get("href", "").strip()
            self._active_link = {
                "href": href,
                "html_line": self.getpos()[0],
                "link_index": len(self.links),
            }
            self._link_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _IGNORED_TAGS:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._active_link is not None:
            self._append_link(self._active_link, _collapse_ws(" ".join(self._link_parts)))
            self._active_link = None
            self._link_parts = []
        if tag in _BLOCK_TAGS:
            self.flush_block()

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        if self._active_link is not None:
            self._link_parts.append(data)
        self._block_parts.append(data)

    def flush_block(self) -> None:
        text = _collapse_ws(" ".join(self._block_parts))
        if text and (not self.blocks or self.blocks[-1] != text):
            self.blocks.append(text)
        self._block_parts = []

    def _append_link(self, raw: dict[str, Any], text: str) -> None:
        absolute_url = urljoin(self.base_url, raw["href"])
        parsed = urlsplit(absolute_url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return
        normalized = normalize_url(absolute_url)
        classification = classify_url(normalized)
        self.links.append(
            {
                "text": text or normalized,
                "url": absolute_url,
                "normalized_url": normalized,
                "domain": classification["domain"],
                "source_position": {
                    "html_line": raw["html_line"],
                    "link_index": raw["link_index"],
                },
                "source_class": classification["source_class"],
                "trust_level": classification["trust_level"],
                "trust_reason": classification["why_trusted_or_not"],
            }
        )


def _build_summary(blocks: list[str]) -> Optional[str]:
    useful = [block for block in blocks if len(block) >= 20]
    if not useful:
        useful = blocks
    return _truncate(" ".join(useful[:4]), MAX_SUMMARY_CHARS) or None


def _build_key_passages(blocks: list[str]) -> list[dict[str, Any]]:
    passages: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if len(block) < 30:
            continue
        passages.append(
            {
                "text": _truncate(block, MAX_PASSAGE_CHARS),
                "source_position": {"block_index": index},
            }
        )
        if len(passages) >= MAX_PASSAGES:
            break
    return passages


def _read_bounded_body(response: requests.Response) -> dict[str, Any]:
    content = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        content.extend(chunk)
        if len(content) > MAX_PAGE_BYTES:
            return {"ok": False, "content": b""}
    return {"ok": True, "content": bytes(content)}


def _is_tracking_param(key: str) -> bool:
    lowered = key.lower()
    return lowered.startswith("utm_") or lowered in _TRACKING_PARAMS


def _parse_ip(value: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _is_textual_content_type(content_type: str) -> bool:
    mime = (content_type or "text/html").split(";", 1)[0].strip().lower()
    return mime in {"text/html", "text/plain", "application/xhtml+xml"}


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _invalid_url(normalized_url: str, error_code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": error_code,
        "message": message,
        "normalized_url": normalized_url,
    }


def _fetch_error(
    source_url: str,
    retrieved_at: str,
    error_code: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": error_code,
        "message": message,
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        **extra,
    }


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
