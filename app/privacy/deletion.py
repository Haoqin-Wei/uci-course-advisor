"""Account deletion across the private-beta persistence stores."""

from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path

from app import config
from app.academic import delete_user_data as delete_academic_data
from app.auth import store as auth_store
from app.data import sessions as sessions_data


_SAFE_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _remove_user_directory(root: Path, user_id: str) -> None:
    root = root.resolve()
    target = (root / user_id).resolve()
    if target.parent != root:
        raise ValueError("Unsafe user data path")
    if target.exists():
        shutil.rmtree(target)


def _delete_sqlite_memory(user_id: str) -> None:
    path = config.memory_db_path()
    if not path.exists():
        return
    with sqlite3.connect(path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for table in ("memory_events", "memory_items", "memory_profiles", "memory_migrations"):
            if table in tables:
                conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        conn.commit()


def delete_account_data(user_id: str) -> None:
    if not _SAFE_USER_ID.fullmatch(user_id or ""):
        raise ValueError("Unsafe user id")

    # Delete user-owned data before the auth row so a failure never leaves
    # inaccessible academic data behind under a deleted account id.
    delete_academic_data(user_id)
    _delete_sqlite_memory(user_id)

    roots = {config.memory_root_path(), sessions_data.MEMORY_ROOT}
    for root in roots:
        _remove_user_directory(Path(root), user_id)

    auth_store.delete_user(user_id)
