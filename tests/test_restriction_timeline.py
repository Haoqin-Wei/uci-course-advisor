from __future__ import annotations

from app.data.restriction_timeline import (
    EvidenceStatus,
    RestrictionEligibility,
    RestrictionEvidenceBundle,
    RestrictionEvent,
    RestrictionQuery,
    RestrictionType,
    classify_restriction_type,
)


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
