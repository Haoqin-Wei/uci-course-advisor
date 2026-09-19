from __future__ import annotations

from pathlib import Path

from app.data.restriction_timeline import (
    EvidenceStatus,
    RestrictionEligibility,
    RestrictionEvidenceBundle,
    RestrictionEvent,
    RestrictionQuery,
    RestrictionType,
    build_restriction_evidence_bundle,
    build_verified_restriction_facts,
    classify_restriction_type,
    compact_linked_restriction_result,
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
        and event.effective_at
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
    assert not any(
        event.department == "I&C SCI"
        and "STATS" in event.statement
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


def test_evidence_gate_selects_major_as_primary_and_keeps_nor_related() -> None:
    html = (FIXTURES / "ics_restrictions_live_layout.html").read_text()
    events = parse_restriction_timeline(
        extract_main_content_blocks(html),
        term="2026 Fall",
        department="I&C SCI",
        source_url="https://ics.uci.edu/course-enrollment-restrictions/",
        source_role="ics_undergraduate_restrictions",
        retrieved_at="2026-07-23T12:00:00Z",
    )
    query = RestrictionQuery(
        term="2026 Fall",
        department="I&C SCI",
        restriction_type=RestrictionType.SCHOOL_MAJOR,
        student_major="CSE",
    )
    websoc = {
        "source_url": "https://www.reg.uci.edu/perl/WebSoc",
        "retrieved_at": "2026-07-23T11:59:00Z",
        "fields": {},
    }
    linked = {
        "pages": [
            {
                "url": "https://ics.uci.edu/course-enrollment-restrictions/",
                "link_role": "ics_undergraduate_restrictions",
                "retrieved_at": "2026-07-23T12:00:00Z",
                "timeline_events": [event.to_dict() for event in events],
            }
        ]
    }

    bundle = build_restriction_evidence_bundle(
        query,
        websoc_result=websoc,
        linked_result=linked,
    )
    facts = build_verified_restriction_facts(bundle)

    assert bundle.evidence_status == EvidenceStatus.VERIFIED
    assert bundle.primary_event is not None
    assert bundle.primary_event.restriction_type == RestrictionType.SCHOOL_MAJOR
    assert bundle.primary_event.effective_at == "2026-09-18T12:00:00-07:00"
    assert bundle.primary_event.exceptions[0]["course_id"] == "I&C SCI 139W"
    assert any(
        event.restriction_type == RestrictionType.NEW_ONLY
        and event.event_id in bundle.related_event_ids
        for event in bundle.events
    )
    assert "2026-09-18 12:00" in facts["summary_markdown"]
    assert "2026-09-01 12:00" in facts["summary_markdown"]
    assert "I&C SCI 139W" in facts["summary_markdown"]
    assert "reg.uci.edu" in facts["summary_markdown"]
    assert "ics.uci.edu" in facts["summary_markdown"]


def test_ambiguous_evidence_returns_one_major_and_one_nor_event() -> None:
    html = (FIXTURES / "ics_restrictions_live_layout.html").read_text()
    events = parse_restriction_timeline(
        extract_main_content_blocks(html),
        term="2026 Fall",
        department="I&C SCI",
        source_url="https://ics.uci.edu/course-enrollment-restrictions/",
        source_role="ics_undergraduate_restrictions",
        retrieved_at="2026-07-23T12:00:00Z",
    )
    bundle = build_restriction_evidence_bundle(
        RestrictionQuery(
            term="2026 Fall",
            department="I&C SCI",
            restriction_type=RestrictionType.AMBIGUOUS,
        ),
        websoc_result={"source_url": "https://www.reg.uci.edu/perl/WebSoc"},
        linked_result={
            "pages": [
                {
                    "url": "https://ics.uci.edu/course-enrollment-restrictions/",
                    "timeline_events": [event.to_dict() for event in events],
                }
            ]
        },
    )
    events_by_id = {event.event_id: event for event in bundle.events}
    returned_types = [
        events_by_id[bundle.primary_event_id].restriction_type,
        *[
            events_by_id[event_id].restriction_type
            for event_id in bundle.related_event_ids
        ],
    ]

    assert returned_types.count(RestrictionType.SCHOOL_MAJOR) == 1
    assert returned_types.count(RestrictionType.NEW_ONLY) == 1


def test_evidence_gate_marks_conflicts_and_unavailable_without_guessing() -> None:
    query = RestrictionQuery(
        term="2026 Fall",
        department="I&C SCI",
        restriction_type=RestrictionType.SCHOOL_MAJOR,
    )
    event = RestrictionEvent(
        event_id="major-1",
        restriction_type=RestrictionType.SCHOOL_MAJOR,
        action="removed",
        effective_at="2026-09-18T12:00:00-07:00",
        term="2026 Fall",
        department="I&C SCI",
        audience=("all campus majors",),
        source_url="https://ics.uci.edu/restrictions-a/",
        statement="School/Major restrictions are removed.",
    )
    conflicting = RestrictionEvent(
        **{
            **event.__dict__,
            "event_id": "major-2",
            "effective_at": "2026-09-20T12:00:00-07:00",
            "source_url": "https://ics.uci.edu/restrictions-b/",
        }
    )
    linked = {
        "pages": [
            {
                "url": event.source_url,
                "timeline_events": [event.to_dict()],
            },
            {
                "url": conflicting.source_url,
                "timeline_events": [conflicting.to_dict()],
            },
        ]
    }

    bundle = build_restriction_evidence_bundle(
        query,
        websoc_result={"source_url": "https://www.reg.uci.edu/perl/WebSoc"},
        linked_result=linked,
    )
    unavailable = build_restriction_evidence_bundle(
        query,
        websoc_result={
            "source_url": "https://www.reg.uci.edu/perl/WebSoc",
            "fields": {},
        },
    )

    assert bundle.evidence_status == EvidenceStatus.CONFLICTING
    assert bundle.conflicts[0]["field"] == "effective_at"
    assert unavailable.evidence_status == EvidenceStatus.UNAVAILABLE
    assert unavailable.missing_required_fields == ["effective_at", "source_url"]
    assert "未能从已抓取的官方来源验证" in (
        build_verified_restriction_facts(unavailable)["summary_markdown"]
    )


def test_eligibility_uses_explicit_cse_group_and_course_exception_wins() -> None:
    event = RestrictionEvent(
        event_id="major-1",
        restriction_type=RestrictionType.SCHOOL_MAJOR,
        action="active",
        effective_at="2026-08-01T12:00:00-07:00",
        term="2026 Fall",
        department="I&C SCI",
        audience=("School of ICS, CSE, and Computer Engineering",),
        exceptions=(
            {
                "course_id": "I&C SCI 139W",
                "text": "I&C SCI 139W remains separately restricted",
            },
        ),
        source_url="https://ics.uci.edu/restrictions/",
        statement=(
            "Courses are restricted to School of ICS, CSE, "
            "and Computer Engineering."
        ),
    )
    linked = {
        "pages": [
            {
                "url": event.source_url,
                "timeline_events": [event.to_dict()],
            }
        ]
    }
    general = build_restriction_evidence_bundle(
        RestrictionQuery(
            term="2026 Fall",
            department="I&C SCI",
            restriction_type=RestrictionType.SCHOOL_MAJOR,
            student_major="CSE",
        ),
        websoc_result={},
        linked_result=linked,
    )
    exception = build_restriction_evidence_bundle(
        RestrictionQuery(
            term="2026 Fall",
            department="I&C SCI",
            course_id="I&C SCI 139W",
            restriction_type=RestrictionType.SCHOOL_MAJOR,
            student_major="CSE",
        ),
        websoc_result={},
        linked_result=linked,
    )

    assert general.eligibility is not None
    assert general.eligibility.eligible is True
    assert "明确把 CSE 列入" in general.eligibility.reason
    assert "CSE 是 School of ICS" not in general.eligibility.reason
    assert exception.eligibility is not None
    assert exception.eligibility.eligible is None
    assert "一般规则的例外" in exception.eligibility.reason


def test_compact_linked_result_removes_page_prose() -> None:
    compact = compact_linked_restriction_result(
        {
            "ok": True,
            "pages": [
                {
                    "url": "https://ics.uci.edu/restrictions/",
                    "text_excerpt": "full fetched prose",
                    "relevant_passages": ["more prose"],
                    "links": [{"url": "https://example.com"}],
                    "timeline_events": [{"event_id": "one"}],
                }
            ],
        }
    )

    assert compact["pages"][0]["timeline_events"] == [{"event_id": "one"}]
    assert "text_excerpt" not in compact["pages"][0]
    assert "relevant_passages" not in compact["pages"][0]
    assert "links" not in compact["pages"][0]
