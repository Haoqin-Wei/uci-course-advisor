"""Fetch every major, minor, specialization and campus-wide requirement set
from Anteater into the SQLite catalog.

Usage:
    python scripts/sync_programs.py            # about 310 requests, roughly two minutes
    python scripts/sync_programs.py --status   # what is stored, and how fresh

A failed or sharply smaller crawl keeps the snapshot already stored; --force
bypasses only that size guard. Set ANTEATER_API_KEY in the environment or
.env for a dedicated rate limit. Exit code is 1 when the sync did not complete.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from _bootstrap import ensure_repo_root_on_path

REPO_ROOT = ensure_repo_root_on_path()

from app.catalog import program_store  # noqa: E402
from app.catalog.program_sync import sync_programs  # noqa: E402


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
        snapshot = program_store.get_program_snapshot(path=db_path)
        if not snapshot:
            print("no programs stored (readers fall back to live Anteater requests)")
        else:
            kinds = program_store.counts_by_kind(path=db_path)
            print(f"programs={snapshot['program_count']} "
                  + " ".join(f"{kind}={kinds.get(kind, 0)}" for kind in program_store.KINDS)
                  + f" synced={snapshot['last_synced_at']} source={snapshot['source']}"
                  + (f" last_error={snapshot['last_error']}" if snapshot["last_error"] else ""))
        return 0
    _load_dotenv()
    result = sync_programs(force=args.force, path=db_path)
    if result.ok:
        kinds = program_store.counts_by_kind(path=db_path)
        print(f"synced {result.program_count} programs in {result.requests} requests "
              f"(previously {result.previous_program_count}): "
              + ", ".join(f"{kinds.get(kind, 0)} {kind}" for kind in program_store.KINDS))
    else:
        print(f"{result.outcome} after {result.requests} requests — {result.error}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
