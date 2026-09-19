"""
Department canonical names and aliases.

Source: enumerated from real UCI data (the cleaned xlsx) plus common
user shorthand. Maintain this list when new departments appear in
freshly-pulled data.

Conventions:
  - Canonical = UCI registrar's department string, uppercased,
    spaces/slashes/ampersands preserved (e.g. "SOC SCI", "CRM/LAW").
  - Aliases match case-insensitively. Exact match only — no substring
    matching, since e.g. "CS" should NOT match "PSCI".
"""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path


# canonical_dept → list of accepted aliases (in addition to canonical itself)
DEPARTMENT_ALIASES: dict[str, list[str]] = {
    # ── Engineering / CS / Info ──
    "COMPSCI":  ["cs", "compsci", "comp sci", "computer science"],
    "I&C SCI":  ["ics", "i&c sci", "i and c sci", "icssci"],
    "IN4MATX":  ["informatics", "inf", "in4matx"],
    "EECS":     ["eecs", "electrical eng", "ee"],
    "ENGRMAE":  ["mae", "engrmae", "mech eng", "mechanical engineering"],
    "ENGRCEE":  ["cee", "engrcee", "civil eng", "civil engineering"],

    # ── Math / Sciences ──
    "MATH":     ["math", "mathematics"],
    "STATS":    ["stats", "stat", "statistics"],
    "PHYSICS":  ["physics", "phys"],
    "CHEM":     ["chem", "chemistry"],
    "BIO SCI":  ["bio", "biosci", "bio sci", "biology", "bio science"],
    "PHRMSCI":  ["phrmsci", "pharmaceutical science"],
    "PHARM":    ["pharm"],
    "PHMD":     ["phmd"],
    "PSCI":     ["psci"],

    # ── Social Sciences ──
    "PSYCH":    ["psych", "psychology"],
    "SOC SCI":  ["soc sci", "social science", "social sci", "socsci"],
    "SOCIOL":   ["sociol", "sociology"],
    "POL SCI":  ["pol sci", "political science", "polsci", "poli sci"],
    "ECON":     ["econ", "economics"],
    "ANTHRO":   ["anthro", "anthropology"],
    "CRM/LAW":  ["crm/law", "crm law", "criminology", "crim"],

    # ── Humanities / Arts ──
    "WRITING":  ["writing", "wr"],
    "HUMAN":    ["human"],
    "SPANISH":  ["spanish", "span"],
    "FRENCH":   ["french"],
    "RUSSIAN":  ["russian"],
    "ART":      ["art"],
    "DANCE":    ["dance"],
    "FLM&MDA":  ["flm&mda", "film & media", "film and media"],

    # ── Pro schools / Other ──
    "PUBHLTH":  ["pubhlth", "public health"],
    "NUR SCI":  ["nur sci", "nursing", "nursci"],
    "EDUC":     ["educ", "education"],
    "MGMT":     ["mgmt", "management"],
    "UPPP":     ["uppp", "urban planning"],
    "SOCECOL":  ["socecol", "social ecology"],
    "UNI AFF":  ["uni aff"],
    "UNI STU":  ["uni stu", "university studies"],
}


# Build reverse lookup: alias_lowercased → canonical
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canonical, _aliases in DEPARTMENT_ALIASES.items():
    _ALIAS_TO_CANONICAL[_canonical.lower()] = _canonical
    for _a in _aliases:
        _ALIAS_TO_CANONICAL[_a.lower()] = _canonical


@lru_cache(maxsize=1)
def _catalog_department_lookup() -> dict[str, str]:
    """Load registrar department codes from the bundled UCI catalog.

    The alias table is intentionally small and conversational. It must not be
    used as an allow-list for structured registrar data, otherwise legitimate
    departments disappear whenever the hand-maintained aliases lag the
    catalog.
    """
    path = Path(__file__).resolve().parents[2] / "data" / "uci" / "courses.csv"
    if not path.exists():
        return {}
    lookup: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                canonical = " ".join((row.get("department") or "").split()).upper()
                if canonical:
                    lookup[canonical.lower()] = canonical
    except OSError:
        return {}
    return lookup


def resolve_department(text: str) -> str | None:
    """Map any known alias to canonical dept. Returns None if unknown.

    Whitespace is collapsed; case is ignored.
    """
    if not text:
        return None
    normalized = " ".join(text.split()).lower()
    return _ALIAS_TO_CANONICAL.get(normalized) or _catalog_department_lookup().get(normalized)


def known_departments() -> set[str]:
    """All canonical department names."""
    return set(DEPARTMENT_ALIASES.keys()) | set(_catalog_department_lookup().values())


# ── Colloquial course-ID form ────────────────────────────
#
# Real UCI / Anteater API form has multi-word depts with spaces or
# special chars ('I&C SCI 33', 'SOC SCI 178C'). But students and LLMs
# naturally write the shorter colloquial form ('ICS33', 'SOCSCI178C'),
# and our existing chat.py regex `_COURSE_ID_SCAN` also captures that
# short form. Use this mapping when producing course IDs that flow
# through LLM output / frontend.

COLLOQUIAL_PREFIX: dict[str, str] = {
    "COMPSCI":   "CS",
    "I&C SCI":   "ICS",
    "SOC SCI":   "SOCSCI",
    "POL SCI":   "POLSCI",
    "BIO SCI":   "BIOSCI",
    "NUR SCI":   "NURSCI",
    "UNI AFF":   "UNIAFF",
    "UNI STU":   "UNISTU",
    "CRM/LAW":   "CRMLAW",
    "FLM&MDA":   "FLMMDA",
    # Single-word depts use their canonical form unchanged
    # (MATH, STATS, COMPSCI, PSYCH, ECON, etc.)
}


def colloquial_course_id(department: str, course_number: str) -> str:
    """
    Build a colloquial, LLM-friendly course ID from canonical
    (department, course_number).

    Examples:
        ('COMPSCI', '122A')   → 'CS122A'
        ('I&C SCI', '33')     → 'ICS33'
        ('MATH', '2D')        → 'MATH2D'
        ('SOC SCI', '178C')   → 'SOCSCI178C'
        ('CRM/LAW', 'C214')   → 'CRMLAWC214'
    """
    if not department or not course_number:
        return ""
    prefix = COLLOQUIAL_PREFIX.get(department)
    if prefix is None:
        # Default: strip spaces, &, / from canonical dept
        prefix = (
            department.replace(" ", "").replace("&", "").replace("/", "")
        )
    return f"{prefix}{course_number}".upper()
