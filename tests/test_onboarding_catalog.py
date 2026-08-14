from __future__ import annotations

import csv

from app.data.uci_general import anteater_programs


def test_whole_catalog_prefers_local_csv_without_network(monkeypatch, tmp_path) -> None:
    local_path = tmp_path / "courses.csv"
    with local_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "course_id",
                "department",
                "course_number",
                "course_numeric",
                "title",
                "min_units",
                "max_units",
                "course_level",
            ],
        )
        writer.writeheader()
        writer.writerow({
            "course_id": "COMPSCI_161",
            "department": "COMPSCI",
            "course_number": "161",
            "course_numeric": "161",
            "title": "Design and Analysis of Algorithms",
            "min_units": "4",
            "max_units": "4",
            "course_level": "Upper Division (100-199)",
        })
        writer.writerow({
            "course_id": "I&C_SCI_33",
            "department": "I&C SCI",
            "course_number": "33",
            "course_numeric": "33",
            "title": "Intermediate Programming",
            "min_units": "4",
            "max_units": "4",
            "course_level": "Lower Division (1-99)",
        })

    monkeypatch.setattr(anteater_programs, "LOCAL_COURSES_PATH", local_path)
    monkeypatch.setattr(
        anteater_programs,
        "ALL_COURSES_DISK_CACHE_PATH",
        tmp_path / "runtime-cache.json",
    )
    monkeypatch.setattr(anteater_programs, "_all_courses_cache", None)
    monkeypatch.setattr(anteater_programs, "_all_courses_source", None)

    def fail_network(*_args, **_kwargs):
        raise AssertionError("local catalogue must not call Anteater")

    monkeypatch.setattr(anteater_programs, "_get", fail_network)

    courses = anteater_programs.list_all_courses()

    assert [course["id"] for course in courses] == ["CS161", "ICS33"]
    assert courses[0]["minUnits"] == 4
    assert anteater_programs.all_courses_source() == "local_csv"


def test_whole_catalog_reuses_restart_safe_cache(monkeypatch, tmp_path) -> None:
    cache_path = tmp_path / "onboarding-courses.json"
    cache_path.write_text(
        '{"schema_version":1,"courses":[{"id":"MATH2A","title":"Calculus"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        anteater_programs,
        "LOCAL_COURSES_PATH",
        tmp_path / "missing.csv",
    )
    monkeypatch.setattr(
        anteater_programs,
        "ALL_COURSES_DISK_CACHE_PATH",
        cache_path,
    )
    monkeypatch.setattr(anteater_programs, "_all_courses_cache", None)
    monkeypatch.setattr(anteater_programs, "_all_courses_source", None)

    def fail_network(*_args, **_kwargs):
        raise AssertionError("disk cache must prevent an Anteater crawl")

    monkeypatch.setattr(anteater_programs, "_get", fail_network)

    assert anteater_programs.list_all_courses() == [
        {"id": "MATH2A", "title": "Calculus"}
    ]
    assert anteater_programs.all_courses_source() == "runtime_cache"
