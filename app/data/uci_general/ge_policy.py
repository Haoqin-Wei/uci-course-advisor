"""
UCI Undergraduate General Education Policy.

Encodes the 10 GE categories required for a UCI bachelor's degree.
This is **frozen policy data** — UCI's GE structure has been stable
since the 2009 revision. Each individual course's GE tags live in
the Anteater /courses API (already in courses.csv as `ge_list`,
normalized to short codes via _GE_LONG_TO_SHORT in db.py).

This module owns the *totals* — how many courses in each category
the student must complete.

Reference: https://catalogue.uci.edu/informationforadmittedstudents/
           requirementsforabachelorsdegree/#generaleducationtext
"""
from __future__ import annotations

from typing import TypedDict


class GECategory(TypedDict):
    code: str           # "GE-II"
    name: str           # "Science and Technology"
    min_courses: int    # 3
    ap_exempt: bool     # True if a high HS record can waive
    notes: str          # human-readable nuance


# ── The 10 categories ───────────────────────────────────────

GE_CATEGORIES: dict[str, GECategory] = {
    "GE-Ia": {
        "code": "GE-Ia",
        "name": "Lower-Division Writing",
        "min_courses": 2,   # catalogue: "two lower-division plus one upper-division course"
        "ap_exempt": True,
        "notes": (
            "Typically WRITING 39A→39B→39C sequence, or 1 quarter of accelerated "
            "WRITING 60. AP English Language/Composition score of 4+ waives 39A. "
            "Must be completed by end of 7th quarter at UCI."
        ),
    },
    "GE-Ib": {
        "code": "GE-Ib",
        "name": "Upper-Division Writing",
        "min_courses": 1,
        "ap_exempt": False,
        "notes": (
            "Upper-division writing course in your major's discipline. "
            "For CS this is ICS 139W. Required for graduation; no AP waiver."
        ),
    },
    "GE-II": {
        "code": "GE-II",
        "name": "Science and Technology",
        "min_courses": 3,
        "ap_exempt": False,
        "notes": "Three courses in physical/biological sciences, engineering, or computing.",
    },
    "GE-III": {
        "code": "GE-III",
        "name": "Social and Behavioral Sciences",
        "min_courses": 3,
        "ap_exempt": False,
        "notes": "Three courses across at least two departments (psych, econ, polisci, etc.).",
    },
    "GE-IV": {
        "code": "GE-IV",
        "name": "Arts and Humanities",
        "min_courses": 3,
        "ap_exempt": False,
        "notes": "Three courses across at least two departments.",
    },
    "GE-Va": {
        "code": "GE-Va",
        "name": "Quantitative Literacy",
        "min_courses": 1,
        "ap_exempt": True,
        "notes": (
            "One quantitative-reasoning course. AP Calc AB/BC ≥3 waives. "
            "For STEM majors typically auto-satisfied by major math requirements."
        ),
    },
    "GE-Vb": {
        "code": "GE-Vb",
        "name": "Formal Reasoning",
        "min_courses": 1,
        "ap_exempt": False,
        "notes": "Formal/symbolic reasoning. For CS majors typically satisfied by ICS 6B/6D.",
    },
    "GE-VI": {
        "code": "GE-VI",
        "name": "Language Other than English",
        "min_courses": 1,
        "ap_exempt": True,
        "notes": (
            "1 year of college-level non-English language (or equiv). "
            "Waived with 3+ years high school foreign language, or AP language score 3+, "
            "or proof of non-English K-12 schooling."
        ),
    },
    "GE-VII": {
        "code": "GE-VII",
        "name": "Multicultural Studies",
        "min_courses": 1,
        "ap_exempt": False,
        "notes": "One course examining racial/ethnic diversity in the United States.",
    },
    "GE-VIII": {
        "code": "GE-VIII",
        "name": "International/Global Issues",
        "min_courses": 1,
        "ap_exempt": False,
        "notes": "One course examining societies/cultures outside the United States.",
    },
}


# ── Helpers ─────────────────────────────────────────────────

def all_categories() -> list[GECategory]:
    """Return all 10 categories as a list, in display order."""
    return list(GE_CATEGORIES.values())


def get_category(code: str) -> GECategory | None:
    """Look up a category by short code ('GE-II', 'GE-Vb', etc)."""
    return GE_CATEGORIES.get(code)


def total_courses_required(
    *,
    ap_calculus: bool = False,
    ap_english: bool = False,
    hs_foreign_language: bool = False,
) -> int:
    """
    Sum of min_courses across all categories, minus AP exemptions.

    Defaults to 'no AP credit' (16 courses). With common exemptions, drops to ~13.
    Useful for the major-progress UI.
    """
    total = sum(cat["min_courses"] for cat in GE_CATEGORIES.values())
    if ap_english:
        total -= 1                  # waives GE-Ia
    if ap_calculus:
        total -= 1                  # waives GE-Va
    if hs_foreign_language:
        total -= 1                  # waives GE-VI
    return total


def progress(completed_ge_courses_by_category: dict[str, int]) -> dict[str, dict]:
    """
    Compute per-category progress given a dict of {ge_code: count_completed}.

    Returns:
        {
          "GE-Ia": {"completed": 1, "required": 1, "status": "done"},
          "GE-II": {"completed": 2, "required": 3, "status": "in_progress"},
          ...
        }
    """
    out: dict[str, dict] = {}
    for code, cat in GE_CATEGORIES.items():
        done = completed_ge_courses_by_category.get(code, 0)
        req  = cat["min_courses"]
        if done >= req:
            status = "done"
        elif done > 0:
            status = "in_progress"
        else:
            status = "not_started"
        out[code] = {"completed": done, "required": req, "status": status,
                     "name": cat["name"]}
    return out