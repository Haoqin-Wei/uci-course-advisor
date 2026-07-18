"""Fixed Registrar WebSoc workflows.

This module handles department-level Schedule of Classes pages where
schools/departments publish enrollment restriction comments above the
course table. It is intentionally separate from agentic web search:
the source URL and allowed follow-up links come from Registrar WebSoc,
not from a model-selected search result.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import urlencode, urljoin, urlsplit

import requests

from app.catalog.term import Term

logger = logging.getLogger(__name__)


WEBSOC_URL = "https://www.reg.uci.edu/perl/WebSoc"
REQUEST_TIMEOUT_S = 12
LINK_TEXT_MAX_CHARS = 1500

_QUARTER_CODES = {
    "Winter": "03",
    "Spring": "14",
    "Summer": "76",
    "Fall": "92",
}

_ICS_LINK_ROLES = (
    ("course-enrollment-restrictions", "ics_undergraduate_restrictions"),
    ("graduate-academic-advising/course-updates", "ics_graduate_course_updates"),
    (
        "undergraduate-programs/majors-minors/undergraduate-student-policies",
        "ics_undergraduate_student_policies",
    ),
    ("undergraduate-academic-advising/concurrent-enrollment", "ics_concurrent_enrollment"),
)


@dataclass
class ParsedHTML:
    text: str
    links: list[dict[str, Any]]


def build_websoc_department_params(term: str, department: str) -> dict[str, str]:
    parsed = Term.parse(term)
    if not parsed:
        raise ValueError(f"could not parse term {term!r}")
    dept = (department or "").strip().upper()
    if not dept:
        raise ValueError("department is required")
    return {
        "YearTerm": f"{parsed.year}-{_QUARTER_CODES[parsed.quarter]}",
        "Dept": dept,
        "CourseCodes": "",
        "InstrName": "",
        "CourseTitle": "",
        "ClassType": "ALL",
        "Units": "",
        "Days": "",
        "StartTime": "",
        "EndTime": "",
        "MaxCap": "",
        "FullCourses": "ANY",
        "FontSize": "100",
        "CancelledCourses": "Exclude",
        "Bldg": "",
        "Room": "",
    }


def build_websoc_department_url(term: str, department: str) -> str:
    return f"{WEBSOC_URL}?{urlencode(build_websoc_department_params(term, department))}"


def fetch_websoc_department_restrictions(
    *,
    term: str,
    department: str,
    session: Optional[requests.Session] = None,
) -> dict[str, Any]:
    """Fetch and parse a fixed Registrar WebSoc department page."""

    params = build_websoc_department_params(term, department)
    source_url = f"{WEBSOC_URL}?{urlencode(params)}"
    http = session or requests.Session()
    retrieved_at = _utc_now()
    try:
        response = http.get(WEBSOC_URL, params=params, timeout=REQUEST_TIMEOUT_S)
        response.raise_for_status()
    except requests.RequestException as e:
        logger.warning("WebSoc department workflow failed: %s", e)
        return {
            "ok": False,
            "workflow_id": "websoc_department_restrictions",
            "error_code": "websoc_request_failed",
            "message": f"Registrar WebSoc request failed: {type(e).__name__}",
            "term": term,
            "department": department,
            "source_url": source_url,
            "retrieved_at": retrieved_at,
        }

    return parse_websoc_department_html(
        response.text,
        term=term,
        department=department,
        source_url=source_url,
        retrieved_at=retrieved_at,
    )


def parse_websoc_department_html(
    html: str,
    *,
    term: str,
    department: str,
    source_url: str,
    retrieved_at: Optional[str] = None,
) -> dict[str, Any]:
    parsed = _parse_html(html)
    text = parsed.text
    blocks = _extract_comment_blocks(text)
    school_comments = "\n\n".join(
        block["text"] for block in blocks if block["block_type"] == "school"
    ).strip()
    department_comments = "\n\n".join(
        block["text"] for block in blocks if block["block_type"] == "department"
    ).strip()
    links = _assign_links_to_blocks(parsed.links, blocks, source_url=source_url)
    fields = _extract_restriction_fields("\n\n".join(block["text"] for block in blocks))
    extracted_any = any(v not in (None, "", []) for v in fields.values())

    return {
        "ok": True,
        "mode": "workflow",
        "workflow_id": "websoc_department_restrictions",
        "term": term,
        "department": (department or "").strip().upper(),
        "source_url": source_url,
        "retrieved_at": retrieved_at or _utc_now(),
        "search_criteria": {
            "department": _search_text(r"Department:\s*([A-Z0-9&/ ]+)", text),
            "exclude_cancelled_courses": bool(
                re.search(r"Exclude cancelled courses", text, re.IGNORECASE)
            ),
        },
        "registration_ends": _search_text(
            r"Registration for term ends on\s+([^\n]+)", text
        ),
        "school_comments": school_comments or None,
        "department_comments": department_comments or None,
        "comment_blocks": blocks,
        "fields": fields,
        "links": links,
        "extraction_status": "complete" if extracted_any else "partial",
    }


def _parse_html(html: str) -> ParsedHTML:
    parser = _WebSocTopHTMLParser()
    parser.feed(html or "")
    parser.close()
    return ParsedHTML(
        text=_clean_text("\n".join(parser.text_parts)),
        links=parser.links,
    )


class _WebSocTopHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[dict[str, Any]] = []
        self._active_href: Optional[str] = None
        self._active_link_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr = {k.lower(): v or "" for k, v in attrs}
        if tag == "a":
            self._active_href = attr.get("href")
            self._active_link_parts = []
        if tag in {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "table"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._active_href is not None:
            self._active_link_parts.append(data)
        self.text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._active_href is not None:
            text = _collapse_ws(" ".join(self._active_link_parts))
            self.links.append(
                {
                    "text": text or self._active_href,
                    "url": self._active_href,
                    "domain": _domain_from_url(self._active_href),
                }
            )
            self._active_href = None
            self._active_link_parts = []
        if tag in {"p", "div", "tr", "li", "h1", "h2", "h3", "table"}:
            self.text_parts.append("\n")


def _extract_comment_blocks(text: str) -> list[dict[str, Any]]:
    matches = list(re.finditer(r"([^\n]+? comments:)", text, flags=re.IGNORECASE))
    blocks: list[dict[str, Any]] = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block_text = _clean_text(text[start:end])
        title = _collapse_ws(match.group(1))
        title_lower = title.lower()
        block_type = "department" if "department comments" in title_lower else "school"
        blocks.append(
            {
                "title": title,
                "block_type": block_type,
                "text": block_text,
            }
        )
    return blocks


def _assign_links_to_blocks(
    links: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    *,
    source_url: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], int] = {}
    for link in links:
        absolute_url = urljoin(source_url, link.get("url") or "")
        key = (link.get("text") or "", link.get("url") or "")
        occurrence_index = seen.get(key, 0)
        seen[key] = occurrence_index + 1
        assigned = _find_nth_link_block(link, blocks, occurrence_index)
        out.append(
            {
                "text": link.get("text") or absolute_url,
                "url": absolute_url,
                "domain": _domain_from_url(absolute_url),
                "link_role": _classify_link_role(absolute_url, link.get("text") or ""),
                "source_block": assigned["title"] if assigned else None,
                "source_block_type": assigned["block_type"] if assigned else None,
                "source_url": source_url,
                "is_uci_official": _is_uci_domain(absolute_url),
                "allowed_for_deep_read": _is_uci_domain(absolute_url),
            }
        )
    return out


def fetch_linked_official_pages(
    workflow_result: dict[str, Any],
    *,
    session: Optional[requests.Session] = None,
    max_pages: int = 3,
) -> dict[str, Any]:
    """Fetch official pages explicitly linked from WebSoc comments.

    This is intentionally not a general crawler. It only follows links
    already present in a successful WebSoc workflow result and only
    keeps UCI official pages selected by deterministic rules.
    """

    if not workflow_result.get("ok"):
        return {
            "ok": False,
            "workflow_id": workflow_result.get("workflow_id"),
            "error_code": "invalid_workflow_result",
            "message": "cannot deep-read links from an unsuccessful workflow result",
            "pages": [],
        }

    selected = [
        link for link in workflow_result.get("links", [])
        if _should_deep_read_link(link, workflow_result)
    ][:max(0, max_pages)]
    http = session or requests.Session()
    pages: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for link in selected:
        retrieved_at = _utc_now()
        try:
            response = http.get(link["url"], timeout=REQUEST_TIMEOUT_S)
            response.raise_for_status()
        except requests.RequestException as e:
            errors.append(
                {
                    "url": link["url"],
                    "error_code": "linked_page_request_failed",
                    "message": f"official linked page request failed: {type(e).__name__}",
                }
            )
            continue

        final_url = getattr(response, "url", link["url"]) or link["url"]
        if not _is_uci_domain(final_url):
            errors.append(
                {
                    "url": link["url"],
                    "final_url": final_url,
                    "error_code": "linked_page_left_allowed_domain",
                    "message": "linked page redirected outside uci.edu",
                }
            )
            continue

        parsed = _parse_html(response.text)
        pages.append(
            {
                "url": final_url,
                "domain": _domain_from_url(final_url),
                "retrieved_at": retrieved_at,
                "source_link_text": link.get("text"),
                "source_block": link.get("source_block"),
                "link_role": link.get("link_role"),
                "text_excerpt": _truncate(parsed.text, LINK_TEXT_MAX_CHARS),
            }
        )

    return {
        "ok": True,
        "workflow_id": workflow_result.get("workflow_id"),
        "source_url": workflow_result.get("source_url"),
        "selected_count": len(selected),
        "pages": pages,
        "errors": errors,
    }


def _should_deep_read_link(link: dict[str, Any], workflow_result: dict[str, Any]) -> bool:
    if not link.get("allowed_for_deep_read"):
        return False
    role = link.get("link_role")
    if role and role.startswith("ics_"):
        return True
    fields = workflow_result.get("fields") or {}
    lacks_restriction_date = not fields.get("major_restriction_removed_at")
    text = f"{link.get('text') or ''} {link.get('url') or ''}".lower()
    return lacks_restriction_date and any(
        needle in text
        for needle in ("restriction", "timeline", "policy", "policies", "enroll")
    )


def _classify_link_role(url: str, text: str = "") -> Optional[str]:
    lowered = f"{url} {text}".lower()
    for needle, role in _ICS_LINK_ROLES:
        if needle in lowered:
            return role
    if "restriction" in lowered or "timeline" in lowered:
        return "restriction_details"
    if "policy" in lowered or "policies" in lowered:
        return "policy_details"
    if "enroll" in lowered:
        return "enrollment_instructions"
    return None


def _find_nth_link_block(
    link: dict[str, Any],
    blocks: list[dict[str, Any]],
    occurrence_index: int,
) -> Optional[dict[str, Any]]:
    remaining = occurrence_index
    needles = list(dict.fromkeys(
        value for value in (link.get("text"), link.get("url")) if value
    ))
    for block in blocks:
        haystack = block["text"]
        count = sum(haystack.count(needle) for needle in needles)
        if count <= 0:
            continue
        if remaining < count:
            return block
        remaining -= count
    return None


def _extract_restriction_fields(text: str) -> dict[str, Any]:
    return {
        "add_deadline": _search_text(
            r"\bADD:\s*The deadline to add courses.*? is (.*?)(?:\.|\n)",
            text,
        ),
        "drop_deadline": _search_text(
            r"\bDROP:\s*The deadline to drop courses.*? is (.*?)(?:\.|\n)",
            text,
        ),
        "change_deadline": _search_text(
            r"\bCHANGE:\s*The deadline to change.*? is (.*?)(?:\.|\n)",
            text,
        ),
        "major_restriction_removed_at": _search_text(
            r"Major restrictions.*? removed on (.*?)(?:\.|\n)",
            text,
        ),
        "nors_removed_at": _search_text(
            r"New Only Restrictions\s*\(NORS\).*? removed on (.*?)(?:\.|\n)",
            text,
        ),
        "contact_emails": sorted(set(re.findall(
            r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
            text,
        ))),
    }


def _search_text(pattern: str, text: str) -> Optional[str]:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return _collapse_ws(match.group(1))


def _clean_text(text: str) -> str:
    lines = [_collapse_ws(line) for line in (text or "").splitlines()]
    return "\n".join(line for line in lines if line)


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _truncate(text: str, limit: int) -> str:
    collapsed = _clean_text(text)
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "…"


def _domain_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    parsed = urlsplit(url)
    domain = parsed.hostname or ""
    return domain.lower() or None


def _is_uci_domain(url: str) -> bool:
    domain = _domain_from_url(url)
    return bool(domain and (domain == "uci.edu" or domain.endswith(".uci.edu")))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
