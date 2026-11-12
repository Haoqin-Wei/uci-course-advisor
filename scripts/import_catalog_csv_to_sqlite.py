"""Copy the checked-in section CSVs into the SQLite catalog store.

Usage:
    python scripts/import_catalog_csv_to_sqlite.py            # all terms
    python scripts/import_catalog_csv_to_sqlite.py --dry-run  # counts only
    python scripts/import_catalog_csv_to_sqlite.py --term 2026_Spring

Every imported term is labelled ``partial``: row volume cannot prove that
every department was collected (same rule as app/catalog/coverage.py). The
snapshot time is the newest ``updated_at`` in the CSV rows, not "now", so a
months-old CSV is not presented as fresh. Re-running is safe; each term is
replaced atomically. The CSV files are read only.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

from _bootstrap import ensure_repo_root_on_path

ensure_repo_root_on_path()

from app.catalog import store  # noqa: E402
from app.catalog.term import Term  # noqa: E402

SOURCE = "csv_import"


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def build_term_rows(data_dir: Path) -> dict[str, list[dict]]:
    """Group CSV sections by canonical term id in the store's row shape."""
    instructors: dict[str, list[str]] = defaultdict(list)
    for row in _read(data_dir / "section_instructors.csv"):
        name = (row.get("instructor_name_raw") or "").strip()
        if name:
            instructors[row.get("section_id", "")].append(name)
    ge: dict[str, list[str]] = defaultdict(list)
    for row in _read(data_dir / "section_ge.csv"):
        code = (row.get("ge_code") or "").strip()
        if code:
            ge[row.get("section_id", "")].append(code)

    by_term: dict[str, list[dict]] = defaultdict(list)
    for row in _read(data_dir / "sections.csv"):
        term = Term.parse((row.get("term_id") or "").strip())
        if not term:
            continue
        section_id = row.get("section_id", "")
        by_term[term.term_id].append({
            "section_code": row.get("sectionCode"),
            "department": row.get("department"),
            "course_number": row.get("courseNumber"),
            "course_numeric": row.get("courseNumeric"),
            "section_type": row.get("sectionType"),
            "section_num": row.get("sectionNum"),
            "units": row.get("units"),
            "instructors": sorted(set(instructors.get(section_id, []))),
            "ge_categories": sorted(set(ge.get(section_id, []))),
            "days": row.get("days"),
            "start_time": row.get("start_time"),
            "end_time": row.get("end_time"),
            "location": row.get("location"),
            "time_is_tba": row.get("time_is_tba"),
            "meetings_json": row.get("meetings_json"),
            "final_exam_json": row.get("final_exam_json"),
            "restrictions": row.get("restrictions"),
            "comments_json": row.get("comments_json"),
            "section_comment": row.get("section_comment") if row.get("payload_version") else (row.get("section_comment") or None),
            "payload_version": row.get("payload_version"),
            "is_cancelled": row.get("is_cancelled"),
            "status": row.get("status"),
            "max_capacity": row.get("max_capacity"),
            "total_enrolled": row.get("total_enrolled"),
            "section_enrolled": row.get("section_enrolled"),
            "num_on_waitlist": row.get("num_on_waitlist"),
            "num_requested": row.get("num_requested"),
            "source_updated_at": row.get("updated_at"),
        })
    return dict(by_term)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", default="data/uci")
    parser.add_argument("--db", default=None, help="override CATALOG_DB_PATH")
    parser.add_argument("--term", action="append", help="term id such as 2026_Spring; repeatable")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="bypass the shrink guard")
    args = parser.parse_args(argv)

    by_term = build_term_rows(Path(args.data_dir))
    wanted = None
    if args.term:
        wanted = set()
        for text in args.term:
            term = Term.parse(text)
            if not term:
                parser.error(f"unrecognized term {text!r}")
            wanted.add(term.term_id)
    db_path = Path(args.db) if args.db else None
    failed = False
    for term_id in sorted(by_term):
        if wanted is not None and term_id not in wanted:
            continue
        rows = by_term[term_id]
        stamps = [r["source_updated_at"] for r in rows if r.get("source_updated_at")]
        newest = max(stamps) if stamps else None
        if args.dry_run:
            print(f"{term_id}: {len(rows)} sections (dry run, newest source row {newest})")
            continue
        result = store.replace_term_sections(
            term_id, rows, source=SOURCE, status="partial",
            fetched_at=newest, synced_at=newest, force=args.force, path=db_path,
        )
        print(f"{term_id}: {result.reason}, {result.section_count} sections stored "
              f"(previously {result.previous_section_count})")
        failed = failed or not result.replaced
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
