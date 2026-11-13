"""What the model is shown when a restriction result repeats earlier content.

Several departments can link to the same official page (I&C SCI and COMPSCI
both link to the ICS enrollment-restrictions page). When the model looks up
both in one turn, the second result used to carry the whole page again. The
model-facing copy of a later result now points back to the earlier one for
events that are identical; nothing the model has not already been given in
this turn is removed, and the full result is still used for the UI, the fetch
audit and the evidence checks.

A single result is also narrowed before that (``focus_on_department``). An
official page such as the ICS one carries a timeline for every department of
the school, and the result used to list each event twice: once under the page
and once in the evidence bundle. The model now gets one copy, limited to the
department it asked about plus the page's school-wide notes.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Optional

from app.data.restriction_timeline import _course_key, _departments_match

TOOL_NAME = "get_department_restrictions"
# Shown to the model in place of the queried department for entries that sit
# above the page's department sections and therefore apply to all of them.
SCHOOL_WIDE = "SCHOOL-WIDE"

# Retrieval stamps differ between a live read and a cache hit of the same page.
_VOLATILE_KEYS = frozenset({"retrieved_at"})
def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in value.items() if key not in _VOLATILE_KEYS}
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    return value


def _fingerprint(value: Any) -> str:
    text = json.dumps(_stable(value), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _key_event_ids(bundle: dict) -> set:
    """Events the answer to this query rests on; these are always shown in full."""
    return {
        bundle.get("primary_event_id"),
        *(bundle.get("related_event_ids") or []),
        *(
            event_id
            for conflict in bundle.get("conflicts") or []
            if isinstance(conflict, dict)
            for event_id in conflict.get("event_ids") or []
        ),
    } - {None}


def _page_section(event: dict) -> tuple[bool, Optional[str]]:
    """(came from a sectioned page read, department section it sits under)."""
    position = event.get("source_position")
    if not isinstance(position, dict) or "section" not in position:
        return False, None
    return True, position.get("section") or None


def _names_course(event: dict, course_id: Optional[str]) -> bool:
    target = _course_key(course_id)
    if not target:
        return False
    named = list(event.get("course_scope") or [])
    for item in event.get("exceptions") or []:
        if isinstance(item, dict):
            named.extend(item.get("course_ids") or [])
            named.append(item.get("course_id"))
    return any(_course_key(course) == target for course in named if course)


def focus_on_department(result: Any) -> tuple[Any, Optional[dict[str, Any]]]:
    """Narrow one result to the department that was asked about.

    Entries are matched by the page section they sit under, never by the
    wording inside them: section titles are registrar department codes, while
    the text uses abbreviations and often names other departments. Kept are the
    queried department's section, the school-wide notes above the sections,
    the events the answer rests on, and (for a course question) entries in
    other sections that name that course. The result is returned unchanged
    when the page has no section for the department or no primary event was
    selected, so an unfamiliar page layout is never narrowed by guesswork.
    """
    if not isinstance(result, dict) or result.get("ok") is False:
        return result, None
    bundle = result.get("evidence_bundle")
    if not isinstance(bundle, dict) or not isinstance(bundle.get("events"), list):
        return result, None
    if not bundle.get("primary_event_id"):
        return result, None
    department = str(result.get("department") or "")
    events = [event for event in bundle["events"] if isinstance(event, dict)]
    sections = {section for _, section in map(_page_section, events) if section}
    if not any(_departments_match(section, department) for section in sections):
        return result, None

    key_ids = _key_event_ids(bundle)
    course_id = result.get("course_id")
    kept: list[dict] = []
    left_out: dict[str, int] = {}
    for event in events:
        from_page, section = _page_section(event)
        if (
            section is None
            or _departments_match(section, department)
            or event.get("event_id") in key_ids
            or _names_course(event, course_id)
        ):
            kept.append(event)
        else:
            left_out[section] = left_out.get(section, 0) + 1

    view = deepcopy(result)
    kept_ids = {event.get("event_id") for event in kept}
    view_events = [event for event in view["evidence_bundle"]["events"]
                   if isinstance(event, dict) and event.get("event_id") in kept_ids]
    school_wide = 0
    for event in view_events:
        from_page, section = _page_section(event)
        if from_page and section is None:
            event["department"] = SCHOOL_WIDE
            school_wide += 1
    view_bundle = view["evidence_bundle"]
    view_bundle["events"] = view_events
    if school_wide:
        view_bundle["school_wide_note"] = (
            f"Events with department {SCHOOL_WIDE} come from the general notes above the page's "
            "department sections and apply to every department on that page."
        )
    if left_out:
        view_bundle["other_departments_omitted"] = {
            "events_by_department": dict(sorted(left_out.items())),
            "note": (
                f"The page also has timelines for these departments. They are not shown because "
                f"this lookup is for {department}; call {TOOL_NAME} for one of them to see it."
            ),
        }
    pages_trimmed = 0
    linked = view.get("linked_pages")
    for page in (linked.get("pages") if isinstance(linked, dict) else None) or []:
        if isinstance(page, dict) and isinstance(page.get("timeline_events"), list):
            count = len(page.pop("timeline_events"))
            page["timeline_event_count"] = count
            page["timeline_events_note"] = "Listed once, in evidence_bundle.events."
            pages_trimmed += 1
    return view, {
        "events_kept": len(view_events),
        "events_other_departments": sum(left_out.values()),
        "school_wide_events": school_wide,
        "page_copies_dropped": pages_trimmed,
        "focus_chars_before": _size(result),
        "focus_chars_after": _size(view),
    }


class RestrictionPayloadDeduper:
    """Per-turn memory of the restriction events already given to the model."""

    def __init__(self) -> None:
        # (scope, event fingerprint) -> the result that first carried it.
        self._seen: dict[tuple[str, str], str] = {}

    def model_view(self, result: Any) -> tuple[Any, Optional[dict[str, int]]]:
        """Return the copy to serialize for the model and what was left out.

        The result is first narrowed to its department, then stripped of events
        the model already has. The second value is ``None`` when the result is
        passed through as is.
        """
        if not isinstance(result, dict) or result.get("ok") is False:
            return result, None
        origin = _origin_label(result)
        focused, focus = focus_on_department(result)
        view = deepcopy(focused)
        page_events = self._dedupe_pages(view, origin)
        bundle_events = self._dedupe_bundle(view, origin)
        if not page_events and not bundle_events:
            return focused, focus
        return view, {
            **(focus or {}),
            "page_events_omitted": page_events,
            "bundle_events_omitted": bundle_events,
            "chars_before": _size(result),
            "chars_after": _size(view),
        }

    def _dedupe_pages(self, view: dict, origin: str) -> int:
        linked = view.get("linked_pages")
        pages = linked.get("pages") if isinstance(linked, dict) else None
        if not isinstance(pages, list):
            return 0
        total = 0
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get("timeline_events"), list):
                continue
            kept, omitted = self._split(
                page["timeline_events"], scope=f"page:{page.get('url')}", origin=origin,
            )
            if omitted:
                page["timeline_events"] = kept
                page["timeline_events_omitted"] = _omitted_note(omitted, "on this same linked page")
                total += len(omitted)
        return total

    def _dedupe_bundle(self, view: dict, origin: str) -> int:
        bundle = view.get("evidence_bundle")
        if not isinstance(bundle, dict) or not isinstance(bundle.get("events"), list):
            return 0
        kept, omitted = self._split(
            bundle["events"], scope="bundle", origin=origin, always_keep=_key_event_ids(bundle),
        )
        if omitted:
            bundle["events"] = kept
            bundle["events_omitted"] = _omitted_note(omitted, "in evidence_bundle.events")
        return len(omitted)

    def _split(
        self,
        events: list,
        *,
        scope: str,
        origin: str,
        always_keep: frozenset | set = frozenset(),
    ) -> tuple[list, list[tuple[str, str]]]:
        """Separate events to show from ones the model already has verbatim."""
        kept: list[Any] = []
        omitted: list[tuple[str, str]] = []
        for event in events:
            if not isinstance(event, dict):
                kept.append(event)
                continue
            key = (scope, _fingerprint(event))
            earlier = self._seen.get(key)
            if earlier is None:
                self._seen[key] = origin
                kept.append(event)
            elif event.get("event_id") in always_keep:
                kept.append(event)
            else:
                omitted.append((str(event.get("event_id")), earlier))
        return kept, omitted


def _omitted_note(omitted: list[tuple[str, str]], where: str) -> dict[str, Any]:
    origins = list(dict.fromkeys(origin for _, origin in omitted))
    return {
        "event_ids": [event_id for event_id, _ in omitted],
        "note": (
            "Omitted because they are identical (same event_id and content) to events "
            f"{where} in the earlier {TOOL_NAME} result for {'; '.join(origins)}. "
            "Use that copy; nothing new was left out."
        ),
    }


def _origin_label(result: dict) -> str:
    department = result.get("department") or "unknown department"
    course = result.get("course_id")
    term = result.get("term") or "unknown term"
    target = f"{department} ({course})" if course else str(department)
    return f"{target}, {term}"
