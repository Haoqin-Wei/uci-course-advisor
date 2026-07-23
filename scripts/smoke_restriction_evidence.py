"""Read-only live smoke check for the M14 restriction evidence pipeline.

This script is intentionally excluded from default CI because it requests the
UCI Registrar WebSoc form/results and query-selected official linked pages. It
prints only compact request metadata and parsed evidence; fetched page bodies
are never printed or persisted.

Run from the repository root:

    python scripts/smoke_restriction_evidence.py \
      --term "2026 Fall" \
      --department "I&C SCI" \
      --restriction-type school_major \
      --expect-primary "2026-09-18T12:00:00-07:00" \
      --expect-related "new_only=2026-09-01T12:00:00-07:00" \
      --expect-exception "I&C SCI 139W"
"""

from __future__ import annotations

import argparse

try:
    from scripts._bootstrap import ensure_repo_root_on_path
except ModuleNotFoundError:  # direct execution
    from _bootstrap import ensure_repo_root_on_path

ensure_repo_root_on_path()

from app.agent.tools import dispatch
from app.data.restriction_timeline import RestrictionType


def _events_by_id(bundle: dict) -> dict[str, dict]:
    return {
        event["event_id"]: event
        for event in bundle.get("events") or []
        if event.get("event_id")
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL {message}")


def _check_expectations(
    bundle: dict,
    *,
    expected_primary: str | None,
    expected_related: list[str],
    expected_exception: list[str],
) -> None:
    events = _events_by_id(bundle)
    primary = events.get(bundle.get("primary_event_id"))
    _require(primary is not None, "evidence bundle has no primary event")

    if expected_primary:
        _require(
            primary.get("effective_at") == expected_primary,
            "primary effective_at "
            f"{primary.get('effective_at')!r} != {expected_primary!r}",
        )

    related = [
        events[event_id]
        for event_id in bundle.get("related_event_ids") or []
        if event_id in events
    ]
    related_pairs = {
        f"{event.get('restriction_type')}={event.get('effective_at')}"
        for event in related
    }
    for expected in expected_related:
        _require(expected in related_pairs, f"missing related event {expected!r}")

    exception_text = " ".join(
        str(item.get("course_id") or item.get("text") or "")
        for item in (primary.get("exceptions") or [])
    )
    for expected in expected_exception:
        _require(
            expected.casefold() in exception_text.casefold(),
            f"missing primary exception {expected!r}",
        )


def run(
    *,
    term: str,
    department: str,
    restriction_type: str,
    expected_primary: str | None = None,
    expected_related: list[str] | None = None,
    expected_exception: list[str] | None = None,
) -> None:
    result = dispatch(
        "get_department_restrictions",
        {
            "term": term,
            "department": department,
            "restriction_type": restriction_type,
        },
        context={"term": term, "user_id": ""},
    )
    _require(isinstance(result, dict), "tool returned a non-object result")
    _require(result.get("ok") is True, f"workflow failed: {result}")

    bundle = result.get("evidence_bundle") or {}
    _require(
        bundle.get("evidence_status") == "verified",
        "evidence status is "
        f"{bundle.get('evidence_status')!r}; "
        f"missing={bundle.get('missing_required_fields') or []}",
    )
    _check_expectations(
        bundle,
        expected_primary=expected_primary,
        expected_related=expected_related or [],
        expected_exception=expected_exception or [],
    )

    fetches = result.get("fetch_summary") or []
    _require(fetches, "fetch audit is empty")
    _require(all(item.get("ok") for item in fetches), "one or more requests failed")
    _require(
        any(item.get("method") == "POST" for item in fetches),
        "WebSoc results POST is absent from the fetch audit",
    )
    _require(
        any(item.get("provides_evidence") for item in fetches),
        "no fetched request is marked as final evidence",
    )

    events = _events_by_id(bundle)
    primary = events[bundle["primary_event_id"]]
    print(
        "PASS restriction evidence "
        f"term={term!r} department={department!r} "
        f"type={restriction_type!r} "
        f"effective_at={primary.get('effective_at')!r}"
    )
    print(
        "PASS evidence gate "
        f"status={bundle.get('evidence_status')} "
        f"events={len(events)} "
        f"exceptions={len(primary.get('exceptions') or [])}"
    )
    for item in fetches:
        print(
            "PASS fetch "
            f"method={item.get('method')} "
            f"status={item.get('status_code')} "
            f"bytes={item.get('bytes')} "
            f"duration_ms={item.get('duration_ms')} "
            f"depth={item.get('depth')} "
            f"evidence={item.get('provides_evidence')} "
            f"url={item.get('final_url') or item.get('url')}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only live smoke check for M14 restriction evidence."
    )
    parser.add_argument("--term", default="2026 Fall")
    parser.add_argument("--department", default="I&C SCI")
    parser.add_argument(
        "--restriction-type",
        default=RestrictionType.SCHOOL_MAJOR.value,
        choices=[item.value for item in RestrictionType],
    )
    parser.add_argument("--expect-primary")
    parser.add_argument(
        "--expect-related",
        action="append",
        default=[],
        metavar="TYPE=ISO_DATETIME",
    )
    parser.add_argument("--expect-exception", action="append", default=[])
    args = parser.parse_args()
    run(
        term=args.term,
        department=args.department,
        restriction_type=args.restriction_type,
        expected_primary=args.expect_primary,
        expected_related=args.expect_related,
        expected_exception=args.expect_exception,
    )


if __name__ == "__main__":
    main()
