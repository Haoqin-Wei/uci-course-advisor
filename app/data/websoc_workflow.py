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
from urllib.parse import urljoin, urlsplit

import requests

from app import observability
from app.catalog.term import Term

logger = logging.getLogger(__name__)


WEBSOC_URL = "https://www.reg.uci.edu/perl/WebSoc"
REQUEST_TIMEOUT_S = 12
LINK_TEXT_MAX_CHARS = 1500
LINK_MAX_BYTES = 500_000
LOG_TEXT_MAX_CHARS = 1000

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


@dataclass
class ParsedForm:
    terms: dict[str, str]
    departments: dict[str, str]


def build_websoc_department_params(term: str, department: str) -> dict[str, str]:
    parsed = Term.parse(term)
    if not parsed:
        raise ValueError(f"could not parse term {term!r}")
    dept = (department or "").strip().upper()
    if not dept:
        raise ValueError("department is required")
    return {
        "Submit": "Display Web Results",
        "YearTerm": f"{parsed.year}-{_QUARTER_CODES[parsed.quarter]}",
        "ShowComments": "on",
        "ShowFinals": "on",
        "Breadth": "ANY",
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
    build_websoc_department_params(term, department)
    return WEBSOC_URL


def fetch_websoc_department_restrictions(
    *,
    term: str,
    department: str,
    session: Optional[requests.Session] = None,
) -> dict[str, Any]:
    """Fetch and parse a fixed Registrar WebSoc department page."""

    params = build_websoc_department_params(term, department)
    source_url = build_websoc_department_url(term, department)
    http = session or requests.Session()
    retrieved_at = _utc_now()
    form_result = fetch_websoc_form_options(session=http)
    if not form_result.get("ok"):
        return {
            **form_result,
            "workflow_id": "websoc_department_restrictions",
            "term": term,
            "department": department,
            "source_url": source_url,
            "request_method": "POST",
            "request_form": params,
            "retrieved_at": retrieved_at,
        }

    requested_term_code = params["YearTerm"]
    requested_department = params["Dept"]
    if requested_term_code not in form_result["terms"]:
        return _workflow_error(
            error_code="websoc_term_unavailable",
            message=f"term {term!r} is not available in the current WebSoc form",
            term=term,
            department=department,
            request_form=params,
            retrieved_at=retrieved_at,
            available_terms=form_result["terms"],
        )
    if requested_department not in form_result["departments"]:
        return _workflow_error(
            error_code="websoc_department_unavailable",
            message=(
                f"department {requested_department!r} is not available in the current "
                "WebSoc form"
            ),
            term=term,
            department=department,
            request_form=params,
            retrieved_at=retrieved_at,
            available_departments=form_result["departments"],
        )

    observability.log_event(
        logger,
        logging.INFO,
        "websoc_search_started",
        workflow_id="websoc_department_restrictions",
        web_search_url=source_url,
        request_method="POST",
        request_form=params,
        term=term,
        department=(department or "").strip().upper(),
        timeout_seconds=REQUEST_TIMEOUT_S,
    )
    try:
        response = http.post(WEBSOC_URL, data=params, timeout=REQUEST_TIMEOUT_S)
        observability.log_event(
            logger,
            logging.INFO,
            "websoc_search_response",
            workflow_id="websoc_department_restrictions",
            web_search_url=source_url,
            request_method="POST",
            request_form=params,
            final_url=getattr(response, "url", None) or source_url,
            status_code=getattr(response, "status_code", None),
            content_type=(getattr(response, "headers", {}) or {}).get("content-type"),
            content_length=len((getattr(response, "text", "") or "").encode("utf-8")),
        )
        response.raise_for_status()
    except requests.RequestException as e:
        observability.log_event(
            logger,
            logging.WARNING,
            "websoc_search_failed",
            workflow_id="websoc_department_restrictions",
            web_search_url=source_url,
            request_method="POST",
            request_form=params,
            term=term,
            department=(department or "").strip().upper(),
            error_type=type(e).__name__,
            error=str(e),
        )
        return {
            "ok": False,
            "workflow_id": "websoc_department_restrictions",
            "error_code": "websoc_request_failed",
            "message": f"Registrar WebSoc request failed: {type(e).__name__}",
            "term": term,
            "department": department,
            "source_url": source_url,
            "request_method": "POST",
            "request_form": params,
            "retrieved_at": retrieved_at,
        }

    result = parse_websoc_department_html(
        response.text,
        term=term,
        department=department,
        source_url=source_url,
        retrieved_at=retrieved_at,
    )
    result["request_method"] = "POST"
    result["request_form"] = params
    observability.log_event(
        logger,
        logging.INFO if result.get("ok") else logging.WARNING,
        "websoc_search_completed",
        workflow_id=result["workflow_id"],
        web_search_url=result["source_url"],
        request_method=result["request_method"],
        request_form=result["request_form"],
        term=result["term"],
        response_term=result.get("response_term"),
        requested_department=result["department"],
        response_department=(result.get("search_criteria") or {}).get("department"),
        registration_ends=result.get("registration_ends"),
        extraction_status=result.get("extraction_status"),
        validation=result.get("validation"),
        error_code=result.get("error_code"),
        comment_block_count=len(result.get("comment_blocks") or []),
        school_comments=_log_text(result.get("school_comments")),
        department_comments=_log_text(result.get("department_comments")),
        restriction_fields=result.get("fields"),
        link_count=len(result.get("links") or []),
        result_urls=[link.get("url") for link in result.get("links") or []],
    )
    return result


def fetch_websoc_form_options(
    *,
    session: Optional[requests.Session] = None,
) -> dict[str, Any]:
    """Read the live form so submitted term and department values are validated."""

    http = session or requests.Session()
    retrieved_at = _utc_now()
    observability.log_event(
        logger,
        logging.INFO,
        "websoc_form_started",
        web_search_url=WEBSOC_URL,
        request_method="GET",
        timeout_seconds=REQUEST_TIMEOUT_S,
    )
    try:
        response = http.get(WEBSOC_URL, timeout=REQUEST_TIMEOUT_S)
        observability.log_event(
            logger,
            logging.INFO,
            "websoc_form_response",
            web_search_url=WEBSOC_URL,
            request_method="GET",
            final_url=getattr(response, "url", None) or WEBSOC_URL,
            status_code=getattr(response, "status_code", None),
            content_type=(getattr(response, "headers", {}) or {}).get("content-type"),
            content_length=len((getattr(response, "text", "") or "").encode("utf-8")),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        observability.log_event(
            logger,
            logging.WARNING,
            "websoc_form_failed",
            web_search_url=WEBSOC_URL,
            request_method="GET",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return {
            "ok": False,
            "error_code": "websoc_form_request_failed",
            "message": f"Registrar WebSoc form request failed: {type(exc).__name__}",
            "source_url": WEBSOC_URL,
            "retrieved_at": retrieved_at,
        }

    parsed = _parse_websoc_form(response.text)
    if not parsed.terms or not parsed.departments:
        observability.log_event(
            logger,
            logging.WARNING,
            "websoc_form_failed",
            web_search_url=WEBSOC_URL,
            request_method="GET",
            error_type="form_parse_failed",
            term_count=len(parsed.terms),
            department_count=len(parsed.departments),
        )
        return {
            "ok": False,
            "error_code": "websoc_form_parse_failed",
            "message": "Registrar WebSoc form did not contain term and department options",
            "source_url": WEBSOC_URL,
            "retrieved_at": retrieved_at,
        }

    observability.log_event(
        logger,
        logging.INFO,
        "websoc_form_completed",
        web_search_url=WEBSOC_URL,
        request_method="GET",
        term_count=len(parsed.terms),
        department_count=len(parsed.departments),
    )
    return {
        "ok": True,
        "source_url": WEBSOC_URL,
        "retrieved_at": retrieved_at,
        "terms": parsed.terms,
        "departments": parsed.departments,
    }


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
    concrete_field_names = (
        "add_deadline",
        "drop_deadline",
        "change_deadline",
        "major_restriction_removed_at",
        "nors_removed_at",
    )
    extracted_any = any(
        fields.get(field_name) not in (None, "", [])
        for field_name in concrete_field_names
    )
    requested_term = Term.parse(term)
    response_term = _extract_result_term(text)
    requested_department = _normalize_department(department)
    response_department = _normalize_department(
        _search_text(r"Department:\s*([A-Z0-9&/ ]+)", text)
    )
    validation_errors: list[dict[str, str]] = []
    if not re.search(r"Schedule of Classes search results", text, re.IGNORECASE):
        validation_errors.append(
            {
                "error_code": "websoc_not_search_results",
                "message": "WebSoc response is not a Schedule of Classes results page",
            }
        )
    if response_department != requested_department:
        validation_errors.append(
            {
                "error_code": "websoc_department_mismatch",
                "message": (
                    f"WebSoc returned department {response_department or 'missing'} "
                    f"instead of {requested_department}"
                ),
            }
        )
    if requested_term is None or response_term != requested_term.display():
        validation_errors.append(
            {
                "error_code": "websoc_term_mismatch",
                "message": (
                    f"WebSoc returned term {response_term or 'missing'} instead of "
                    f"{requested_term.display() if requested_term else term}"
                ),
            }
        )
    if not blocks:
        validation_errors.append(
            {
                "error_code": "websoc_comments_missing",
                "message": "WebSoc results page did not contain school or department comments",
            }
        )

    validation_ok = not validation_errors

    result = {
        "ok": validation_ok,
        "mode": "workflow",
        "workflow_id": "websoc_department_restrictions",
        "term": term,
        "department": requested_department,
        "source_url": source_url,
        "retrieved_at": retrieved_at or _utc_now(),
        "response_term": response_term,
        "search_criteria": {
            "department": response_department,
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
        "validation": {
            "ok": validation_ok,
            "errors": validation_errors,
        },
        "extraction_status": (
            "invalid" if not validation_ok else "complete" if extracted_any else "partial"
        ),
    }
    if validation_errors:
        result["error_code"] = validation_errors[0]["error_code"]
        result["message"] = "; ".join(error["message"] for error in validation_errors)
    return result


def _parse_websoc_form(html: str) -> ParsedForm:
    parser = _WebSocFormHTMLParser()
    parser.feed(html or "")
    parser.close()
    return ParsedForm(
        terms=parser.options.get("YearTerm", {}),
        departments=parser.options.get("Dept", {}),
    )


class _WebSocFormHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.options: dict[str, dict[str, str]] = {}
        self._select_name: Optional[str] = None
        self._option_value: Optional[str] = None
        self._option_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr = {key.lower(): value or "" for key, value in attrs}
        if tag == "select":
            name = attr.get("name")
            self._select_name = name if name in {"YearTerm", "Dept"} else None
        elif tag == "option" and self._select_name:
            self._option_value = attr.get("value", "").strip()
            self._option_text = []

    def handle_data(self, data: str) -> None:
        if self._option_value is not None:
            self._option_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._select_name and self._option_value is not None:
            if self._option_value:
                self.options.setdefault(self._select_name, {})[self._option_value] = (
                    _collapse_ws(" ".join(self._option_text))
                )
            self._option_value = None
            self._option_text = []
        elif tag == "select":
            self._select_name = None


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
    observability.log_event(
        logger,
        logging.INFO,
        "websoc_linked_search_selected",
        workflow_id=workflow_result.get("workflow_id"),
        source_url=workflow_result.get("source_url"),
        selected_count=len(selected),
        result_urls=[link.get("url") for link in selected],
    )
    http = session or requests.Session()
    pages: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for link in selected:
        retrieved_at = _utc_now()
        linked_url = link["url"]
        observability.log_event(
            logger,
            logging.INFO,
            "websoc_linked_page_started",
            workflow_id=workflow_result.get("workflow_id"),
            web_search_url=linked_url,
            source_url=workflow_result.get("source_url"),
            link_role=link.get("link_role"),
            source_block=link.get("source_block"),
        )
        try:
            response = http.get(linked_url, timeout=REQUEST_TIMEOUT_S)
            observability.log_event(
                logger,
                logging.INFO,
                "websoc_linked_page_response",
                workflow_id=workflow_result.get("workflow_id"),
                web_search_url=linked_url,
                final_url=getattr(response, "url", None) or linked_url,
                status_code=getattr(response, "status_code", None),
                content_type=(getattr(response, "headers", {}) or {}).get("content-type"),
                content_length=len((getattr(response, "text", "") or "").encode("utf-8")),
            )
            response.raise_for_status()
        except requests.RequestException as e:
            observability.log_event(
                logger,
                logging.WARNING,
                "websoc_linked_page_failed",
                workflow_id=workflow_result.get("workflow_id"),
                web_search_url=linked_url,
                error_type=type(e).__name__,
                error=str(e),
            )
            errors.append(
                {
                    "url": linked_url,
                    "error_code": "linked_page_request_failed",
                    "message": f"official linked page request failed: {type(e).__name__}",
                }
            )
            continue

        final_url = getattr(response, "url", linked_url) or linked_url
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

        content_type = (getattr(response, "headers", {}) or {}).get("content-type", "")
        if content_type and not _is_textual_content_type(content_type):
            errors.append(
                {
                    "url": link["url"],
                    "final_url": final_url,
                    "error_code": "linked_page_unsupported_content_type",
                    "message": f"linked page content type is not text/html: {content_type}",
                }
            )
            continue

        content_length = _safe_int((getattr(response, "headers", {}) or {}).get("content-length"))
        if content_length is not None and content_length > LINK_MAX_BYTES:
            errors.append(
                {
                    "url": link["url"],
                    "final_url": final_url,
                    "error_code": "linked_page_too_large",
                    "message": "linked page is larger than the workflow limit",
                }
            )
            continue

        if len((response.text or "").encode("utf-8")) > LINK_MAX_BYTES:
            errors.append(
                {
                    "url": link["url"],
                    "final_url": final_url,
                    "error_code": "linked_page_too_large",
                    "message": "linked page body is larger than the workflow limit",
                }
            )
            continue

        parsed = _parse_html(response.text)
        restriction_fields = _extract_restriction_fields(parsed.text)
        relevant_passages = _extract_restriction_passages(parsed.text)
        page_links = _normalize_page_links(parsed.links, source_url=final_url)
        page = {
            "url": final_url,
            "domain": _domain_from_url(final_url),
            "retrieved_at": retrieved_at,
            "source_link_text": link.get("text"),
            "source_block": link.get("source_block"),
            "link_role": link.get("link_role"),
            "text_excerpt": _truncate(parsed.text, LINK_TEXT_MAX_CHARS),
            "restriction_fields": restriction_fields,
            "relevant_passages": relevant_passages,
            "links": page_links,
        }
        pages.append(page)
        observability.log_event(
            logger,
            logging.INFO,
            "websoc_linked_page_completed",
            workflow_id=workflow_result.get("workflow_id"),
            web_search_url=linked_url,
            final_url=final_url,
            link_role=link.get("link_role"),
            text_excerpt=_log_text(page["text_excerpt"]),
            restriction_fields=restriction_fields,
            relevant_passages=relevant_passages,
            link_count=len(page_links),
            result_urls=[item.get("url") for item in page_links[:10]],
        )

    result = {
        "ok": True,
        "workflow_id": workflow_result.get("workflow_id"),
        "source_url": workflow_result.get("source_url"),
        "selected_count": len(selected),
        "pages": pages,
        "restriction_evidence": [
            {
                "url": page["url"],
                "link_role": page.get("link_role"),
                "fields": page.get("restriction_fields") or {},
                "passages": page.get("relevant_passages") or [],
            }
            for page in pages
        ],
        "errors": errors,
    }
    observability.log_event(
        logger,
        logging.INFO,
        "websoc_linked_search_completed",
        workflow_id=workflow_result.get("workflow_id"),
        source_url=workflow_result.get("source_url"),
        selected_count=len(selected),
        page_count=len(pages),
        error_count=len(errors),
        result_urls=[page.get("url") for page in pages],
    )
    return result


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


def _is_textual_content_type(content_type: str) -> bool:
    lower = content_type.lower()
    return (
        "text/html" in lower
        or "text/plain" in lower
        or "application/xhtml+xml" in lower
    )


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
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
        "authorization_code_notes": _matching_lines(
            text,
            patterns=(
                r"authorization code",
                r"\b[ABX]\s+(?:or\s+[ABX]\s+)?restriction",
                r"\b[ABX]-restricted",
            ),
        ),
        "restriction_update_notes": _matching_lines(
            text,
            patterns=(
                r"major restriction",
                r"new only restriction",
                r"enrollment restriction",
                r"restriction.*(?:removed|lifted|release|timeline|update)",
            ),
        ),
        "contact_emails": sorted(
            set(
                re.findall(
                    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
                    text,
                )
            )
        ),
    }


def _extract_restriction_passages(text: str, *, max_passages: int = 8) -> list[str]:
    return _matching_lines(
        text,
        patterns=(
            r"restriction",
            r"authorization code",
            r"\bNORS?\b",
            r"\bADD(?:ING)?\b",
            r"\bDROP(?:PING)?\b",
            r"grade option",
            r"waitlist",
        ),
        max_items=max_passages,
    )


def _matching_lines(
    text: str,
    *,
    patterns: tuple[str, ...],
    max_items: int = 12,
) -> list[str]:
    matches: list[str] = []
    for line in _clean_text(text).splitlines():
        if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in patterns):
            excerpt = _truncate(line, 700)
            if excerpt not in matches:
                matches.append(excerpt)
        if len(matches) >= max_items:
            break
    return matches


def _normalize_page_links(
    links: list[dict[str, Any]],
    *,
    source_url: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in links:
        absolute_url = urljoin(source_url, link.get("url") or "")
        if absolute_url in seen or not _domain_from_url(absolute_url):
            continue
        seen.add(absolute_url)
        normalized.append(
            {
                "text": link.get("text") or absolute_url,
                "url": absolute_url,
                "domain": _domain_from_url(absolute_url),
                "source_url": source_url,
                "is_uci_official": _is_uci_domain(absolute_url),
            }
        )
    return normalized


def _extract_result_term(text: str) -> Optional[str]:
    match = re.search(
        r"\b(Fall|Winter|Spring|Summer)\s+Quarter,\s*(\d{4})\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return f"{match.group(1).capitalize()} {match.group(2)}"


def _normalize_department(value: Optional[str]) -> str:
    return _collapse_ws(value or "").upper()


def _workflow_error(
    *,
    error_code: str,
    message: str,
    term: str,
    department: str,
    request_form: dict[str, str],
    retrieved_at: str,
    **extra: Any,
) -> dict[str, Any]:
    observability.log_event(
        logger,
        logging.WARNING,
        "websoc_search_rejected",
        workflow_id="websoc_department_restrictions",
        web_search_url=WEBSOC_URL,
        request_method="POST",
        request_form=request_form,
        error_code=error_code,
        message=message,
    )
    return {
        "ok": False,
        "workflow_id": "websoc_department_restrictions",
        "error_code": error_code,
        "message": message,
        "term": term,
        "department": department,
        "source_url": WEBSOC_URL,
        "request_method": "POST",
        "request_form": request_form,
        "retrieved_at": retrieved_at,
        **extra,
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


def _log_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _truncate(value, LOG_TEXT_MAX_CHARS)


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
