from __future__ import annotations

import json

from app.catalog.types import CourseRef


def test_minimal_catalog_fixture_matches_production_loader(minimal_catalog):
    course_ids = {row["course_id"] for row in minimal_catalog.course_rows}
    loaded_refs = {course.ref.course_id() for course in minimal_catalog.courses}

    assert minimal_catalog.term.term_id == "2025_Spring"
    assert course_ids == {"COMPSCI_161", "IN4MATX_43", "COMPSCI_199"}
    assert loaded_refs == course_ids
    assert len(minimal_catalog.sections) == 4

    cs161_sections = [
        section
        for section in minimal_catalog.sections
        if section.course == CourseRef("COMPSCI", "161")
    ]
    assert [section.section_type for section in cs161_sections] == ["Lec", "Dis"]
    assert cs161_sections[0].seats_open == 20
    assert cs161_sections[0].instructors == ("TESTER, A.",)

    full_section = next(
        section
        for section in minimal_catalog.sections
        if section.section_id == "2025_Spring_20001"
    )
    assert full_section.status == "FULL"
    assert full_section.ge_categories == ("GE II: Science and Technology",)

    tba_section = next(
        section
        for section in minimal_catalog.sections
        if section.section_id == "2025_Spring_30001"
    )
    assert tba_section.days is None
    assert tba_section.start_time is None
    assert tba_section.final_exam == {"examStatus": "TBA_FINAL"}


def test_seeded_user_fixture_loads_profile_memory_and_session(seeded_user):
    from app.data import sessions
    from app.memory.manager import get_memory_manager

    manager = get_memory_manager()
    manager.initialize_session(seeded_user.session_id, seeded_user.user_id)

    profile = manager.provider.get_profile(seeded_user.user_id)
    preferences = manager.get_preferences(seeded_user.user_id)
    meta = sessions.get_session_meta(
        seeded_user.user_id,
        seeded_user.session_id,
    )
    turns = sessions.read_turns(
        seeded_user.user_id,
        seeded_user.session_id,
    )

    assert profile["major"] == "Computer Science"
    assert profile["completed_courses"] == ["ACENG20A"]
    assert preferences == [
        {
            "id": "pref_morning",
            "text": "Prefers morning classes",
            "learned_at": "2025-01-15T12:00:00+00:00",
            "last_confirmed_at": "2025-01-15T12:00:00+00:00",
        }
    ]
    assert "Prefers morning classes" in manager.system_prompt_block(
        seeded_user.user_id
    )
    assert meta["term_scope"] == "Spring 2025"
    assert meta["turn_count"] == 2
    assert [turn["role"] for turn in turns] == ["user", "assistant"]


def test_seeded_user_fixture_is_copied_before_use(seeded_user):
    copied_profile = seeded_user.user_dir / "profile.json"
    source_profile = seeded_user.source_dir / "profile.json"

    copied_profile.write_text(
        json.dumps({"major": "Changed only in temporary test data"}),
        encoding="utf-8",
    )

    assert json.loads(source_profile.read_text(encoding="utf-8"))["major"] == (
        "Computer Science"
    )
