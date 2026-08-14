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
from app.data.restriction_timeline import (
    RestrictionQuery,
    RestrictionType,
    extract_main_content_blocks,
    parse_restriction_timeline,
    select_query_focused_blocks,
)

logger = logging.getLogger(__name__)


WEBSOC_URL = "https://www.reg.uci.edu/perl/WebSoc"
REQUEST_TIMEOUT_S = 12
LINK_TEXT_MAX_CHARS = 1500
LINK_MAX_BYTES = 500_000
SECOND_HOP_MAX_PAGES = 1
SECOND_HOP_ALLOWED_HOSTS = frozenset({"docs.google.com"})
LOG_TEXT_MAX_CHARS = 1000
AGENT_TOOL = "get_department_restrictions"
WORKFLOW_ID = "websoc_department_restrictions"
COURSE_AGENT_TOOL = "get_sections"
COURSE_WORKFLOW_ID = "websoc_course_offering"

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

_IGNORED_HTML_TAGS = {"script", "style", "noscript", "svg"}


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
        "CourseNum": "",
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


def build_websoc_course_params(
    term: str,
    department: str,
    course_number: str,
) -> dict[str, str]:
    params = build_websoc_department_params(term, department)
    normalized_course = (course_number or "").strip().upper()
    if not normalized_course:
        raise ValueError("course_number is required")
    params["CourseNum"] = normalized_course
    params.pop("ShowComments", None)
    return params


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
            fetches=form_result.get("fetches") or [],
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
            fetches=form_result.get("fetches") or [],
        )

    request_started = observability.now()
    observability.log_agent_web_fetch_started(
        logger,
        url=source_url,
        method="POST",
        tool=AGENT_TOOL,
        workflow_id=WORKFLOW_ID,
        term=term,
        department=(department or "").strip().upper(),
        timeout_seconds=REQUEST_TIMEOUT_S,
    )
    try:
        response = http.post(WEBSOC_URL, data=params, timeout=REQUEST_TIMEOUT_S)
        response.raise_for_status()
        duration_ms = observability.elapsed_ms(request_started)
        post_fetch = _fetch_record(
            method="POST",
            url=source_url,
            final_url=getattr(response, "url", None) or source_url,
            source_role="registrar_websoc_results",
            status_code=getattr(response, "status_code", None),
            content_length=len(
                (getattr(response, "text", "") or "").encode("utf-8")
            ),
            duration_ms=duration_ms,
            ok=True,
            depth=0,
        )
        observability.log_agent_web_fetch_completed(
            logger,
            url=source_url,
            final_url=getattr(response, "url", None) or source_url,
            method="POST",
            tool=AGENT_TOOL,
            workflow_id=WORKFLOW_ID,
            status_code=getattr(response, "status_code", None),
            content_type=(getattr(response, "headers", {}) or {}).get("content-type"),
            content_length=len((getattr(response, "text", "") or "").encode("utf-8")),
            duration_ms=duration_ms,
            term=term,
            department=(department or "").strip().upper(),
        )
    except requests.RequestException as e:
        duration_ms = observability.elapsed_ms(request_started)
        post_fetch = _fetch_record(
            method="POST",
            url=source_url,
            final_url=source_url,
            source_role="registrar_websoc_results",
            status_code=None,
            content_length=None,
            duration_ms=duration_ms,
            ok=False,
            depth=0,
            error=f"{type(e).__name__}: {e}",
        )
        observability.log_agent_web_fetch_failed(
            logger,
            url=source_url,
            method="POST",
            tool=AGENT_TOOL,
            workflow_id=WORKFLOW_ID,
            duration_ms=duration_ms,
            error=f"{type(e).__name__}: {e}",
            term=term,
            department=(department or "").strip().upper(),
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
            "fetches": [
                *(form_result.get("fetches") or []),
                post_fetch,
            ],
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
    result["fetches"] = [
        *(form_result.get("fetches") or []),
        post_fetch,
    ]
    observability.log_event(
        logger,
        logging.INFO if result.get("ok") else logging.WARNING,
        "agent_web_extraction_completed",
        workflow_id=result["workflow_id"],
        tool=AGENT_TOOL,
        term=result["term"],
        department=result["department"],
        extraction_status=result.get("extraction_status"),
        error_code=result.get("error_code"),
        comment_block_count=len(result.get("comment_blocks") or []),
        link_count=len(result.get("links") or []),
    )
    return result


def fetch_websoc_course_offering(
    *,
    term: str,
    department: str,
    course_number: str,
    session: Optional[requests.Session] = None,
) -> dict[str, Any]:
    """Fetch one course from the Registrar's fixed WebSoc POST workflow.

    This is the authoritative historical offering path. It deliberately
    distinguishes an explicit Registrar no-match result from transport,
    identity-validation, or parsing failures.
    """

    params = build_websoc_course_params(term, department, course_number)
    source_url = WEBSOC_URL
    http = session or requests.Session()
    retrieved_at = _utc_now()
    form_result = fetch_websoc_form_options(
        session=http,
        agent_tool=COURSE_AGENT_TOOL,
        workflow_id=COURSE_WORKFLOW_ID,
    )
    base = {
        "workflow_id": COURSE_WORKFLOW_ID,
        "term": term,
        "department": params["Dept"],
        "course_number": params["CourseNum"],
        "source": "registrar_websoc",
        "source_url": source_url,
        "request_method": "POST",
        "request_form": params,
        "retrieved_at": retrieved_at,
    }
    if not form_result.get("ok"):
        return {
            **form_result,
            **base,
            "ok": False,
            "found": False,
            "offering_status": "unavailable",
        }

    requested_term_code = params["YearTerm"]
    requested_department = params["Dept"]
    if requested_term_code not in form_result["terms"]:
        return _course_workflow_error(
            base,
            error_code="websoc_term_unavailable",
            message=f"term {term!r} is not available in the current WebSoc form",
            fetches=form_result.get("fetches") or [],
            available_terms=form_result["terms"],
        )
    if requested_department not in form_result["departments"]:
        return _course_workflow_error(
            base,
            error_code="websoc_department_unavailable",
            message=(
                f"department {requested_department!r} is not available in the current "
                "WebSoc form"
            ),
            fetches=form_result.get("fetches") or [],
            available_departments=form_result["departments"],
        )

    request_started = observability.now()
    observability.log_agent_web_fetch_started(
        logger,
        url=source_url,
        method="POST",
        tool=COURSE_AGENT_TOOL,
        workflow_id=COURSE_WORKFLOW_ID,
        term=term,
        department=requested_department,
        course_number=params["CourseNum"],
        timeout_seconds=REQUEST_TIMEOUT_S,
    )
    try:
        response = http.post(WEBSOC_URL, data=params, timeout=REQUEST_TIMEOUT_S)
        response.raise_for_status()
        duration_ms = observability.elapsed_ms(request_started)
        post_fetch = _fetch_record(
            method="POST",
            url=source_url,
            final_url=getattr(response, "url", None) or source_url,
            source_role="registrar_websoc_course_results",
            status_code=getattr(response, "status_code", None),
            content_length=len((getattr(response, "text", "") or "").encode("utf-8")),
            duration_ms=duration_ms,
            ok=True,
            depth=0,
        )
        observability.log_agent_web_fetch_completed(
            logger,
            url=source_url,
            final_url=getattr(response, "url", None) or source_url,
            method="POST",
            tool=COURSE_AGENT_TOOL,
            workflow_id=COURSE_WORKFLOW_ID,
            status_code=getattr(response, "status_code", None),
            content_type=(getattr(response, "headers", {}) or {}).get("content-type"),
            content_length=len((getattr(response, "text", "") or "").encode("utf-8")),
            duration_ms=duration_ms,
            term=term,
            department=requested_department,
            course_number=params["CourseNum"],
        )
    except requests.RequestException as exc:
        duration_ms = observability.elapsed_ms(request_started)
        post_fetch = _fetch_record(
            method="POST",
            url=source_url,
            final_url=source_url,
            source_role="registrar_websoc_course_results",
            status_code=None,
            content_length=None,
            duration_ms=duration_ms,
            ok=False,
            depth=0,
            error=f"{type(exc).__name__}: {exc}",
        )
        observability.log_agent_web_fetch_failed(
            logger,
            url=source_url,
            method="POST",
            tool=COURSE_AGENT_TOOL,
            workflow_id=COURSE_WORKFLOW_ID,
            duration_ms=duration_ms,
            error=f"{type(exc).__name__}: {exc}",
            term=term,
            department=requested_department,
            course_number=params["CourseNum"],
        )
        return _course_workflow_error(
            base,
            error_code="websoc_request_failed",
            message=f"Registrar WebSoc request failed: {type(exc).__name__}",
            fetches=[*(form_result.get("fetches") or []), post_fetch],
        )

    result = parse_websoc_course_html(
        response.text,
        term=term,
        department=requested_department,
        course_number=params["CourseNum"],
        source_url=source_url,
        retrieved_at=retrieved_at,
    )
    result["request_method"] = "POST"
    result["request_form"] = params
    result["fetches"] = [*(form_result.get("fetches") or []), post_fetch]
    observability.log_event(
        logger,
        logging.INFO if result.get("ok") else logging.WARNING,
        "agent_web_extraction_completed",
        workflow_id=COURSE_WORKFLOW_ID,
        tool=COURSE_AGENT_TOOL,
        term=term,
        department=requested_department,
        course_number=params["CourseNum"],
        offering_status=result.get("offering_status"),
        section_count=len(result.get("sections") or []),
        error_code=result.get("error_code"),
    )
    return result


def fetch_websoc_form_options(
    *,
    session: Optional[requests.Session] = None,
    agent_tool: str = AGENT_TOOL,
    workflow_id: str = WORKFLOW_ID,
) -> dict[str, Any]:
    """Read the live form so submitted term and department values are validated."""

    http = session or requests.Session()
    retrieved_at = _utc_now()
    request_started = observability.now()
    observability.log_agent_web_fetch_started(
        logger,
        url=WEBSOC_URL,
        method="GET",
        tool=agent_tool,
        workflow_id=workflow_id,
        timeout_seconds=REQUEST_TIMEOUT_S,
    )
    try:
        response = http.get(WEBSOC_URL, timeout=REQUEST_TIMEOUT_S)
        response.raise_for_status()
        duration_ms = observability.elapsed_ms(request_started)
        fetch_record = _fetch_record(
            method="GET",
            url=WEBSOC_URL,
            final_url=getattr(response, "url", None) or WEBSOC_URL,
            source_role="registrar_websoc_form",
            status_code=getattr(response, "status_code", None),
            content_length=len(
                (getattr(response, "text", "") or "").encode("utf-8")
            ),
            duration_ms=duration_ms,
            ok=True,
            depth=0,
        )
        observability.log_agent_web_fetch_completed(
            logger,
            url=WEBSOC_URL,
            final_url=getattr(response, "url", None) or WEBSOC_URL,
            method="GET",
            tool=agent_tool,
            workflow_id=workflow_id,
            status_code=getattr(response, "status_code", None),
            content_type=(getattr(response, "headers", {}) or {}).get("content-type"),
            content_length=len((getattr(response, "text", "") or "").encode("utf-8")),
            duration_ms=duration_ms,
        )
    except requests.RequestException as exc:
        duration_ms = observability.elapsed_ms(request_started)
        fetch_record = _fetch_record(
            method="GET",
            url=WEBSOC_URL,
            final_url=WEBSOC_URL,
            source_role="registrar_websoc_form",
            status_code=None,
            content_length=None,
            duration_ms=duration_ms,
            ok=False,
            depth=0,
            error=f"{type(exc).__name__}: {exc}",
        )
        observability.log_agent_web_fetch_failed(
            logger,
            url=WEBSOC_URL,
            method="GET",
            tool=agent_tool,
            workflow_id=workflow_id,
            duration_ms=duration_ms,
            error=f"{type(exc).__name__}: {exc}",
        )
        return {
            "ok": False,
            "error_code": "websoc_form_request_failed",
            "message": f"Registrar WebSoc form request failed: {type(exc).__name__}",
            "source_url": WEBSOC_URL,
            "retrieved_at": retrieved_at,
            "fetches": [fetch_record],
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
            "fetches": [fetch_record],
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
        "fetches": [fetch_record],
    }


def parse_websoc_course_html(
    html: str,
    *,
    term: str,
    department: str,
    course_number: str,
    source_url: str,
    retrieved_at: Optional[str] = None,
) -> dict[str, Any]:
    parsed = _parse_html(html)
    text = parsed.text
    requested_term = Term.parse(term)
    requested_department = _normalize_department(department)
    requested_course = (course_number or "").strip().upper()
    response_term = _extract_result_term(text)
    response_department = _normalize_department(
        _search_text(r"Department:\s*([^\n]+)", text)
    )
    response_course = (
        _search_text(r"Course Number Range:\s*([^\n]+)", text) or ""
    ).strip().upper()
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
    if response_course != requested_course:
        validation_errors.append(
            {
                "error_code": "websoc_course_mismatch",
                "message": (
                    f"WebSoc returned course range {response_course or 'missing'} "
                    f"instead of {requested_course}"
                ),
            }
        )

    course_table = _parse_websoc_course_table(
        html,
        department=requested_department,
        course_number=requested_course,
        retrieved_at=retrieved_at or _utc_now(),
    )
    sections = course_table["sections"]
    explicit_no_match = bool(
        re.search(
            r"No courses matched your search criteria for this term",
            text,
            re.IGNORECASE,
        )
    )
    if explicit_no_match and sections:
        validation_errors.append(
            {
                "error_code": "websoc_conflicting_course_result",
                "message": "WebSoc response contained both sections and a no-match message",
            }
        )
    if not explicit_no_match and not sections:
        validation_errors.append(
            {
                "error_code": "websoc_course_result_missing",
                "message": "WebSoc result contained neither course sections nor an explicit no-match",
            }
        )

    validation_ok = not validation_errors
    offering_status = (
        "unavailable"
        if not validation_ok
        else "not_offered"
        if explicit_no_match
        else "offered"
    )
    result: dict[str, Any] = {
        "ok": validation_ok,
        "found": validation_ok and bool(sections),
        "mode": "workflow",
        "workflow_id": COURSE_WORKFLOW_ID,
        "offering_status": offering_status,
        "authoritative": validation_ok,
        "term": term,
        "department": requested_department,
        "course_number": requested_course,
        "course_id": f"{requested_department} {requested_course}",
        "course_title": course_table.get("course_title"),
        "source": "registrar_websoc",
        "source_url": source_url,
        "source_class": "official_uci",
        "trust_level": "high",
        "retrieved_at": retrieved_at or _utc_now(),
        "response_term": response_term,
        "search_criteria": {
            "department": response_department,
            "course_number": response_course,
            "exclude_cancelled_courses": bool(
                re.search(r"Exclude cancelled courses", text, re.IGNORECASE)
            ),
        },
        "sections": sections if validation_ok else [],
        "validation": {"ok": validation_ok, "errors": validation_errors},
        "extraction_status": "complete" if validation_ok else "invalid",
    }
    if offering_status == "not_offered":
        result["reason"] = (
            f"Registrar WebSoc reported no matching sections for "
            f"{requested_department} {requested_course} in "
            f"{requested_term.display() if requested_term else term}"
        )
    if validation_errors:
        result["error_code"] = validation_errors[0]["error_code"]
        result["message"] = "; ".join(error["message"] for error in validation_errors)
    return result


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


class _WebSocCourseTableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, Any]]] = []
        self._row: Optional[list[dict[str, Any]]] = None
        self._cell: Optional[dict[str, Any]] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr = {key.lower(): value or "" for key, value in attrs}
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = {"class": attr.get("class", ""), "parts": []}
        elif tag == "br" and self._cell is not None:
            self._cell["parts"].append("\n")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None


def _parse_websoc_course_table(
    html: str,
    *,
    department: str,
    course_number: str,
    retrieved_at: str,
) -> dict[str, Any]:
    parser = _WebSocCourseTableHTMLParser()
    parser.feed(html or "")
    parser.close()
    course_title: Optional[str] = None
    sections: list[dict[str, Any]] = []
    for row in parser.rows:
        cells = [_websoc_cell_text(cell) for cell in row]
        for index, cell in enumerate(row):
            if "coursetitle" not in str(cell.get("class") or "").lower():
                continue
            title_text = cells[index]
            title_match = re.match(
                rf"^\s*{re.escape(department)}\s+{re.escape(course_number)}\s+"
                r"(.+?)(?:\s*\(Prerequisites\))?\s*$",
                title_text,
                flags=re.IGNORECASE,
            )
            if title_match:
                course_title = _collapse_ws(title_match.group(1))

        if len(cells) < 15 or not re.fullmatch(r"\d{5}", cells[0]):
            continue
        max_capacity = _websoc_int(cells[8])
        enrolled = _websoc_int(cells[9])
        seats_open = (
            max(0, max_capacity - enrolled)
            if max_capacity is not None and enrolled is not None
            else None
        )
        days, start_time, end_time, time_display = _split_websoc_time(cells[6])
        status = _collapse_ws(cells[14]).upper() or None
        sections.append(
            {
                "section_code": cells[0],
                "section_num": cells[2] or None,
                "section_type": cells[1] or None,
                "units": cells[3] or None,
                "days": days,
                "start_time": start_time,
                "end_time": end_time,
                "time_display": time_display,
                "location": cells[7] or None,
                "instructors": _websoc_cell_lines(row[4]),
                "modality": cells[5] or None,
                "max_capacity": max_capacity,
                "enrolled": enrolled,
                "seats_open": seats_open,
                "waitlisted": None,
                "requests": _websoc_int(cells[10]),
                "status": status,
                "is_cancelled": status in {"CANCELLED", "CANCELED"},
                "ge_categories": [],
                "restrictions": cells[11] or None,
                "final_exam": None,
                "source": "registrar_websoc",
                "is_live": False,
                "retrieved_at": retrieved_at,
            }
        )
    return {"course_title": course_title, "sections": sections}


def _websoc_cell_text(cell: dict[str, Any]) -> str:
    return _collapse_ws(" ".join(str(part) for part in cell.get("parts") or []))


def _websoc_cell_lines(cell: dict[str, Any]) -> list[str]:
    raw = "".join(str(part) for part in cell.get("parts") or [])
    return [line for line in (_collapse_ws(part) for part in raw.splitlines()) if line]


def _websoc_int(value: str) -> Optional[int]:
    try:
        return int((value or "").strip())
    except (TypeError, ValueError):
        return None


def _split_websoc_time(
    value: str,
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    display = _collapse_ws(value)
    if not display or display.upper() in {"TBA", "ARR"}:
        return None, None, None, display or None
    match = re.match(
        r"^(?P<days>.*?)\s+(?P<start>\d{1,2}:\d{2}[ap]?)\s*-\s*"
        r"(?P<end>\d{1,2}:\d{2}[ap]?)$",
        display,
        flags=re.IGNORECASE,
    )
    if not match:
        return None, None, None, display
    return (
        _collapse_ws(match.group("days")) or None,
        match.group("start"),
        match.group("end"),
        display,
    )


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
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag in _IGNORED_HTML_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        attr = {k.lower(): v or "" for k, v in attrs}
        if tag == "a":
            self._active_href = attr.get("href")
            self._active_link_parts = []
        if tag in {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "table"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._active_href is not None:
            self._active_link_parts.append(data)
        self.text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in _IGNORED_HTML_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
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
        table_header = re.search(
            r"\nCode\nType\nSec\nUnits\nInstructor(?:\n|$)",
            text[match.end():end],
            flags=re.IGNORECASE,
        )
        if table_header:
            end = match.end() + table_header.start()
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
        if assigned is None:
            continue
        identity = _url_identity(absolute_url)
        if identity is None:
            continue
        out.append(
            {
                "text": link.get("text") or absolute_url,
                "url": absolute_url,
                "domain": _domain_from_url(absolute_url),
                "link_role": _classify_link_role(absolute_url, link.get("text") or ""),
                "source_block": assigned["title"],
                "source_block_type": assigned["block_type"],
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

    page_limit = max(0, max_pages)
    query = _restriction_query_from_workflow(workflow_result)
    selected = _select_first_hop_links(
        workflow_result,
        query=query,
        max_pages=page_limit,
    )
    observability.log_event(
        logger,
        logging.INFO,
        "restriction_page_selected",
        workflow_id=workflow_result.get("workflow_id"),
        source_url=workflow_result.get("source_url"),
        restriction_type=query.restriction_type.value,
        selected_count=len(selected),
        result_urls=[link.get("url") for link in selected],
    )
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
    attempted_urls: list[str] = []
    seen_requested: set[tuple[str, str, str]] = set()
    for link in selected:
        identity = _url_identity(link.get("url") or "")
        if identity is None or identity in seen_requested:
            continue
        seen_requested.add(identity)
        attempted_urls.append(link["url"])
        page, error = _fetch_and_parse_linked_page(
            http,
            link,
            workflow_result=workflow_result,
            query=query,
            allowed_url=_is_uci_domain,
            depth=1,
        )
        if error:
            errors.append(error)
            continue
        if page is not None:
            pages.append(page)
            if _page_satisfies_query(page, query):
                break

    first_hop_pages = list(pages)
    if (
        query.restriction_type == RestrictionType.COURSE_SPECIFIC
        and not any(_page_satisfies_query(page, query) for page in pages)
        and len(pages) < page_limit
    ):
        second_hop_budget = min(
            SECOND_HOP_MAX_PAGES,
            page_limit - len(pages),
        )
        for parent_page in first_hop_pages:
            for link in _select_second_hop_links(
                parent_page,
                max_pages=second_hop_budget,
            ):
                identity = _url_identity(link.get("url") or "")
                if identity is None or identity in seen_requested:
                    continue
                seen_requested.add(identity)
                attempted_urls.append(link["url"])
                page, error = _fetch_and_parse_linked_page(
                    http,
                    link,
                    workflow_result=workflow_result,
                    query=query,
                    allowed_url=_is_allowed_second_hop_url,
                    depth=2,
                    parent_url=parent_page["url"],
                )
                if error:
                    errors.append(error)
                    continue
                if page is not None:
                    pages.append(page)
                    second_hop_budget -= 1
                if second_hop_budget <= 0 or (
                    page is not None and _page_satisfies_query(page, query)
                ):
                    break
            if second_hop_budget <= 0 or any(
                _page_satisfies_query(page, query) for page in pages
            ):
                break

    result = {
        "ok": True,
        "workflow_id": workflow_result.get("workflow_id"),
        "source_url": workflow_result.get("source_url"),
        "selected_count": len(attempted_urls),
        "fetched_urls": attempted_urls,
        "pages": pages,
        "fetches": [
            fetch
            for fetch in (
                [
                    page.get("fetch")
                    for page in pages
                    if page.get("fetch")
                ]
                + [
                    error.get("fetch")
                    for error in errors
                    if error.get("fetch")
                ]
            )
            if fetch
        ],
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
        selected_count=len(attempted_urls),
        page_count=len(pages),
        error_count=len(errors),
        result_urls=[page.get("url") for page in pages],
    )
    return result


def _fetch_and_parse_linked_page(
    http: requests.Session,
    link: dict[str, Any],
    *,
    workflow_result: dict[str, Any],
    query: RestrictionQuery,
    allowed_url,
    depth: int,
    parent_url: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    retrieved_at = _utc_now()
    linked_url = link["url"]
    request_started = observability.now()
    observability.log_agent_web_fetch_started(
        logger,
        url=linked_url,
        method="GET",
        tool=AGENT_TOOL,
        workflow_id=workflow_result.get("workflow_id") or WORKFLOW_ID,
        link_role=link.get("link_role"),
        depth=depth,
    )
    try:
        response = http.get(linked_url, timeout=REQUEST_TIMEOUT_S)
        response.raise_for_status()
        duration_ms = observability.elapsed_ms(request_started)
        request_fetch = _fetch_record(
            method="GET",
            url=linked_url,
            final_url=getattr(response, "url", None) or linked_url,
            source_role=link.get("link_role") or "official_link",
            status_code=getattr(response, "status_code", None),
            content_length=len(
                (getattr(response, "text", "") or "").encode("utf-8")
            ),
            duration_ms=duration_ms,
            ok=True,
            depth=depth,
            parent_url=parent_url,
        )
        observability.log_agent_web_fetch_completed(
            logger,
            url=linked_url,
            final_url=getattr(response, "url", None) or linked_url,
            method="GET",
            tool=AGENT_TOOL,
            workflow_id=workflow_result.get("workflow_id") or WORKFLOW_ID,
            status_code=getattr(response, "status_code", None),
            content_type=(getattr(response, "headers", {}) or {}).get("content-type"),
            content_length=len((getattr(response, "text", "") or "").encode("utf-8")),
            duration_ms=duration_ms,
            link_role=link.get("link_role"),
            depth=depth,
        )
    except requests.RequestException as e:
        duration_ms = observability.elapsed_ms(request_started)
        request_fetch = _fetch_record(
            method="GET",
            url=linked_url,
            final_url=linked_url,
            source_role=link.get("link_role") or "official_link",
            status_code=None,
            content_length=None,
            duration_ms=duration_ms,
            ok=False,
            depth=depth,
            parent_url=parent_url,
            error=f"{type(e).__name__}: {e}",
        )
        observability.log_agent_web_fetch_failed(
            logger,
            url=linked_url,
            method="GET",
            tool=AGENT_TOOL,
            workflow_id=workflow_result.get("workflow_id") or WORKFLOW_ID,
            duration_ms=duration_ms,
            error=f"{type(e).__name__}: {e}",
            link_role=link.get("link_role"),
            depth=depth,
        )
        return None, {
            "url": linked_url,
            "depth": depth,
            "error_code": "linked_page_request_failed",
            "message": f"official linked page request failed: {type(e).__name__}",
            "fetch": request_fetch,
        }

    final_url = getattr(response, "url", linked_url) or linked_url
    if not allowed_url(final_url):
        return None, {
            "url": linked_url,
            "final_url": final_url,
            "depth": depth,
            "error_code": "linked_page_left_allowed_domain",
            "message": "linked page redirected outside its allowed domain",
            "fetch": request_fetch,
        }

    content_type = (getattr(response, "headers", {}) or {}).get("content-type", "")
    if content_type and not _is_textual_content_type(content_type):
        return None, {
            "url": linked_url,
            "final_url": final_url,
            "depth": depth,
            "error_code": "linked_page_unsupported_content_type",
            "message": f"linked page content type is not text/html: {content_type}",
            "fetch": request_fetch,
        }

    content_length = _safe_int(
        (getattr(response, "headers", {}) or {}).get("content-length")
    )
    response_text = getattr(response, "text", "") or ""
    if (
        content_length is not None
        and content_length > LINK_MAX_BYTES
        or len(response_text.encode("utf-8")) > LINK_MAX_BYTES
    ):
        return None, {
            "url": linked_url,
            "final_url": final_url,
            "depth": depth,
            "error_code": "linked_page_too_large",
            "message": "linked page body is larger than the workflow limit",
            "fetch": request_fetch,
        }

    parsed = _parse_html(response_text)
    content_blocks = extract_main_content_blocks(response_text)
    focused_blocks = select_query_focused_blocks(content_blocks, query)
    timeline_events = parse_restriction_timeline(
        content_blocks,
        term=query.term,
        department=query.department,
        source_url=final_url,
        source_role=link.get("link_role") or "official_link",
        retrieved_at=retrieved_at,
    )
    restriction_fields = _extract_restriction_fields(parsed.text)
    _fill_fields_from_timeline(restriction_fields, timeline_events)
    relevant_passages = [
        block["text"] for block in focused_blocks[:12]
    ] or _extract_restriction_passages(parsed.text)
    excerpt_blocks = focused_blocks or content_blocks
    page_links = _normalize_page_links(parsed.links, source_url=final_url)
    page = {
        "url": final_url,
        "domain": _domain_from_url(final_url),
        "retrieved_at": retrieved_at,
        "source_link_text": link.get("text"),
        "source_block": link.get("source_block"),
        "source_blocks": link.get("source_blocks") or [],
        "link_role": link.get("link_role"),
        "depth": depth,
        "parent_url": parent_url,
        "fetch": request_fetch,
        "text_excerpt": _truncate(
            "\n".join(block["text"] for block in excerpt_blocks),
            LINK_TEXT_MAX_CHARS,
        ),
        "content_block_count": len(content_blocks),
        "selected_block_count": len(focused_blocks),
        "restriction_fields": restriction_fields,
        "relevant_passages": relevant_passages,
        "timeline_events": [event.to_dict() for event in timeline_events],
        "links": page_links,
    }
    observability.log_event(
        logger,
        logging.INFO,
        "restriction_timeline_extracted",
        workflow_id=workflow_result.get("workflow_id"),
        tool=AGENT_TOOL,
        url=final_url,
        link_role=link.get("link_role"),
        passage_count=len(relevant_passages),
        link_count=len(page_links),
        timeline_event_count=len(timeline_events),
        depth=depth,
    )
    observability.log_event(
        logger,
        logging.INFO,
        "agent_web_extraction_completed",
        workflow_id=workflow_result.get("workflow_id"),
        tool=AGENT_TOOL,
        url=final_url,
        link_role=link.get("link_role"),
        passage_count=len(relevant_passages),
        link_count=len(page_links),
        timeline_event_count=len(timeline_events),
        depth=depth,
    )
    return page, None


def _restriction_query_from_workflow(
    workflow_result: dict[str, Any],
) -> RestrictionQuery:
    raw_restriction_type = workflow_result.get("restriction_type") or "ambiguous"
    try:
        restriction_type = RestrictionType(raw_restriction_type)
    except ValueError:
        restriction_type = RestrictionType.AMBIGUOUS
    return RestrictionQuery(
        term=str(workflow_result.get("term") or ""),
        department=str(workflow_result.get("department") or ""),
        course_id=workflow_result.get("course_id"),
        restriction_type=restriction_type,
        student_major=workflow_result.get("student_major"),
        student_school=workflow_result.get("student_school"),
        academic_level=str(
            workflow_result.get("academic_level") or "undergraduate"
        ),
    )


def _select_first_hop_links(
    workflow_result: dict[str, Any],
    *,
    query: RestrictionQuery,
    max_pages: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
    preferred_roles = _preferred_first_hop_roles(query)
    for link in workflow_result.get("links", []):
        role = link.get("link_role")
        if role and role.startswith("ics_") and role not in preferred_roles:
            continue
        if not _should_deep_read_link(link, workflow_result):
            continue
        identity = _url_identity(link.get("url") or "")
        if identity is None:
            continue
        source_block = link.get("source_block")
        existing = selected_by_identity.get(identity)
        if existing is not None:
            if (
                source_block
                and source_block not in existing["source_blocks"]
            ):
                existing["source_blocks"].append(source_block)
            continue
        if len(selected) >= max_pages:
            continue
        item = {
            **link,
            "source_blocks": [source_block] if source_block else [],
        }
        selected_by_identity[identity] = item
        selected.append(item)
    return selected


def _preferred_first_hop_roles(query: RestrictionQuery) -> set[str]:
    if query.academic_level.lower().startswith("grad"):
        return {"ics_graduate_course_updates"}
    if query.restriction_type == RestrictionType.ADD_DROP_CHANGE:
        return {"ics_undergraduate_student_policies"}
    return {"ics_undergraduate_restrictions"}


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


def _select_second_hop_links(
    parent_page: dict[str, Any],
    *,
    max_pages: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for link in parent_page.get("links", []):
        if len(selected) >= max_pages:
            break
        if not link.get("allowed_for_second_hop"):
            continue
        identity = _url_identity(link.get("url") or "")
        if identity is None or identity in seen:
            continue
        seen.add(identity)
        selected.append(link)
    return selected


def _page_satisfies_query(
    page: dict[str, Any],
    query: RestrictionQuery,
) -> bool:
    events = page.get("timeline_events") or []
    if query.restriction_type == RestrictionType.AMBIGUOUS:
        return bool(events or page.get("relevant_passages"))
    if query.restriction_type == RestrictionType.COURSE_SPECIFIC:
        target = _normalize_course_id(query.course_id)
        if not target:
            return False
        for event in events:
            values = [
                *(event.get("course_scope") or []),
                event.get("statement") or "",
            ]
            for exception in event.get("exceptions") or []:
                values.extend(
                    [
                        exception.get("course_id") or "",
                        *(exception.get("course_ids") or []),
                        exception.get("text") or "",
                    ]
                )
            if any(target in _normalize_course_id(value) for value in values):
                return True
        return False
    return any(
        event.get("restriction_type") == query.restriction_type.value
        and event.get("effective_at")
        for event in events
    )


def _normalize_course_id(value: Optional[str]) -> str:
    upper = (value or "").upper()
    upper = re.sub(r"I\s*&\s*C\s*SCI", "ICS", upper)
    return re.sub(r"[^A-Z0-9]", "", upper)


def _is_allowed_second_hop_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in SECOND_HOP_ALLOWED_HOSTS


def _fill_fields_from_timeline(
    fields: dict[str, Any],
    events: list,
) -> None:
    """Backfill legacy flat fields from structured events for compatibility."""

    for event in events:
        if not event.effective_at or event.action != "removed":
            continue
        if (
            event.restriction_type == RestrictionType.SCHOOL_MAJOR
            and not fields.get("major_restriction_removed_at")
        ):
            fields["major_restriction_removed_at"] = event.effective_at
        elif (
            event.restriction_type == RestrictionType.NEW_ONLY
            and not fields.get("nors_removed_at")
        ):
            fields["nors_removed_at"] = event.effective_at


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


def _fetch_record(
    *,
    method: str,
    url: str,
    final_url: str,
    source_role: str,
    status_code: Optional[int],
    content_length: Optional[int],
    duration_ms: float,
    ok: bool,
    depth: int,
    parent_url: Optional[str] = None,
    error: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "method": method,
        "url": url,
        "final_url": final_url,
        "host": _domain_from_url(final_url or url),
        "source_role": source_role,
        "status_code": status_code,
        "ok": ok,
        "bytes": content_length,
        "duration_ms": round(float(duration_ms), 2),
        "depth": depth,
        "parent_url": parent_url,
        "error": error,
        "provides_evidence": False,
    }


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
            r"Major restrictions[^\n]*?removed on\s+([^\n.]+)",
            text,
        ),
        "nors_removed_at": _search_text(
            r"New Only Restrictions\s*\(NORS?\)[^\n]*?removed on\s+([^\n.]+)",
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
    lines = _clean_text(text).splitlines()
    for index, line in enumerate(lines):
        if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in patterns):
            excerpt = line
            if re.search(r"\b(?:on|at|by|until)\s*:?[\s]*$", line, re.IGNORECASE):
                if index + 1 < len(lines):
                    excerpt = f"{line} {lines[index + 1]}"
            excerpt = _truncate(excerpt, 700)
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
        text = link.get("text") or absolute_url
        second_hop_role = _classify_second_hop_role(absolute_url, text)
        normalized.append(
            {
                "text": text,
                "url": absolute_url,
                "domain": _domain_from_url(absolute_url),
                "source_url": source_url,
                "is_uci_official": _is_uci_domain(absolute_url),
                "link_role": second_hop_role,
                "allowed_for_second_hop": bool(
                    second_hop_role and _is_allowed_second_hop_url(absolute_url)
                ),
            }
        )
    return normalized


def _classify_second_hop_role(url: str, text: str) -> Optional[str]:
    value = f"{text} {url}".lower()
    if re.search(
        r"restriction.{0,24}(?:spreadsheet|sheet|timeline|table)"
        r"|(?:spreadsheet|sheet|timeline|table).{0,24}restriction"
        r"|docs\.google\.com/spreadsheets",
        value,
    ):
        return "restriction_spreadsheet"
    return None


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


def _course_workflow_error(
    base: dict[str, Any],
    *,
    error_code: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    observability.log_event(
        logger,
        logging.WARNING,
        "websoc_search_rejected",
        workflow_id=COURSE_WORKFLOW_ID,
        web_search_url=WEBSOC_URL,
        request_method="POST",
        request_form=base.get("request_form"),
        error_code=error_code,
        message=message,
    )
    return {
        **base,
        "ok": False,
        "found": False,
        "offering_status": "unavailable",
        "authoritative": False,
        "error_code": error_code,
        "message": message,
        "sections": [],
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


def _summarize_request_form(form: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "Submit",
        "YearTerm",
        "Dept",
        "ShowComments",
        "ShowFinals",
        "CancelledCourses",
    )
    return {key: form.get(key) for key in keys if form.get(key) not in (None, "")}


def _summarize_restriction_fields(fields: Optional[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in (fields or {}).items():
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            summary[key] = [_truncate(str(item), 240) for item in value[:3]]
            if len(value) > 3:
                summary[f"{key}_remaining"] = len(value) - 3
        else:
            summary[key] = value
    return summary


def _url_identity(url: str) -> Optional[tuple[str, str, str]]:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    path = parsed.path.rstrip("/") or "/"
    return parsed.hostname.lower(), path, parsed.query


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
