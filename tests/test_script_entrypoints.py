from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_script_without_pythonpath(script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / script)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_validation_smoke_script_runs_without_pythonpath():
    result = _run_script_without_pythonpath("scripts/smoke_test.py")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "Loaded catalog" in result.stdout
    assert "Validation overall: fail" in result.stdout
    assert "HALLUCINATED_COURSE_ID" in result.stdout
    assert "COMPSCI 999" in result.stdout
    assert "UNKNOWN_INSTRUCTOR" in result.stdout
    assert "Professor Nonexistent" in result.stdout


def test_limit_reached_smoke_script_runs_without_pythonpath():
    result = _run_script_without_pythonpath("scripts/smoke_limit_reached.py")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "all assertions passed" in result.stdout
