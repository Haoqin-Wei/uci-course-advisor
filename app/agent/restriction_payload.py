"""What the model is shown when a restriction result repeats earlier content.

Several departments can link to the same official page (I&C SCI and COMPSCI
both link to the ICS enrollment-restrictions page). When the model looks up
both in one turn, the second result used to carry the whole page again. The
model-facing copy of a later result now points back to the earlier one for
events that are identical; nothing the model has not already been given in
this turn is removed, and the full result is still used for the UI, the fetch
audit and the evidence checks.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Optional

TOOL_NAME = "get_department_restrictions"

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


class RestrictionPayloadDeduper:
    """Per-turn memory of the restriction events already given to the model."""

    def __init__(self) -> None:
        # (scope, event fingerprint) -> the result that first carried it.
        self._seen: dict[tuple[str, str], str] = {}

    def model_view(self, result: Any) -> tuple[Any, Optional[dict[str, int]]]:
        """Return the copy to serialize for the model and what was left out.

        The second value is ``None`` when the result is passed through as is.
        """
        if not isinstance(result, dict) or result.get("ok") is False:
            return result, None
        origin = _origin_label(result)
        view = deepcopy(result)
        page_events = self._dedupe_pages(view, origin)
        bundle_events = self._dedupe_bundle(view, origin)
        if not page_events and not bundle_events:
            return result, None
        return view, {
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
        # The events this query's answer rests on always stay in full.
        key_ids = {
            bundle.get("primary_event_id"),
            *(bundle.get("related_event_ids") or []),
            *(
                event_id
                for conflict in bundle.get("conflicts") or []
                if isinstance(conflict, dict)
                for event_id in conflict.get("event_ids") or []
            ),
        }
        kept, omitted = self._split(bundle["events"], scope="bundle", origin=origin, always_keep=key_ids)
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
