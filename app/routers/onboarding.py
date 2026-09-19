"""
Onboarding-data API.

Read-only endpoints the frontend wizard hits to build its dropdowns
and course-cards. All data ultimately comes from the Anteater API
(programs / GE endpoints) — we cache aggressively in-process because
this catalogue moves on the order of quarters.

Endpoints:
    GET /api/onboarding/schools
        List of {slug, name} for the school-pick step. Static —
        comes from app.data.uci_general.colleges.

    GET /api/onboarding/majors[?school=<slug>&division=Undergraduate&type=B.S.]
        Filtered + alphabetized program list. Default filters favor
        the common case: undergrad B.S./B.A. for the wizard.

    GET /api/onboarding/major/{program_id}/requirements
        Full requirement tree for one major. Each leaf carries a
        `courses: [...]` array the frontend renders as toggleable cards.

    GET /api/onboarding/ge
        Full GE tree, sorted I → VIII at the top level.

    GET /api/onboarding/departments
        Full list of UCI departments registered with WebSoc (~180
        entries), alphabetical by name. Powers the Phase C wizard's
        department index in Step 4.

    GET /api/onboarding/courses?department=<deptCode>
        Every course offered by the department, paginated server-side
        through Anteater so the client just sees one slim array.

Auth note: P1-1b will add an auth_required dependency. For now these
read-only endpoints are public — the data is published anyway.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.data.uci_general import anteater_programs, colleges

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


# ── Schools ──────────────────────────────────────────────

@router.get("/schools")
def list_schools():
    """Static list of UCI schools in canonical display order."""
    return {"schools": colleges.SCHOOLS}


# ── Majors ───────────────────────────────────────────────

@router.get("/majors")
def list_majors(
    school: Optional[str] = Query(None, description="School slug to filter by"),
    division: Optional[str] = Query("Undergraduate"),
    type: Optional[str] = Query(None, description="Degree type like 'B.S.', 'B.A.'"),
):
    """
    Sorted alphabetically by name. Default division=Undergraduate
    because the wizard's school+major pickers only care about
    bachelor's programs; pass division=Graduate or empty to override.

    The `school` filter best-matches by checking whether the program's
    name contains the school's distinctive keyword(s). UCI's API
    doesn't expose a school field on programs, so this is a heuristic.
    It's tuned so common ICS / Engineering / Bio queries return the
    right set; less-common schools may need refinement.
    """
    out = anteater_programs.majors(division=division, type_filter=type)

    if school:
        school_meta = colleges.school_by_slug(school)
        if not school_meta:
            raise HTTPException(status_code=400, detail=f"Unknown school: {school}")
        out = [m for m in out if _major_belongs_to_school(m, school)]

    return {
        "count":    len(out),
        "school":   school,
        "division": division,
        "type":     type,
        "majors":   out,
    }


# Heuristic name patterns per school. Order matters for "Computer
# Science and Engineering" (could match both ICS and Engineering — we
# put it in Engineering because it's an EECS program).
_SCHOOL_NAME_KEYWORDS: dict[str, list[str]] = {
    "ics": [
        "computer science",
        "informatics",
        "software engineering",
        "data science",
        "information and computer science",
        "business information management",
        "game",
        "human-computer interaction",
    ],
    "engineering": [
        "aerospace engineering",
        "biomedical engineering",
        "chemical engineering",
        "civil engineering",
        "computer engineering",
        "computer science and engineering",
        "electrical engineering",
        "environmental engineering",
        "materials science",
        "mechanical engineering",
        "engineering",
    ],
    "physical": [
        "chemistry", "math", "physics", "earth", "earth system",
    ],
    "biological": [
        "biology", "biological", "biochem", "ecology", "evolution",
        "developmental", "molecular biology", "neuroscience",
        "neurobiology", "microbiology",
    ],
    "social": [
        "anthropology", "cognitive", "economics", "international",
        "political science", "psychology", "sociology", "linguistics",
        "language science", "social sciences",
    ],
    "humanities": [
        "history", "philosophy", "english", "writing", "religious",
        "literature", "art history", "asian", "european", "global",
        "german", "french", "spanish", "italian", "chinese", "japanese",
        "korean", "russian", "classics", "humanities", "gender",
        "women", "african american", "film",
    ],
    "arts": [
        "art history",  # NB: also in humanities — first match wins; tune ordering above if needed
        "art", "dance", "drama", "music",
    ],
    "business": [
        "business administration", "business information", "accounting",
        "management",
    ],
    "education": [
        "education",
    ],
    "ecology": [
        "criminology", "law", "public policy", "urban", "social ecology",
        "planning",
    ],
    "health": [
        "nursing", "pharmaceutical", "public health", "health science",
    ],
}


def _major_belongs_to_school(major: dict, school_slug: str) -> bool:
    """True if the program's name suggests it lives in this school."""
    name = (major.get("name") or "").lower()
    for kw in _SCHOOL_NAME_KEYWORDS.get(school_slug, []):
        if kw in name:
            return True
    return False


# ── Single major requirements ────────────────────────────

@router.get("/major/{program_id}/requirements")
def major_requirements(program_id: str):
    """Full requirement tree for one major. 404 if the id is unknown."""
    data = anteater_programs.major(program_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Unknown program: {program_id}")
    return data


# ── GE ───────────────────────────────────────────────────

@router.get("/ge")
def ge_requirements():
    """
    GE requirements tree, top-level sorted I → VIII. Each leaf has a
    `courses: [...]` array of course IDs the frontend renders as
    selectable cards.
    """
    data = anteater_programs.ge_requirements()
    if not data:
        raise HTTPException(status_code=503, detail="GE data unavailable")
    return data


# ── Departments + courses ────────────────────────────────

@router.get("/departments")
def list_departments():
    """All WebSoc departments, sorted alphabetically by name."""
    depts = anteater_programs.departments()
    return {"count": len(depts), "departments": depts}


@router.get("/courses")
def list_courses(department: str = Query(..., description="Dept code (e.g. 'COMPSCI', 'I&C SCI')")):
    """
    Slim course list for one department: id / courseNumber / title /
    courseLevel / units. Wraps Anteater's paginated /courses behind a
    single response — the wizard doesn't need to know about take/skip.
    """
    courses = anteater_programs.list_courses_by_department(department)
    return {
        "department": department,
        "count":      len(courses),
        "courses":    courses,
    }


@router.get("/courses/all")
def list_all_courses():
    """
    Full slim UCI catalog for the Step 4 course picker. The data module
    prefers the checked-in local catalog, then a restart-safe runtime cache,
    and only uses the paginated API as a last resort.
    """
    courses = anteater_programs.list_all_courses()
    return {
        "count": len(courses),
        "source": anteater_programs.all_courses_source(),
        "courses": courses,
    }
