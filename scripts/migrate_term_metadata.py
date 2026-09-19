#!/usr/bin/env python3
"""Idempotently migrate legacy conversation term metadata to auto mode."""

from _bootstrap import bootstrap_project_root

bootstrap_project_root()

from app.data.sessions import migrate_term_metadata


if __name__ == "__main__":
    result = migrate_term_metadata()
    print(
        "term metadata migration: "
        f"migrated={result['migrated']} skipped={result['skipped']} failed={result['failed']}"
    )
    raise SystemExit(1 if result["failed"] else 0)
