"""Offline smoke test for app.data.db.

Run from the project root:

    python scripts/verify_db.py

The script intentionally uses only local catalog data. It exits non-zero
if the current DB envelope API no longer returns the expected shape.
"""

from __future__ import annotations

try:
    from scripts._bootstrap import ensure_repo_root_on_path
except ModuleNotFoundError:  # direct execution: python scripts/verify_db.py
    from _bootstrap import ensure_repo_root_on_path

ensure_repo_root_on_path()

from app.data import db


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"❌ {message}")


def main() -> None:
    section("1. get_course_info('CS122A')")
    course = db.get_course_info("CS122A")
    require(course.get("found") is True, course.get("reason", "CS122A not found"))
    payload = course["course"]
    print(f"✅ {payload['course_id']}: {payload['title']} ({payload['units']} units)")
    print(f"   source={course['source']} prereq={payload.get('prerequisite_text') or 'n/a'}")

    section("2. search_courses(term='Spring 2025', department='COMPSCI')")
    search = db.search_courses(term="Spring 2025", department="COMPSCI")
    require(search.get("found") is True, search.get("reason", "search failed"))
    require(search.get("total_found", 0) > 0, "expected COMPSCI courses in Spring 2025")
    print(f"✅ found {search['total_found']} COMPSCI courses")
    print(f"   first 5: {[item['course_id'] for item in search['courses'][:5]]}")

    section("3. get_sections('CS161', 'Spring 2025')")
    sections = db.get_sections("CS161", "Spring 2025")
    require(sections.get("found") is True, sections.get("reason", "CS161 sections missing"))
    print(
        f"✅ found {len(sections['sections'])} section rows "
        f"(coverage={sections.get('coverage_status')}, source={sections.get('source')})"
    )
    for row in sections["sections"][:3]:
        instructors = ", ".join(row.get("instructors") or []) or "TBA"
        print(
            "   "
            f"{row.get('section_type')} {row.get('section_num')} "
            f"{row.get('time_display') or row.get('days') or 'TBA'} "
            f"{instructors}"
        )

    section("4. check_prerequisites_met('CS122A', completed=['ICS 33'])")
    prereq = db.check_prerequisites_met("CS122A", completed_courses=["ICS 33"])
    require(prereq.get("found") is True, prereq.get("reason", "prereq lookup failed"))
    print(f"✅ status={prereq.get('status')} met={prereq.get('met')}")
    print(f"   missing={prereq.get('missing')} unknown={prereq.get('unknown')}")

    print("\n✅ db smoke passed")


if __name__ == "__main__":
    main()
