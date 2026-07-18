"""Deterministic routing hints for fixed web workflows.

The router decides which high-risk search-like questions should start
from a fixed tool path. It does not answer the user and it does not
block the agent from using normal tools later; it gives the LLM an
explicit, testable instruction before agentic web search is considered.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from app.catalog.departments import DEPARTMENT_ALIASES
from app.catalog.normalization import iter_course_mentions


_AVAILABILITY_PATTERNS = (
    r"\bopen\b",
    r"\bfull\b",
    r"\bwaitl(?:ist)?\b",
    r"\bseat[s]?\b",
    r"\bspot[s]?\b",
    r"\bcapacity\b",
    r"\benrolled\b",
    r"\bstatus\b",
    r"\bNOR\b",
    r"\bNew Only Reserved\b",
    r"\brestriction code[s]?\b",
    r"空位",
    r"位置",
    r"剩",
    r"waitlist",
    r"候补",
    r"满",
    r"现在.*能不能选",
)

_DEPARTMENT_RESTRICTION_PATTERNS = (
    r"\bmajor restriction[s]?\b",
    r"\bNew Only Restriction[s]?\b",
    r"\bNORS\b",
    r"\bnon[- ]?major\b",
    r"\badd/drop/change\b",
    r"\badd\b.*\bdrop\b.*\bchange\b",
    r"专业限制",
    r"major.*限制",
    r"限制.*解除",
    r"什么时候.*解除",
    r"不是.*major",
    r"非.*major",
    r"什么时候.*能选",
    r"加课.*截止",
    r"退课.*截止",
    r"改.*grade",
)


def route_search_workflows(user_text: str, *, term: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Return a fixed-workflow route hint for WebSoc-sensitive questions."""

    text = user_text or ""
    intents: list[str] = []
    if _matches_any(_AVAILABILITY_PATTERNS, text):
        intents.append("availability")
    if _matches_any(_DEPARTMENT_RESTRICTION_PATTERNS, text):
        intents.append("department_restriction")

    if not intents:
        return None

    courses = [
        ref.display()
        for ref, _start, _end in iter_course_mentions(text)
    ]
    departments = _extract_departments(text)
    tools: list[str] = []
    if "availability" in intents:
        tools.append("get_live_sections")
    if "department_restriction" in intents:
        tools.append("get_department_restrictions")

    return {
        "mode": "fixed_workflow_first",
        "intents": intents,
        "recommended_tools": tools,
        "term": term,
        "course_ids": list(dict.fromkeys(courses)),
        "departments": departments,
        "no_agentic_web_search_first": True,
    }


def build_route_hint_message(route: Optional[dict[str, Any]]) -> Optional[dict[str, str]]:
    if not route:
        return None
    return {
        "role": "system",
        "content": (
            "Workflow route hint: this user request matched fixed WebSoc "
            f"workflow intent(s) {route['intents']}. Start with tool(s) "
            f"{route['recommended_tools']} for term {route.get('term') or 'selected term'} "
            "before using general web_search. If both availability and "
            "department_restriction are present, call get_live_sections first, "
            "then get_department_restrictions, and keep their sources separate. "
            f"Detected courses: {route.get('course_ids') or []}. "
            f"Detected departments: {route.get('departments') or []}."
        ),
    }


def _matches_any(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _extract_departments(text: str) -> list[str]:
    found: list[str] = []
    for canonical, aliases in DEPARTMENT_ALIASES.items():
        for token in (canonical, *aliases):
            if _contains_token(text, token):
                found.append(canonical)
                break
    return list(dict.fromkeys(found))


def _contains_token(text: str, token: str) -> bool:
    if not token:
        return False
    escaped = re.escape(token)
    return re.search(
        rf"(?<![A-Za-z0-9_&/]){escaped}(?![A-Za-z0-9_&/])",
        text,
        flags=re.IGNORECASE,
    ) is not None
