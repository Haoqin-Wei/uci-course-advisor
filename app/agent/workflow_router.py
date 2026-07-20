"""Developer-authored routing registry for fixed workflows.

Only entries declared in ``WORKFLOW_REGISTRY`` may route a request to a
workflow. Everything else is explicitly agentic. Search tools never decide
the route; both routes may use them after the workflow's primary tools run.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Optional

from app.catalog.departments import DEPARTMENT_ALIASES
from app.catalog.normalization import iter_course_mentions


@dataclass(frozen=True)
class WorkflowRule:
    """One manually maintained workflow-routing rule."""

    workflow_id: str
    intent: str
    tools: tuple[str, ...]
    patterns: tuple[str, ...]
    primary_source: str

    def matches(self, text: str) -> bool:
        return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in self.patterns)


WORKFLOW_REGISTRY: tuple[WorkflowRule, ...] = (
    WorkflowRule(
        workflow_id="websoc_live_availability",
        intent="availability",
        tools=("get_live_sections",),
        primary_source="live_anteater_websoc",
        patterns=(
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
        ),
    ),
    WorkflowRule(
        workflow_id="websoc_department_restrictions",
        intent="department_restriction",
        tools=("get_department_restrictions",),
        primary_source="registrar_websoc_department_comments",
        patterns=(
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
        ),
    ),
)


def route_solution(user_text: str, *, term: Optional[str] = None) -> dict[str, Any]:
    """Select a developer-declared workflow or the agentic route."""

    text = user_text or ""
    matches = [rule for rule in WORKFLOW_REGISTRY if rule.matches(text)]
    if not matches:
        return {
            "route_type": "agentic",
            "mode": "agentic",
            "workflow_ids": [],
            "intents": [],
            "recommended_tools": [],
            "term": term,
        }

    courses = [ref.display() for ref, _start, _end in iter_course_mentions(text)]
    tools = [tool for rule in matches for tool in rule.tools]
    return {
        "route_type": "workflow",
        "mode": "developer_workflow",
        "workflow_ids": [rule.workflow_id for rule in matches],
        "intents": [rule.intent for rule in matches],
        "recommended_tools": list(dict.fromkeys(tools)),
        "primary_sources": [rule.primary_source for rule in matches],
        "term": term,
        "course_ids": list(dict.fromkeys(courses)),
        "departments": _extract_departments(text),
        "search_tools_are_supplemental": True,
        "source_conflict_policy": "present_both",
    }


def route_search_workflows(user_text: str, *, term: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Compatibility wrapper returning only developer workflow matches."""

    route = route_solution(user_text, term=term)
    return route if route["route_type"] == "workflow" else None


def build_route_hint_message(route: Optional[dict[str, Any]]) -> Optional[dict[str, str]]:
    if not route or route.get("route_type") != "workflow":
        return None
    return {
        "role": "system",
        "content": (
            "Developer workflow registry match: this request must start with "
            f"workflow(s) {route['workflow_ids']} and primary tool(s) "
            f"{route['recommended_tools']} for term {route.get('term') or 'selected term'}. "
            "web_search and fetch_page are optional supplemental tools, not route "
            "selectors. If both availability and department restriction workflows "
            "match, call get_live_sections first, then get_department_restrictions, "
            "and keep their sources separate. If a workflow primary source conflicts "
            "with a supplemental web source, present both claims and both sources. "
            f"Detected courses: {route.get('course_ids') or []}. "
            f"Detected departments: {route.get('departments') or []}."
        ),
    }


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
