from __future__ import annotations

from pathlib import Path

from app.data.restriction_timeline import (
    EvidenceStatus,
    RestrictionEligibility,
    RestrictionEvidenceBundle,
    RestrictionEvent,
    RestrictionQuery,
    RestrictionType,
    classify_restriction_type,
    extract_main_content_blocks,
    parse_restriction_timeline,
    select_query_focused_blocks,
)

FIXTURES = Path(__file__).parent / "fixtures" / "websoc"


def test_restriction_query_and_bundle_are_json_ready() -> None:
    query = RestrictionQuery(
        term="2026 Fall",
        department="I&C SCI",
        restriction_type=RestrictionType.SCHOOL_MAJOR,
        student_major="CSE",
        student_school="Engineering",
    )
    event = RestrictionEvent(
        event_id="event-1",
        restriction_type=RestrictionType.SCHOOL_MAJOR,
        action="removed",
        effective_at="2026-09-18T12:00:00-07:00",
        term="2026 Fall",
        department="I&C SCI",
        audience=("all campus majors",),
        exceptions=({"course_id": "I&C SCI 139W"},),
        source_url="https://ics.uci.edu/course-enrollment-restrictions/",
        source_role="ics_undergraduate_restrictions",
    )
    bundle = RestrictionEvidenceBundle(
        query=query,
        events=[event],
        primary_event_id=event.event_id,
        eligibility=RestrictionEligibility(
            eligible=True,
            student_major="CSE",
            student_school="Engineering",
            allowed_groups=("CSE",),
            reason="CSE is explicitly listed.",
            evidence_event_id=event.event_id,
        ),
        evidence_status=EvidenceStatus.VERIFIED,
    )

    payload = bundle.to_dict()

    assert payload["query"]["restriction_type"] == "school_major"
    assert payload["events"][0]["effective_at"] == "2026-09-18T12:00:00-07:00"
    assert payload["events"][0]["exceptions"] == [{"course_id": "I&C SCI 139W"}]
    assert payload["eligibility"]["student_school"] == "Engineering"
    assert bundle.primary_event == event


def test_restriction_type_classifier_keeps_major_and_nor_distinct() -> None:
    assert classify_restriction_type("ICS 专业限制什么时候解除？") == (
        RestrictionType.SCHOOL_MAJOR
    )
    assert classify_restriction_type("When are New Only Restrictions removed?") == (
        RestrictionType.NEW_ONLY
    )
    assert classify_restriction_type(
        "ICS 45C 什么时候开放？",
        has_course=True,
    ) == RestrictionType.COURSE_SPECIFIC
    assert classify_restriction_type("ICS 限制什么时候解除？") == (
        RestrictionType.AMBIGUOUS
    )


def test_main_content_extraction_excludes_navigation_and_keeps_late_timeline() -> None:
    html = (FIXTURES / "ics_restrictions_live_layout.html").read_text()

    blocks = extract_main_content_blocks(html)
    text = "\n".join(block["text"] for block in blocks)

    assert "Admissions Programs Research" not in text
    assert "repeated footer" not in text
    assert "I&C SCI courses" in text
    assert "9/18/2026" in text
    assert "ICS 139W remains restricted" in text


def test_cross_line_timeline_keeps_major_and_nor_as_separate_events() -> None:
    html = (FIXTURES / "ics_restrictions_live_layout.html").read_text()
    blocks = extract_main_content_blocks(html)

    events = parse_restriction_timeline(
        blocks,
        term="2026 Fall",
        department="I&C SCI",
        source_url="https://ics.uci.edu/course-enrollment-restrictions/",
        source_role="ics_undergraduate_restrictions",
        retrieved_at="2026-07-23T12:00:00Z",
    )

    nor = next(event for event in events if event.restriction_type == RestrictionType.NEW_ONLY)
    major = next(
        event
        for event in events
        if event.restriction_type == RestrictionType.SCHOOL_MAJOR
        and event.department == "I&C SCI"
    )

    assert nor.effective_at == "2026-09-01T12:00:00-07:00"
    assert major.effective_at == "2026-09-18T12:00:00-07:00"
    assert major.audience == ("all campus majors, except",)
    assert major.exceptions[0]["course_id"] == "I&C SCI 139W"
    assert any(
        event.department == "COMPSCI"
        and event.effective_at == "2026-09-20T09:00:00-07:00"
        for event in events
    )


def test_query_focused_selection_keeps_date_time_and_exception_neighbors() -> None:
    html = (FIXTURES / "ics_restrictions_live_layout.html").read_text()
    blocks = extract_main_content_blocks(html)
    query = RestrictionQuery(
        term="2026 Fall",
        department="I&C SCI",
        restriction_type=RestrictionType.SCHOOL_MAJOR,
    )

    selected = select_query_focused_blocks(blocks, query, neighbor_count=3)
    text = "\n".join(block["text"] for block in selected)

    assert "I&C SCI courses" in text
    assert "9/18/2026" in text
    assert "12:00pm" in text
    assert "School/Major restrictions are removed" in text
    assert "ICS 139W remains restricted" in text
