"""Catalog data coverage manifest.

The catalog can answer different questions with different confidence:
course metadata may exist in courses.csv while term sections are partial
or unavailable. This module summarizes the section data build so API
callers and the frontend can tell "no local section row" from "we cannot
confirm this term is complete".
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.catalog.term import Term

SCHEMA_VERSION = "uci-relational-v1"
DATA_DIR = Path("data/uci")

_STALE_AFTER_DAYS = 365


def get_coverage_manifest(data_dir: str | Path = DATA_DIR) -> dict:
    return _get_coverage_manifest_cached(str(Path(data_dir)))


@lru_cache(maxsize=8)
def _get_coverage_manifest_cached(data_dir_str: str) -> dict:
    data_dir = Path(data_dir_str)
    generated_at = datetime.now(timezone.utc).isoformat()
    source_files = _source_file_metadata(data_dir)
    updated_at = _latest_updated_at(source_files)

    by_term: dict[str, dict] = defaultdict(
        lambda: {
            "section_count": 0,
            "course_ids": set(),
            "departments": set(),
        }
    )

    sections_path = data_dir / "sections.csv"
    if sections_path.exists():
        with sections_path.open("r", encoding="utf-8", newline="") as file:
            for row in csv.DictReader(file):
                term_id = (row.get("term_id") or "").strip()
                term = Term.parse(term_id)
                if not term:
                    continue
                bucket = by_term[term.term_id]
                bucket["section_count"] += 1
                if row.get("course_id"):
                    bucket["course_ids"].add(row["course_id"])
                if row.get("department"):
                    bucket["departments"].add(row["department"])

    terms: list[dict] = []
    for term_id, bucket in by_term.items():
        term = Term.parse(term_id)
        if not term:
            continue
        section_count = bucket["section_count"]
        departments = sorted(bucket["departments"])
        status = _coverage_status(section_count, updated_at)
        terms.append({
            "term_id": term.term_id,
            "name": term.display(),
            "coverage_status": status,
            "section_count": section_count,
            "course_count": len(bucket["course_ids"]),
            "department_count": len(departments),
            "departments": departments,
            "source": str(data_dir),
            "updated_at": updated_at,
            "schema_version": SCHEMA_VERSION,
        })

    terms.sort(key=lambda item: _term_sort_key(item["term_id"]), reverse=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "updated_at": updated_at,
        "source": str(data_dir),
        "source_files": source_files,
        "terms": terms,
    }


def get_term_coverage(term: Term, data_dir: str | Path = DATA_DIR) -> dict:
    manifest = get_coverage_manifest(data_dir)
    for item in manifest["terms"]:
        if item["term_id"] == term.term_id:
            return item
    return {
        "term_id": term.term_id,
        "name": term.display(),
        "coverage_status": "unavailable",
        "section_count": 0,
        "course_count": 0,
        "department_count": 0,
        "departments": [],
        "source": str(Path(data_dir)),
        "updated_at": manifest.get("updated_at"),
        "schema_version": SCHEMA_VERSION,
    }


def default_term_name(manifest: Optional[dict] = None) -> Optional[str]:
    manifest = manifest or get_coverage_manifest()
    complete = [
        item for item in manifest["terms"]
        if item.get("coverage_status") == "complete"
    ]
    if complete:
        return complete[0]["name"]
    return manifest["terms"][0]["name"] if manifest["terms"] else None


def _coverage_status(section_count: int, updated_at: Optional[str]) -> str:
    if section_count <= 0:
        return "unavailable"
    if _is_stale(updated_at):
        return "stale"
    # Row volume cannot prove that every department was collected. Until the
    # importer records explicit completion evidence, all snapshots are partial.
    return "partial"


def _is_stale(updated_at: Optional[str]) -> bool:
    if not updated_at:
        return False
    try:
        dt = datetime.fromisoformat(updated_at)
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - dt
    return age.days > _STALE_AFTER_DAYS


def _source_file_metadata(data_dir: Path) -> list[dict]:
    files = []
    for filename in (
        "courses.csv",
        "sections.csv",
        "section_instructors.csv",
        "section_ge.csv",
    ):
        path = data_dir / filename
        if not path.exists():
            continue
        stat = path.stat()
        files.append({
            "path": str(path),
            "bytes": stat.st_size,
            "updated_at": datetime.fromtimestamp(
                stat.st_mtime,
                timezone.utc,
            ).isoformat(),
        })
    return files


def _latest_updated_at(source_files: list[dict]) -> Optional[str]:
    values = [item["updated_at"] for item in source_files if item.get("updated_at")]
    return max(values) if values else None


def _term_sort_key(term_id: str) -> tuple[int, int]:
    term = Term.parse(term_id)
    if not term:
        return (0, 0)
    quarter_order = {"Fall": 0, "Winter": 1, "Spring": 2, "Summer": 3}
    return (term.year, quarter_order.get(term.quarter, 0))
