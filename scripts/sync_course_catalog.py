"""Fetch the whole UCI course catalogue from Anteater into the SQLite catalog.

Usage:
    python scripts/sync_course_catalog.py            # about 95 requests, under a minute
    python scripts/sync_course_catalog.py --status   # what is stored, and how fresh

Titles, prerequisites, GE labels, corequisites, grading option, repeatability
and overlapping courses are replaced together. A failed or sharply smaller
crawl keeps the catalogue already stored; --force bypasses only that size
guard. Set ANTEATER_API_KEY in the environment or .env for a dedicated rate
limit. Exit code is 1 when the sync did not complete.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from _bootstrap import ensure_repo_root_on_path

REPO_ROOT = ensure_repo_root_on_path()

from app.catalog import course_store  # noqa: E402
from app.catalog.course_sync import sync_courses  # noqa: E402


def _load_dotenv() -> None:
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=None, help="override CATALOG_DB_PATH")
    parser.add_argument("--force", action="store_true", help="bypass the shrink guard")
    parser.add_argument("--status", action="store_true", help="print the stored snapshot and exit")
    args = parser.parse_args(argv)
    db_path = Path(args.db) if args.db else None
    if args.status:
        snapshot = course_store.get_course_snapshot(path=db_path)
        if not snapshot:
            print("no course catalogue stored (readers use data/uci/courses.csv)")
        else:
            print(f"courses={snapshot['course_count']} synced={snapshot['last_synced_at']} "
                  f"source={snapshot['source']}"
                  + (f" last_error={snapshot['last_error']}" if snapshot["last_error"] else ""))
        return 0
    _load_dotenv()
    result = sync_courses(force=args.force, path=db_path)
    if result.ok:
        print(f"synced {result.course_count} courses in {result.pages} pages "
              f"(previously {result.previous_course_count})")
    else:
        print(f"{result.outcome} after {result.pages} pages — {result.error}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
