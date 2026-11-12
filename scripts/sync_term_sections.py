"""Fetch whole-term section listings from Anteater into the SQLite catalog.

Usage:
    python scripts/sync_term_sections.py --term "2027 Winter"
    python scripts/sync_term_sections.py --term "2027 Winter" --term "2026 Fall" --term "2026 Winter"
    python scripts/sync_term_sections.py --status          # show what is stored

One HTTP request per term (about 8 MB). Set ANTEATER_API_KEY in the
environment or .env for a dedicated rate limit. A failed or suspicious fetch
keeps the rows already stored; --force bypasses only the shrink guard.
Exit code is 1 when any requested term did not sync.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from _bootstrap import ensure_repo_root_on_path

REPO_ROOT = ensure_repo_root_on_path()

from app.catalog import store  # noqa: E402
from app.catalog.sync import sync_term  # noqa: E402
from app.catalog.term import Term  # noqa: E402


def _load_dotenv() -> None:
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _print_status(db_path) -> None:
    snapshots = store.list_term_snapshots(path=db_path)
    if not snapshots:
        print("catalog store is empty")
    for item in snapshots:
        print(f"{item['term_id']:<14} {item['status']:<11} sections={item['section_count']:<6} "
              f"courses={item['course_count']:<5} depts={item['department_count']:<4} "
              f"synced={item['last_synced_at']} source={item['source']}"
              + (f" last_error={item['last_error']}" if item["last_error"] else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--term", action="append", default=[], help='e.g. "2027 Winter"; repeatable')
    parser.add_argument("--db", default=None, help="override CATALOG_DB_PATH")
    parser.add_argument("--force", action="store_true", help="bypass the shrink guard")
    parser.add_argument("--status", action="store_true", help="print stored snapshots and exit")
    args = parser.parse_args(argv)
    db_path = Path(args.db) if args.db else None
    if args.status:
        _print_status(db_path)
        return 0
    if not args.term:
        parser.error("give at least one --term, or --status")
    terms = []
    for text in args.term:
        term = Term.parse(text)
        if not term:
            parser.error(f"unrecognized term {text!r}")
        terms.append(term)
    _load_dotenv()
    # Oldest first, so a new term can be compared with last year's same quarter.
    failed = False
    for term in sorted(terms):
        result = sync_term(term.term_id, force=args.force, path=db_path)
        line = f"{term.term_id}: {result.outcome}"
        if result.outcome == "synced":
            line += f" ({result.status}), {result.section_count} sections (previously {result.previous_section_count})"
        elif result.error:
            line += f" — {result.error}"
        print(line)
        failed = failed or not result.ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
