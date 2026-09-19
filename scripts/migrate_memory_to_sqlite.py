#!/usr/bin/env python3
"""Import legacy JSON profile/fact/preference memory into SQLite.

The application performs this migration lazily when each user next loads a
session. This command is the eager deployment option: run it before switching
traffic and inspect the printed per-user counts.

Usage:
    python scripts/migrate_memory_to_sqlite.py
    python scripts/migrate_memory_to_sqlite.py --dry-run
    python scripts/migrate_memory_to_sqlite.py --memory-root /srv/zot/memory
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from _bootstrap import ensure_repo_root_on_path


ensure_repo_root_on_path()

from app.memory.sqlite_provider import SQLiteMemoryProvider  # noqa: E402


def _user_directories(memory_root: Path) -> list[Path]:
    result = []
    if not memory_root.exists():
        return result
    for path in sorted(memory_root.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        if any((path / name).exists() for name in ("profile.json", "facts.json", "preferences.json")):
            result.append(path)
    return result


def _run(memory_root: Path, db_path: Path, max_active_items: int) -> tuple[int, int]:
    provider = SQLiteMemoryProvider(
        db_path=db_path,
        legacy_base_dir=memory_root,
        max_active_items=max_active_items,
    )
    migrated = 0
    errors = 0
    for user_dir in _user_directories(memory_root):
        try:
            provider.initialize("migration", user_dir.name)
            stats = provider.memory_stats(user_dir.name)
            profile_fields = len(provider.get_profile(user_dir.name))
            print(
                f"✓ {user_dir.name}: profile_fields={profile_fields}, "
                f"active={stats.get('active', 0)}, "
                f"superseded={stats.get('superseded', 0)}"
            )
            migrated += 1
        except Exception as exc:  # deployment script must continue to report all users
            print(f"✗ {user_dir.name}: {type(exc).__name__}: {exc}")
            errors += 1
    provider.shutdown()
    return migrated, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-root", default="data/memory")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--max-active-items", type=int, default=1000)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Import into a temporary database and leave the target untouched",
    )
    args = parser.parse_args()

    memory_root = Path(args.memory_root)
    users = _user_directories(memory_root)
    if not users:
        print(f"No legacy memory users found under {memory_root}")
        return 0

    if args.dry_run:
        with tempfile.TemporaryDirectory(prefix="zot-memory-migration-") as temp_dir:
            migrated, errors = _run(
                memory_root,
                Path(temp_dir) / "long_term_memory.db",
                args.max_active_items,
            )
        label = "DRY RUN"
    else:
        db_path = Path(args.db_path) if args.db_path else memory_root / "long_term_memory.db"
        migrated, errors = _run(memory_root, db_path, args.max_active_items)
        label = str(db_path)

    print(f"{label}: {migrated} users imported, {errors} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
