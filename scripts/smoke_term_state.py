"""Read-only live smoke check for the M13 automatic-term data sources.

This script is intentionally excluded from default CI because it calls the
hosted Anteater API. It does not write the runtime term cache.

Run from the repository root:

    python scripts/smoke_term_state.py
    python scripts/smoke_term_state.py --term "2026 Fall"
"""

from __future__ import annotations

import argparse

try:
    from scripts._bootstrap import ensure_repo_root_on_path
except ModuleNotFoundError:  # direct execution
    from _bootstrap import ensure_repo_root_on_path

ensure_repo_root_on_path()

from app.data import anteater
from app.terms import TermKey, parse_term_key


QUARTER_ORDER = {"Winter": 0, "Spring": 1, "Fall": 2}


def _calendar_term(record: dict) -> TermKey | None:
    values = [
        record.get("shortName"),
        record.get("term"),
        (
            f"{record.get('year')} {record.get('quarter')}"
            if record.get("year") and record.get("quarter")
            else None
        ),
    ]
    for value in values:
        if not value:
            continue
        parsed = parse_term_key(str(value))
        if parsed.kind == "single":
            return parsed.terms[0]
    return None


def _require_ok(label: str, result) -> None:
    if not result.ok:
        raise SystemExit(
            f"FAIL {label}: status={result.status} error={result.error or 'unknown'}"
        )


def _choose_term(
    requested: str | None,
    calendar_terms: set[TermKey],
    websoc_terms: set[TermKey],
) -> TermKey:
    if requested:
        parsed = parse_term_key(requested)
        if parsed.kind != "single":
            raise SystemExit(f"FAIL invalid --term value: {requested!r}")
        candidate = parsed.terms[0]
        if candidate not in calendar_terms:
            raise SystemExit(f"FAIL {candidate.canonical_name} is absent from /calendar/all")
        if candidate not in websoc_terms:
            raise SystemExit(f"FAIL {candidate.canonical_name} is absent from /websoc/terms")
        return candidate

    candidates = [
        term
        for term in calendar_terms & websoc_terms
        if term.is_regular
    ]
    if not candidates:
        raise SystemExit("FAIL no regular term is shared by calendar and WebSoc")
    return max(candidates, key=lambda term: (term.year, QUARTER_ORDER[term.quarter]))


def run(requested_term: str | None = None) -> None:
    calendar = anteater.fetch_calendar_all()
    _require_ok("calendar/all", calendar)
    calendar_terms = {
        term
        for record in (calendar.data or [])
        if (term := _calendar_term(record)) is not None
    }
    if not calendar_terms:
        raise SystemExit("FAIL calendar/all returned no parseable terms")

    terms = anteater.fetch_websoc_terms()
    _require_ok("websoc/terms", terms)
    websoc_terms: set[TermKey] = set()
    for record in terms.data or []:
        parsed = parse_term_key(str(record.get("shortName") or ""))
        if parsed.kind == "single":
            websoc_terms.add(parsed.terms[0])
    if not websoc_terms:
        raise SystemExit("FAIL websoc/terms returned no parseable terms")

    candidate = _choose_term(requested_term, calendar_terms, websoc_terms)
    availability = anteater.check_term_data_availability(candidate)
    if availability.status != "ok":
        raise SystemExit(
            f"FAIL full WebSoc {candidate.canonical_name}: "
            f"status={availability.status} error={availability.error or 'unknown'}"
        )
    if not availability.available:
        raise SystemExit(
            f"FAIL full WebSoc {candidate.canonical_name} is an empty shell "
            f"(courses={availability.course_count}, sections={availability.section_count})"
        )

    print(
        "PASS calendar/all "
        f"records={len(calendar.data or [])} bytes={calendar.content_length}"
    )
    print(
        "PASS websoc/terms "
        f"terms={len(websoc_terms)} bytes={terms.content_length}"
    )
    print(
        f"PASS full WebSoc term={candidate.canonical_name} "
        f"courses={availability.course_count} sections={availability.section_count} "
        f"bytes={availability.content_length}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only live smoke check for M13 automatic-term sources."
    )
    parser.add_argument(
        "--term",
        help="Canonical or parseable UCI term; defaults to the latest shared regular term.",
    )
    args = parser.parse_args()
    run(args.term)


if __name__ == "__main__":
    main()
