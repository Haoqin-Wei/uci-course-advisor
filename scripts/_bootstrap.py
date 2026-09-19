"""Script bootstrap helpers.

Direct execution like `python scripts/smoke_test.py` puts `scripts/`
on `sys.path`, not the repository root. Add the root explicitly so
scripts can import `app.*` without requiring callers to set PYTHONPATH.
"""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def ensure_repo_root_on_path() -> Path:
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return REPO_ROOT
