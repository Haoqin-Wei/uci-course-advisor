"""Structured restriction queries and evidence.

The restriction workflow owns factual retrieval and extraction.  The LLM may
explain the resulting bundle, but it must not manufacture missing dates,
audiences, or exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from html.parser import HTMLParser
import re
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.catalog.normalization import iter_course_mentions


class RestrictionType(str, Enum):
    SCHOOL_MAJOR = "school_major"
    NEW_ONLY = "new_only"
    CLASS_LEVEL = "class_level"
    REPEAT = "repeat"
    AUTHORIZATION_CODE = "authorization_code"
    COURSE_SPECIFIC = "course_specific"
    ADD_DROP_CHANGE = "add_drop_change"
    AMBIGUOUS = "ambiguous"


class EvidenceStatus(str, Enum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    CONFLICTING = "conflicting"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RestrictionQuery:
    term: str
    department: str
    restriction_type: RestrictionType
    course_id: Optional[str] = None
    student_major: Optional[str] = None
    student_school: Optional[str] = None
    academic_level: str = "undergraduate"

    def to_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "department": self.department,
            "course_id": self.course_id,
            "restriction_type": self.restriction_type.value,
            "student_major": self.student_major,
            "student_school": self.student_school,
            "academic_level": self.academic_level,
        }


@dataclass(frozen=True)
class RestrictionEvent:
    event_id: str
    restriction_type: RestrictionType
    action: str
    effective_at: Optional[str]
    term: str
    department: str
    course_scope: tuple[str, ...] = ()
    audience: tuple[str, ...] = ()
    exceptions: tuple[dict[str, Any], ...] = ()
    source_url: str = ""
    source_role: str = ""
    retrieved_at: Optional[str] = None
    source_position: Optional[dict[str, Any]] = None
    statement: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "restriction_type": self.restriction_type.value,
            "action": self.action,
            "effective_at": self.effective_at,
            "term": self.term,
            "department": self.department,
            "course_scope": list(self.course_scope),
            "audience": list(self.audience),
            "exceptions": [dict(item) for item in self.exceptions],
            "source_url": self.source_url,
            "source_role": self.source_role,
            "retrieved_at": self.retrieved_at,
            "source_position": self.source_position,
            "statement": self.statement,
        }


@dataclass(frozen=True)
class RestrictionEligibility:
    eligible: Optional[bool]
    student_major: Optional[str]
    student_school: Optional[str]
    allowed_groups: tuple[str, ...] = ()
    reason: str = ""
    evidence_event_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "student_major": self.student_major,
            "student_school": self.student_school,
            "allowed_groups": list(self.allowed_groups),
            "reason": self.reason,
            "evidence_event_id": self.evidence_event_id,
        }


@dataclass
class RestrictionEvidenceBundle:
    query: RestrictionQuery
    events: list[RestrictionEvent] = field(default_factory=list)
    primary_event_id: Optional[str] = None
    related_event_ids: list[str] = field(default_factory=list)
    eligibility: Optional[RestrictionEligibility] = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    evidence_status: EvidenceStatus = EvidenceStatus.PARTIAL
    missing_required_fields: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def primary_event(self) -> Optional[RestrictionEvent]:
        return next(
            (
                event
                for event in self.events
                if event.event_id == self.primary_event_id
            ),
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query.to_dict(),
            "events": [event.to_dict() for event in self.events],
            "primary_event_id": self.primary_event_id,
            "related_event_ids": list(self.related_event_ids),
            "eligibility": (
                self.eligibility.to_dict() if self.eligibility is not None else None
            ),
            "sources": [dict(source) for source in self.sources],
            "evidence_status": self.evidence_status.value,
            "missing_required_fields": list(self.missing_required_fields),
            "conflicts": [dict(conflict) for conflict in self.conflicts],
        }


def classify_restriction_type(
    text: str,
    *,
    has_course: bool = False,
) -> RestrictionType:
    """Classify the restriction fact the user is asking for."""

    value = text or ""
    if re.search(r"\b(?:new only|nors?|new only restriction)s?\b|新生(?:预留|限制)", value, re.I):
        return RestrictionType.NEW_ONLY
    if re.search(r"专业限制|院系限制|学校限制|\b(?:school|major|department) restrictions?\b", value, re.I):
        return RestrictionType.SCHOOL_MAJOR
    if re.search(r"年级限制|upper[- ]division|class[- ]level|standing", value, re.I):
        return RestrictionType.CLASS_LEVEL
    if re.search(r"重修限制|\brepeat restrictions?\b", value, re.I):
        return RestrictionType.REPEAT
    if re.search(r"授权码|authorization code|\b[ABX][ -]?restrictions?\b", value, re.I):
        return RestrictionType.AUTHORIZATION_CODE
    if re.search(r"add\s*/?\s*drop|change grade|加课.*截止|退课.*截止|改.*grade", value, re.I):
        return RestrictionType.ADD_DROP_CHANGE
    if has_course and re.search(r"限制|开放|能选|restriction|open|enroll", value, re.I):
        return RestrictionType.COURSE_SPECIFIC
    return RestrictionType.AMBIGUOUS


def build_restriction_evidence_bundle(
    query: RestrictionQuery,
    *,
    websoc_result: dict[str, Any],
    linked_result: Optional[dict[str, Any]] = None,
) -> RestrictionEvidenceBundle:
    """Merge fetched facts and run the required-field evidence gate."""

    linked = linked_result or {}
    events = _events_from_linked_pages(linked.get("pages") or [])
    events.extend(_events_from_legacy_fields(websoc_result, query))
    events = _deduplicate_events(events)

    target_events = _target_events(events, query)
    primary = _select_primary_event(target_events, query)
    related = [
        event
        for event in events
        if (primary is None or event.event_id != primary.event_id)
        and _departments_match(event.department, query.department)
    ]
    missing = _missing_required_fields(query, primary)
    conflicts = _find_conflicts(target_events)
    if conflicts:
        status = EvidenceStatus.CONFLICTING
    elif primary is not None and not missing:
        status = EvidenceStatus.VERIFIED
    elif primary is not None:
        status = EvidenceStatus.PARTIAL
    else:
        status = EvidenceStatus.UNAVAILABLE

    sources = _build_sources(websoc_result, linked, events)
    eligibility = _evaluate_eligibility(query, primary)
    return RestrictionEvidenceBundle(
        query=query,
        events=events,
        primary_event_id=primary.event_id if primary else None,
        related_event_ids=[event.event_id for event in related],
        eligibility=eligibility,
        sources=sources,
        evidence_status=status,
        missing_required_fields=missing,
        conflicts=conflicts,
    )


def build_verified_restriction_facts(
    bundle: RestrictionEvidenceBundle,
) -> dict[str, Any]:
    """Render immutable core facts before any LLM explanation."""

    primary = bundle.primary_event
    related = [
        event
        for event in bundle.events
        if event.event_id in bundle.related_event_ids
    ]
    source_urls = [
        source["url"]
        for source in bundle.sources
        if source.get("url")
    ]
    if primary is None:
        summary = (
            f"未能从已抓取的官方来源验证 "
            f"{_restriction_label(bundle.query.restriction_type)}的具体时间。"
        )
        if bundle.missing_required_fields:
            summary += (
                " 缺少："
                + "、".join(bundle.missing_required_fields)
                + "。"
            )
    else:
        summary = (
            f"**直接答案：**{primary.department or bundle.query.department} "
            f"{_restriction_label(primary.restriction_type)}"
            f"{_action_label(primary.action)}"
            f"{_format_effective_at(primary.effective_at)}。"
        )

    lines = [summary]
    related_facts = []
    for event in related:
        if not event.effective_at:
            continue
        if primary and event.restriction_type == primary.restriction_type:
            continue
        fact = {
            "restriction_type": event.restriction_type.value,
            "effective_at": event.effective_at,
            "department": event.department,
            "source_url": event.source_url,
        }
        related_facts.append(fact)
        lines.append(
            f"**相关但不同的限制：**"
            f"{_restriction_label(event.restriction_type)}"
            f"{_action_label(event.action)}"
            f"{_format_effective_at(event.effective_at)}。"
        )

    eligibility_payload = (
        bundle.eligibility.to_dict() if bundle.eligibility else None
    )
    if bundle.eligibility and bundle.eligibility.reason:
        lines.append(f"**你的适用性：**{bundle.eligibility.reason}")

    exceptions = list(primary.exceptions) if primary else []
    if exceptions:
        rendered = "；".join(
            (
                f"{item.get('course_id')}: {item.get('text')}"
                if item.get("course_id") and item.get("text")
                else item.get("text")
                or item.get("course_id")
                or "未命名例外"
            )
            for item in exceptions
        )
        lines.append(f"**例外：**{rendered}。")
    elif primary is not None:
        lines.append("**例外：**已检查该事件后的例外列表，未发现列出的例外。")

    if source_urls:
        rendered_sources = "、".join(
            f"[{_source_host(url)}]({url})" for url in source_urls
        )
        retrieved = next(
            (
                source.get("retrieved_at")
                for source in reversed(bundle.sources)
                if source.get("retrieved_at")
            ),
            None,
        )
        suffix = f"；抓取时间 {retrieved}" if retrieved else ""
        lines.append(f"**来源：**{rendered_sources}{suffix}。")

    return {
        "evidence_status": bundle.evidence_status.value,
        "primary": primary.to_dict() if primary else None,
        "related": related_facts,
        "eligibility": eligibility_payload,
        "exceptions": exceptions,
        "source_urls": source_urls,
        "summary_markdown": "\n\n".join(lines),
    }


def compact_linked_restriction_result(
    linked_result: dict[str, Any],
) -> dict[str, Any]:
    """Remove fetched prose while retaining replayable structured evidence."""

    compact_pages = []
    for page in linked_result.get("pages") or []:
        compact_pages.append(
            {
                key: page.get(key)
                for key in (
                    "url",
                    "domain",
                    "retrieved_at",
                    "source_link_text",
                    "source_blocks",
                    "link_role",
                    "depth",
                    "parent_url",
                    "content_block_count",
                    "selected_block_count",
                    "restriction_fields",
                    "timeline_events",
                )
            }
        )
    return {
        key: linked_result.get(key)
        for key in (
            "ok",
            "workflow_id",
            "source_url",
            "selected_count",
            "fetched_urls",
            "errors",
        )
    } | {"pages": compact_pages}


def _events_from_linked_pages(
    pages: list[dict[str, Any]],
) -> list[RestrictionEvent]:
    events: list[RestrictionEvent] = []
    for page_index, page in enumerate(pages):
        for event_index, payload in enumerate(page.get("timeline_events") or []):
            try:
                restriction_type = RestrictionType(payload["restriction_type"])
            except (KeyError, ValueError):
                continue
            events.append(
                RestrictionEvent(
                    event_id=(
                        f"p{page_index + 1}-"
                        f"{payload.get('event_id') or event_index + 1}"
                    ),
                    restriction_type=restriction_type,
                    action=str(payload.get("action") or "unknown"),
                    effective_at=payload.get("effective_at"),
                    term=str(payload.get("term") or ""),
                    department=str(payload.get("department") or ""),
                    course_scope=tuple(payload.get("course_scope") or []),
                    audience=tuple(payload.get("audience") or []),
                    exceptions=tuple(payload.get("exceptions") or []),
                    source_url=str(payload.get("source_url") or page.get("url") or ""),
                    source_role=str(
                        payload.get("source_role")
                        or page.get("link_role")
                        or "official_link"
                    ),
                    retrieved_at=payload.get("retrieved_at") or page.get("retrieved_at"),
                    source_position=payload.get("source_position"),
                    statement=str(payload.get("statement") or ""),
                )
            )
    return events


def _events_from_legacy_fields(
    websoc_result: dict[str, Any],
    query: RestrictionQuery,
) -> list[RestrictionEvent]:
    fields = websoc_result.get("fields") or {}
    mapping = (
        (
            RestrictionType.SCHOOL_MAJOR,
            "major_restriction_removed_at",
        ),
        (RestrictionType.NEW_ONLY, "nors_removed_at"),
    )
    events = []
    for restriction_type, field_name in mapping:
        effective_at = fields.get(field_name)
        if not effective_at:
            continue
        events.append(
            RestrictionEvent(
                event_id=f"websoc-{restriction_type.value}",
                restriction_type=restriction_type,
                action="removed",
                effective_at=str(effective_at),
                term=query.term,
                department=query.department,
                audience=(query.department,),
                source_url=str(websoc_result.get("source_url") or ""),
                source_role="registrar_websoc_comments",
                retrieved_at=websoc_result.get("retrieved_at"),
                source_position={"field": field_name},
                statement=str(effective_at),
            )
        )
    return events


def _deduplicate_events(
    events: list[RestrictionEvent],
) -> list[RestrictionEvent]:
    deduplicated: list[RestrictionEvent] = []
    seen: set[tuple[Any, ...]] = set()
    for event in events:
        key = (
            event.restriction_type,
            event.action,
            event.effective_at,
            event.department,
            event.course_scope,
            event.source_url,
        )
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(event)
    return deduplicated


def _target_events(
    events: list[RestrictionEvent],
    query: RestrictionQuery,
) -> list[RestrictionEvent]:
    if query.restriction_type == RestrictionType.AMBIGUOUS:
        requested_types = {
            RestrictionType.SCHOOL_MAJOR,
            RestrictionType.NEW_ONLY,
        }
    else:
        requested_types = {query.restriction_type}
    target_course = _course_key(query.course_id)
    return [
        event
        for event in events
        if event.restriction_type in requested_types
        and _departments_match(event.department, query.department)
        and (
            not target_course
            or query.restriction_type != RestrictionType.COURSE_SPECIFIC
            or target_course
            in {
                _course_key(course_id)
                for course_id in event.course_scope
            }
        )
    ]


def _select_primary_event(
    target_events: list[RestrictionEvent],
    query: RestrictionQuery,
) -> Optional[RestrictionEvent]:
    if not target_events:
        return None
    if query.restriction_type == RestrictionType.AMBIGUOUS:
        return next(
            (
                event
                for event in target_events
                if event.restriction_type == RestrictionType.SCHOOL_MAJOR
            ),
            target_events[0],
        )
    return target_events[0]


def _missing_required_fields(
    query: RestrictionQuery,
    event: Optional[RestrictionEvent],
) -> list[str]:
    if event is None:
        required = ["effective_at", "source_url"]
        if query.restriction_type == RestrictionType.COURSE_SPECIFIC:
            required = ["course_id", "current_stage", "next_change_or_unavailable"]
        return required
    missing = []
    if not event.effective_at:
        missing.append("effective_at")
    if not event.source_url:
        missing.append("source_url")
    if query.restriction_type in {
        RestrictionType.SCHOOL_MAJOR,
        RestrictionType.NEW_ONLY,
    }:
        if not event.department:
            missing.append("department")
        if not (event.course_scope or event.audience or event.statement):
            missing.append("scope")
    if query.restriction_type == RestrictionType.COURSE_SPECIFIC:
        if not query.course_id:
            missing.append("course_id")
        if not event.course_scope:
            missing.append("current_stage")
    return missing


def _find_conflicts(
    events: list[RestrictionEvent],
) -> list[dict[str, Any]]:
    dates = {
        event.effective_at
        for event in events
        if event.effective_at
    }
    if len(dates) <= 1:
        return []
    return [
        {
            "field": "effective_at",
            "values": sorted(dates),
            "event_ids": [event.event_id for event in events],
        }
    ]


def _build_sources(
    websoc_result: dict[str, Any],
    linked_result: dict[str, Any],
    events: list[RestrictionEvent],
) -> list[dict[str, Any]]:
    event_urls = {event.source_url for event in events if event.source_url}
    sources = []
    websoc_url = websoc_result.get("source_url")
    if websoc_url:
        sources.append(
            {
                "url": websoc_url,
                "source_role": "registrar_websoc",
                "retrieved_at": websoc_result.get("retrieved_at"),
                "provides_evidence": websoc_url in event_urls,
            }
        )
    seen = {websoc_url}
    for page in linked_result.get("pages") or []:
        url = page.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append(
            {
                "url": url,
                "source_role": page.get("link_role"),
                "retrieved_at": page.get("retrieved_at"),
                "depth": page.get("depth", 1),
                "provides_evidence": url in event_urls,
            }
        )
    return sources


def _evaluate_eligibility(
    query: RestrictionQuery,
    event: Optional[RestrictionEvent],
) -> RestrictionEligibility:
    if event is None:
        return RestrictionEligibility(
            eligible=None,
            student_major=query.student_major,
            student_school=query.student_school,
            reason="官方证据不足，无法判断当前资格。",
        )
    course_key = _course_key(query.course_id)
    matching_exceptions = [
        item
        for item in event.exceptions
        if course_key
        and course_key
        in {
            _course_key(item.get("course_id")),
            *(_course_key(value) for value in item.get("course_ids") or []),
        }
    ]
    if matching_exceptions:
        return RestrictionEligibility(
            eligible=None,
            student_major=query.student_major,
            student_school=query.student_school,
            reason=(
                f"{query.course_id} 被列为一般规则的例外；"
                "必须按该课程的专门规则判断。"
            ),
            evidence_event_id=event.event_id,
        )
    if not query.student_major and not query.student_school:
        return RestrictionEligibility(
            eligible=None,
            student_major=None,
            student_school=None,
            allowed_groups=event.audience,
            reason="未提供专业或学院，只能说明公开规则，不能替你推断身份。",
            evidence_event_id=event.event_id,
        )

    allowed_text = " ".join((*event.audience, event.statement))
    major = query.student_major or ""
    if major and re.search(rf"\b{re.escape(major)}\b", allowed_text, re.I):
        return RestrictionEligibility(
            eligible=True,
            student_major=query.student_major,
            student_school=query.student_school,
            allowed_groups=event.audience,
            reason=f"官方事件明确把 {major} 列入该阶段允许的群体。",
            evidence_event_id=event.event_id,
        )
    if re.search(r"all campus majors", allowed_text, re.I):
        return RestrictionEligibility(
            eligible=True,
            student_major=query.student_major,
            student_school=query.student_school,
            allowed_groups=event.audience,
            reason="官方事件说明该阶段面向全校专业开放。",
            evidence_event_id=event.event_id,
        )
    return RestrictionEligibility(
        eligible=None,
        student_major=query.student_major,
        student_school=query.student_school,
        allowed_groups=event.audience,
        reason="官方事件没有明确列出该身份，不能据此断言可选或不可选。",
        evidence_event_id=event.event_id,
    )


def _departments_match(left: str, right: str) -> bool:
    return _department_key(left) == _department_key(right)


def _department_key(value: Optional[str]) -> str:
    upper = (value or "").upper()
    upper = re.sub(r"I\s*&\s*C\s*SCI", "ICS", upper)
    return re.sub(r"[^A-Z0-9]", "", upper)


def _course_key(value: Optional[str]) -> str:
    upper = (value or "").upper()
    upper = re.sub(r"I\s*&\s*C\s*SCI", "ICS", upper)
    return re.sub(r"[^A-Z0-9]", "", upper)


def _restriction_label(restriction_type: RestrictionType) -> str:
    return {
        RestrictionType.SCHOOL_MAJOR: "School/Major 专业限制",
        RestrictionType.NEW_ONLY: "New Only（NOR）限制",
        RestrictionType.CLASS_LEVEL: "年级限制",
        RestrictionType.REPEAT: "重修限制",
        RestrictionType.AUTHORIZATION_CODE: "授权码限制",
        RestrictionType.COURSE_SPECIFIC: "课程专属限制",
        RestrictionType.ADD_DROP_CHANGE: "加退课/评分选项规则",
        RestrictionType.AMBIGUOUS: "限制",
    }[restriction_type]


def _action_label(action: str) -> str:
    return {
        "removed": "解除时间为 ",
        "active": "仍然生效，记录时间为 ",
        "extended": "延长/恢复，记录时间为 ",
    }.get(action, "记录时间为 ")


def _format_effective_at(value: Optional[str]) -> str:
    if not value:
        return "未公布"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M %Z").strip()


def _source_host(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url)
    return match.group(1) if match else url


_IGNORED_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "nav",
    "header",
    "footer",
    "aside",
}
_BLOCK_TAGS = {
    "body",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "li",
    "tr",
}
_DATE_LINE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")
_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b", re.I)
_SECTION = re.compile(
    r"^(COMPSCI|CSE|GDIM|I&C SCI|IN4MATX|STATS|SWE)\s+courses\b",
    re.I,
)


class _MainContentParser(HTMLParser):
    """Extract ordered semantic blocks while excluding site chrome."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, Any]] = []
        self._stack: list[dict[str, Any]] = []
        self._ignored_depth = 0
        self._main_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, Optional[str]]],
    ) -> None:
        del attrs
        tag = tag.lower()
        if tag in _IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in {"main", "article"}:
            self._main_depth += 1
        if tag in _BLOCK_TAGS:
            self._stack.append(
                {
                    "tag": tag,
                    "parts": [],
                    "in_main": self._main_depth > 0,
                }
            )

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or not self._stack:
            return
        for block in self._stack:
            block["parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag in _BLOCK_TAGS and self._stack:
            position = len(self.blocks)
            block = self._stack.pop()
            text = _collapse_ws(" ".join(block["parts"]))
            if text:
                self.blocks.append(
                    {
                        "index": position,
                        "tag": block["tag"],
                        "text": text,
                        "in_main": block["in_main"],
                    }
                )
        if tag in {"main", "article"} and self._main_depth:
            self._main_depth -= 1


def extract_main_content_blocks(html: str) -> list[dict[str, Any]]:
    """Return ordered main-content blocks, falling back to cleaned body blocks."""

    parser = _MainContentParser()
    parser.feed(html or "")
    parser.close()
    main = [block for block in parser.blocks if block["in_main"]]
    fallback = [
        block for block in parser.blocks if block["tag"] != "body"
    ] or parser.blocks
    selected = main or fallback
    return [
        {**block, "index": index}
        for index, block in enumerate(selected)
    ]


def select_query_focused_blocks(
    blocks: list[dict[str, Any]],
    query: RestrictionQuery,
    *,
    neighbor_count: int = 2,
) -> list[dict[str, Any]]:
    """Select relevant blocks plus structural/date neighbors for LLM context."""

    if not blocks:
        return []
    needles = {
        query.department.lower(),
        query.restriction_type.value.replace("_", " "),
    }
    if query.course_id:
        needles.add(query.course_id.lower())
    if query.restriction_type == RestrictionType.SCHOOL_MAJOR:
        needles.update({"school/major", "major restriction", "all campus majors"})
    elif query.restriction_type == RestrictionType.NEW_ONLY:
        needles.update({"new only", "nor", "nors"})
    elif query.restriction_type == RestrictionType.AMBIGUOUS:
        needles.update({"restriction", "enrollment"})

    selected: set[int] = set()
    current_heading: Optional[int] = None
    for index, block in enumerate(blocks):
        if block["tag"].startswith("h"):
            current_heading = index
        lowered = block["text"].lower()
        if any(needle and needle in lowered for needle in needles):
            selected.update(
                range(
                    max(0, index - neighbor_count),
                    min(len(blocks), index + neighbor_count + 1),
                )
            )
            if current_heading is not None:
                selected.add(current_heading)
    return [blocks[index] for index in sorted(selected)]


def parse_restriction_timeline(
    blocks: list[dict[str, Any]],
    *,
    term: str,
    department: str,
    source_url: str,
    source_role: str,
    retrieved_at: Optional[str] = None,
) -> list[RestrictionEvent]:
    """Parse date/time/action groups into deterministic restriction events."""

    drafts: list[dict[str, Any]] = []
    current_date: Optional[tuple[int, int, int]] = None
    current_time: Optional[tuple[int, int]] = None
    current_department = department
    active_event: Optional[dict[str, Any]] = None

    for index, block in enumerate(blocks):
        text = block["text"].strip()
        section = _SECTION.search(text)
        if section:
            current_department = _canonical_section_department(section.group(1))
            active_event = None
            continue

        date_match = _DATE_LINE.match(text)
        if date_match:
            month, day, year = (int(value) for value in date_match.groups())
            current_date = (year, month, day)
            current_time = None
            active_event = None
            continue

        time_match = _TIME.search(text)
        if time_match and _looks_like_time_line(text):
            current_time = _parse_time_match(time_match)
            if not _action_type(text):
                continue

        if active_event is not None and block.get("tag") == "li":
            refs = [ref.display() for ref, _start, _end in iter_course_mentions(text)]
            item: dict[str, Any] = {"text": text}
            if len(refs) == 1:
                item["course_id"] = refs[0]
            elif refs:
                item["course_ids"] = refs
            active_event["exceptions"].append(item)
            continue

        action_info = _action_type(text)
        if action_info is not None:
            restriction_type, action = action_info
            course_scope = [
                ref.display()
                for ref, _start, _end in iter_course_mentions(text)
            ]
            inline_date, inline_time = _inline_datetime(text)
            event_date = inline_date or current_date
            event_time = inline_time or current_time
            effective_at = _iso_los_angeles(event_date, event_time)
            audience = _extract_audience(text)
            active_event = {
                "event_id": f"{restriction_type.value}-{len(drafts) + 1}",
                "restriction_type": restriction_type,
                "action": action,
                "effective_at": effective_at,
                "term": term,
                "department": current_department,
                "course_scope": course_scope,
                "audience": list(audience),
                "exceptions": [],
                "source_url": source_url,
                "source_role": source_role,
                "retrieved_at": retrieved_at,
                "source_position": {
                    "block_index": block.get("index", index),
                    "tag": block.get("tag"),
                },
                "statement": text,
            }
            drafts.append(active_event)
            continue

    return [
        RestrictionEvent(
            event_id=draft["event_id"],
            restriction_type=draft["restriction_type"],
            action=draft["action"],
            effective_at=draft["effective_at"],
            term=draft["term"],
            department=draft["department"],
            course_scope=tuple(draft["course_scope"]),
            audience=tuple(draft["audience"]),
            exceptions=tuple(draft["exceptions"]),
            source_url=draft["source_url"],
            source_role=draft["source_role"],
            retrieved_at=draft["retrieved_at"],
            source_position=draft["source_position"],
            statement=draft["statement"],
        )
        for draft in drafts
    ]


def _action_type(text: str) -> Optional[tuple[RestrictionType, str]]:
    lowered = text.lower()
    action = None
    if re.search(r"\b(?:removed|lifted|open(?:ed)? to)\b|解除|开放", lowered):
        action = "removed"
    elif re.search(r"\b(?:remain|continues?|active|restricted to)\b|继续限制", lowered):
        action = "active"
    elif re.search(r"\b(?:extend|reinstate)\w*\b|延长|恢复限制", lowered):
        action = "extended"
    if action is None:
        return None
    if re.search(r"new only|\bNORS?\b|新生预留", text, re.I):
        return RestrictionType.NEW_ONLY, action
    if re.search(r"school/major|school or major|major restrictions?", text, re.I):
        return RestrictionType.SCHOOL_MAJOR, action
    if re.search(r"upper[- ]division standing|class[- ]level", text, re.I):
        return RestrictionType.CLASS_LEVEL, action
    if re.search(r"repeat restrictions?", text, re.I):
        return RestrictionType.REPEAT, action
    if any(iter_course_mentions(text)):
        return RestrictionType.COURSE_SPECIFIC, action
    return None


def _inline_datetime(
    text: str,
) -> tuple[Optional[tuple[int, int, int]], Optional[tuple[int, int]]]:
    date_match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    date_value = None
    if date_match:
        month, day, year = (int(value) for value in date_match.groups())
        date_value = (year, month, day)
    time_match = _TIME.search(text)
    return date_value, _parse_time_match(time_match) if time_match else None


def _parse_time_match(match: re.Match[str]) -> tuple[int, int]:
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = re.sub(r"[^apm]", "", match.group(3).lower())
    if meridiem.startswith("p") and hour != 12:
        hour += 12
    elif meridiem.startswith("a") and hour == 12:
        hour = 0
    return hour, minute


def _iso_los_angeles(
    date_value: Optional[tuple[int, int, int]],
    time_value: Optional[tuple[int, int]],
) -> Optional[str]:
    if date_value is None:
        return None
    hour, minute = time_value or (0, 0)
    value = datetime(
        *date_value,
        hour,
        minute,
        tzinfo=ZoneInfo("America/Los_Angeles"),
    )
    return value.isoformat()


def _looks_like_time_line(text: str) -> bool:
    stripped = text.strip().rstrip(":")
    return bool(_TIME.fullmatch(stripped))


def _extract_audience(text: str) -> tuple[str, ...]:
    match = re.search(
        r"(?:open(?:ed)?|available)\s+to\s+(.+?)(?:only)?(?:[.;]|$)",
        text,
        re.I,
    )
    if not match:
        return ()
    value = _collapse_ws(match.group(1)).strip(" :")
    return (value,) if value else ()


def _canonical_section_department(value: str) -> str:
    upper = value.upper()
    if upper in {"I&C SCI", "IN4MATX"}:
        return "I&C SCI" if upper == "I&C SCI" else "IN4MATX"
    return upper


def _collapse_ws(value: str) -> str:
    return " ".join((value or "").split())
