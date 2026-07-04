from __future__ import annotations

import ast
import inspect

from app.scheduling import (
    calendar_day_names,
    find_conflicts,
    parse_day_codes,
    parse_days,
    section_time_status,
    sections_overlap,
    summarize_for_card,
    time_to_minutes,
    validate_schedule_bundle,
)


def test_scheduling_service_parses_canonical_week_codes_and_calendar_names():
    assert parse_day_codes("MTuWThFSaSu") == (
        "M",
        "Tu",
        "W",
        "Th",
        "F",
        "Sa",
        "Su",
    )
    assert parse_days("TuTh") == {"Tu", "Th"}
    assert calendar_day_names("MWFSaSu") == (
        "Mon",
        "Wed",
        "Fri",
        "Sat",
        "Sun",
    )


def test_scheduling_service_parses_times_and_overlap_statuses():
    lecture = {
        "days": "MW",
        "start_time": "09:00",
        "end_time": "10:20",
        "time_is_tba": "false",
    }
    overlapping = {
        "days": "M",
        "start_time": "10:00",
        "end_time": "10:50",
        "time_is_tba": "false",
    }
    back_to_back = {
        "days": "M",
        "start_time": "10:20",
        "end_time": "11:10",
        "time_is_tba": "false",
    }
    different_day = {
        "days": "Tu",
        "start_time": "09:30",
        "end_time": "10:50",
        "time_is_tba": "false",
    }

    assert time_to_minutes("09:30") == 570
    assert time_to_minutes("24:00") is None
    assert section_time_status(lecture, overlapping) == "conflict"
    assert sections_overlap(lecture, overlapping) is True
    assert section_time_status(lecture, back_to_back) == "clear"
    assert sections_overlap(lecture, back_to_back) is False
    assert section_time_status(lecture, different_day) == "clear"


def test_scheduling_service_marks_tba_or_missing_times_as_unknown():
    known = {
        "days": "MW",
        "start_time": "09:00",
        "end_time": "10:20",
        "time_is_tba": "false",
    }
    tba = {
        "days": "TBA",
        "start_time": "",
        "end_time": "",
        "time_is_tba": "true",
    }
    missing_time = {
        "days": "M",
        "start_time": "",
        "end_time": "",
        "time_is_tba": "false",
    }

    assert section_time_status(known, tba) == "unknown"
    assert section_time_status(known, missing_time) == "unknown"
    assert sections_overlap(known, tba) is False


def test_scheduling_service_finds_and_summarizes_known_conflicts():
    candidate_sections = [
        {
            "section_id": "cand_a",
            "section_code": "10000",
            "days": "MW",
            "start_time": "09:00",
            "end_time": "10:20",
        },
        {
            "section_id": "cand_b",
            "section_code": "10001",
            "days": "Tu",
            "start_time": "09:00",
            "end_time": "10:20",
        },
    ]
    student_sections = [
        {
            "section_id": "student_a",
            "course_id": "IN4MATX43",
            "section_code": "20000",
            "days": "M",
            "start_time": "10:00",
            "end_time": "10:50",
        }
    ]

    conflicts = find_conflicts(candidate_sections, student_sections)

    assert conflicts == {
        "cand_a": [
            {
                "course_id": "IN4MATX43",
                "section_code": "20000",
                "window": "M 10:00–10:50",
            }
        ]
    }
    assert summarize_for_card(candidate_sections, conflicts) == {
        "status": "some",
        "summary": "1 of 2 sections conflict with current schedule",
        "conflicting_count": 1,
        "total_sections": 2,
    }


def test_validate_schedule_bundle_accepts_clear_recommendations():
    result = validate_schedule_bundle(
        [
            {
                "course_id": "COMPSCI161",
                "primary_code": "10000",
                "sections": [
                    {
                        "course_id": "COMPSCI161",
                        "section_code": "10000",
                        "section_num": "A",
                        "days": "MW",
                        "start_time": "09:00",
                        "end_time": "10:20",
                    }
                ],
            },
            {
                "course_id": "IN4MATX43",
                "primary_code": "20000",
                "sections": [
                    {
                        "course_id": "IN4MATX43",
                        "section_code": "20000",
                        "section_num": "A",
                        "days": "TuTh",
                        "start_time": "11:00",
                        "end_time": "12:20",
                    }
                ],
            },
        ],
        pending_sections=[
            {
                "course_id": "STATS67",
                "section_code": "30000",
                "section_num": "A",
                "days": "F",
                "start_time": "09:00",
                "end_time": "09:50",
            }
        ],
    )

    assert result == {
        "valid": True,
        "warnings": [],
        "conflicts": [],
        "unknowns": [],
    }


def test_validate_schedule_bundle_reports_cancelled_and_full_sections_as_conflicts():
    result = validate_schedule_bundle(
        [
            {
                "course_id": "COMPSCI161",
                "primary_code": "10000",
                "sections": [
                    {
                        "course_id": "COMPSCI161",
                        "section_code": "10000",
                        "section_num": "A",
                        "status": "OPEN",
                        "is_cancelled": True,
                        "days": "MW",
                        "start_time": "09:00",
                        "end_time": "10:20",
                    }
                ],
            },
            {
                "course_id": "IN4MATX43",
                "primary_code": "20000",
                "sections": [
                    {
                        "course_id": "IN4MATX43",
                        "section_code": "20000",
                        "section_num": "A",
                        "status": "FULL",
                        "days": "TuTh",
                        "start_time": "11:00",
                        "end_time": "12:20",
                    }
                ],
            },
        ]
    )

    assert result["valid"] is False
    assert result["warnings"] == []
    assert result["unknowns"] == []
    assert result["conflicts"] == [
        {
            "type": "section_unavailable",
            "scope": "bundle",
            "message": "COMPSCI161 A is cancelled",
            "sections": [
                {
                    "course_id": "COMPSCI161",
                    "section_code": "10000",
                    "section_num": "A",
                    "window": "MW 09:00–10:20",
                }
            ],
        },
        {
            "type": "section_unavailable",
            "scope": "bundle",
            "message": "IN4MATX43 A is FULL",
            "sections": [
                {
                    "course_id": "IN4MATX43",
                    "section_code": "20000",
                    "section_num": "A",
                    "window": "TuTh 11:00–12:20",
                }
            ],
        },
    ]


def test_validate_schedule_bundle_reports_waitlist_and_restrictions_as_warnings():
    result = validate_schedule_bundle(
        [
            {
                "course_id": "COMPSCI161",
                "primary_code": "10000",
                "sections": [
                    {
                        "course_id": "COMPSCI161",
                        "section_code": "10000",
                        "section_num": "A",
                        "status": "Waitl",
                        "restrictions": "L",
                        "days": "MW",
                        "start_time": "09:00",
                        "end_time": "10:20",
                    }
                ],
            }
        ]
    )

    assert result["valid"] is True
    assert result["conflicts"] == []
    assert result["unknowns"] == []
    assert result["warnings"] == [
        {
            "type": "section_waitlist",
            "scope": "bundle",
            "message": "COMPSCI161 A is waitlist-only",
            "sections": [
                {
                    "course_id": "COMPSCI161",
                    "section_code": "10000",
                    "section_num": "A",
                    "window": "MW 09:00–10:20",
                }
            ],
        },
        {
            "type": "section_restriction",
            "scope": "bundle",
            "message": "COMPSCI161 A has enrollment restrictions: L",
            "sections": [
                {
                    "course_id": "COMPSCI161",
                    "section_code": "10000",
                    "section_num": "A",
                    "window": "MW 09:00–10:20",
                }
            ],
        },
    ]


def test_validate_schedule_bundle_reports_recommendation_time_conflicts():
    result = validate_schedule_bundle(
        [
            {
                "course_id": "COMPSCI161",
                "primary_code": "10000",
                "sections": [
                    {
                        "course_id": "COMPSCI161",
                        "section_code": "10000",
                        "section_num": "A",
                        "days": "MW",
                        "start_time": "09:00",
                        "end_time": "10:20",
                    }
                ],
            },
            {
                "course_id": "IN4MATX43",
                "primary_code": "20000",
                "sections": [
                    {
                        "course_id": "IN4MATX43",
                        "section_code": "20000",
                        "section_num": "A",
                        "days": "M",
                        "start_time": "10:00",
                        "end_time": "10:50",
                    }
                ],
            },
        ]
    )

    assert result["valid"] is False
    assert result["unknowns"] == []
    assert result["conflicts"] == [
        {
            "type": "time_conflict",
            "scope": "bundle",
            "message": "COMPSCI161 A conflicts with IN4MATX43 A",
            "sections": [
                {
                    "course_id": "COMPSCI161",
                    "section_code": "10000",
                    "section_num": "A",
                    "window": "MW 09:00–10:20",
                },
                {
                    "course_id": "IN4MATX43",
                    "section_code": "20000",
                    "section_num": "A",
                    "window": "M 10:00–10:50",
                },
            ],
        }
    ]


def test_validate_schedule_bundle_reports_pending_schedule_conflicts():
    result = validate_schedule_bundle(
        [
            {
                "course_id": "COMPSCI161",
                "primary_code": "10000",
                "sections": [
                    {
                        "course_id": "COMPSCI161",
                        "section_code": "10000",
                        "section_num": "A",
                        "days": "MW",
                        "start_time": "09:00",
                        "end_time": "10:20",
                    }
                ],
            }
        ],
        pending_sections=[
            {
                "course_id": "STATS67",
                "section_code": "30000",
                "section_num": "A",
                "days": "M",
                "start_time": "09:30",
                "end_time": "10:50",
            }
        ],
    )

    assert result["valid"] is False
    assert result["unknowns"] == []
    assert result["conflicts"] == [
        {
            "type": "time_conflict",
            "scope": "pending_schedule",
            "message": "COMPSCI161 A conflicts with pending STATS67 A",
            "sections": [
                {
                    "course_id": "COMPSCI161",
                    "section_code": "10000",
                    "section_num": "A",
                    "window": "MW 09:00–10:20",
                },
                {
                    "course_id": "STATS67",
                    "section_code": "30000",
                    "section_num": "A",
                    "window": "M 09:30–10:50",
                },
            ],
        }
    ]


def test_validate_schedule_bundle_reports_missing_required_secondary_pairing():
    result = validate_schedule_bundle(
        [
            {
                "course_id": "COMPSCI161",
                "primary_code": "10000",
                "requires_secondary": True,
                "secondary_type": "Dis",
                "sections": [
                    {
                        "course_id": "COMPSCI161",
                        "section_code": "10000",
                        "section_num": "A",
                        "section_type": "Lec",
                        "days": "MW",
                        "start_time": "09:00",
                        "end_time": "10:20",
                    },
                    {
                        "course_id": "COMPSCI161",
                        "section_code": "10001",
                        "section_num": "A1",
                        "section_type": "Dis",
                        "days": "F",
                        "start_time": "09:00",
                        "end_time": "09:50",
                    },
                ],
            }
        ]
    )

    assert result["valid"] is False
    assert result["unknowns"] == []
    assert result["conflicts"] == [
        {
            "type": "incomplete_pairing",
            "scope": "bundle",
            "message": "COMPSCI161 requires Lec plus Dis, but no Dis section is selected",
            "sections": [
                {
                    "course_id": "COMPSCI161",
                    "section_code": "10000",
                    "section_num": "A",
                    "window": "MW 09:00–10:20",
                }
            ],
        }
    ]


def test_validate_schedule_bundle_accepts_complete_primary_secondary_pairing():
    result = validate_schedule_bundle(
        [
            {
                "course_id": "COMPSCI161",
                "requires_secondary": True,
                "secondary_type": "Dis",
                "selected_sections": [
                    {
                        "course_id": "COMPSCI161",
                        "section_code": "10000",
                        "section_num": "A",
                        "section_type": "Lec",
                        "days": "MW",
                        "start_time": "09:00",
                        "end_time": "10:20",
                    },
                    {
                        "course_id": "COMPSCI161",
                        "section_code": "10001",
                        "section_num": "A1",
                        "section_type": "Dis",
                        "days": "F",
                        "start_time": "09:00",
                        "end_time": "09:50",
                    },
                ],
            }
        ]
    )

    assert result == {
        "valid": True,
        "warnings": [],
        "conflicts": [],
        "unknowns": [],
    }


def test_validate_schedule_bundle_reports_tba_as_unknown_not_clear():
    result = validate_schedule_bundle(
        [
            {
                "course_id": "COMPSCI161",
                "primary_code": "10000",
                "sections": [
                    {
                        "course_id": "COMPSCI161",
                        "section_code": "10000",
                        "section_num": "A",
                        "days": "TBA",
                        "start_time": "",
                        "end_time": "",
                        "time_is_tba": "true",
                    }
                ],
            }
        ],
        pending_sections=[
            {
                "course_id": "STATS67",
                "section_code": "30000",
                "section_num": "A",
                "days": "MW",
                "start_time": "09:00",
                "end_time": "10:20",
            }
        ],
    )

    assert result["valid"] is False
    assert result["conflicts"] == []
    assert result["unknowns"] == [
        {
            "type": "time_unknown",
            "scope": "bundle",
            "message": "COMPSCI161 A has unknown meeting time",
            "sections": [
                {
                    "course_id": "COMPSCI161",
                    "section_code": "10000",
                    "section_num": "A",
                    "window": "TBA ?–?",
                }
            ],
        },
        {
            "type": "time_unknown",
            "scope": "pending_schedule",
            "message": (
                "Cannot determine whether COMPSCI161 A conflicts with "
                "pending STATS67 A"
            ),
            "sections": [
                {
                    "course_id": "COMPSCI161",
                    "section_code": "10000",
                    "section_num": "A",
                    "window": "TBA ?–?",
                },
                {
                    "course_id": "STATS67",
                    "section_code": "30000",
                    "section_num": "A",
                    "window": "MW 09:00–10:20",
                },
            ],
        },
    ]


def test_validate_schedule_bundle_reports_recommendation_final_exam_conflicts():
    result = validate_schedule_bundle(
        [
            {
                "course_id": "COMPSCI161",
                "primary_code": "10000",
                "sections": [
                    {
                        "course_id": "COMPSCI161",
                        "section_code": "10000",
                        "section_num": "A",
                        "days": "MW",
                        "start_time": "09:00",
                        "end_time": "10:20",
                        "final_exam": {
                            "examStatus": "SCHEDULED_FINAL",
                            "dayOfWeek": "Thu",
                            "month": 6,
                            "day": 12,
                            "startTime": {"hour": 8, "minute": 0},
                            "endTime": {"hour": 10, "minute": 0},
                        },
                    }
                ],
            },
            {
                "course_id": "IN4MATX43",
                "primary_code": "20000",
                "sections": [
                    {
                        "course_id": "IN4MATX43",
                        "section_code": "20000",
                        "section_num": "A",
                        "days": "TuTh",
                        "start_time": "11:00",
                        "end_time": "12:20",
                        "final_exam": {
                            "examStatus": "SCHEDULED_FINAL",
                            "dayOfWeek": "Thu",
                            "month": 6,
                            "day": 12,
                            "startTime": {"hour": 9, "minute": 30},
                            "endTime": {"hour": 11, "minute": 30},
                        },
                    }
                ],
            },
        ]
    )

    assert result["valid"] is False
    assert result["unknowns"] == []
    assert result["conflicts"] == [
        {
            "type": "final_exam_conflict",
            "scope": "bundle",
            "message": "COMPSCI161 A final exam conflicts with IN4MATX43 A final exam",
            "sections": [
                {
                    "course_id": "COMPSCI161",
                    "section_code": "10000",
                    "section_num": "A",
                    "window": "MW 09:00–10:20",
                    "final_exam": "Thu 6/12 08:00–10:00",
                },
                {
                    "course_id": "IN4MATX43",
                    "section_code": "20000",
                    "section_num": "A",
                    "window": "TuTh 11:00–12:20",
                    "final_exam": "Thu 6/12 09:30–11:30",
                },
            ],
        }
    ]


def test_validate_schedule_bundle_reports_pending_final_exam_conflicts():
    result = validate_schedule_bundle(
        [
            {
                "course_id": "COMPSCI161",
                "primary_code": "10000",
                "sections": [
                    {
                        "course_id": "COMPSCI161",
                        "section_code": "10000",
                        "section_num": "A",
                        "days": "MW",
                        "start_time": "09:00",
                        "end_time": "10:20",
                        "final_exam": {
                            "examStatus": "SCHEDULED_FINAL",
                            "dayOfWeek": "Thu",
                            "month": 6,
                            "day": 12,
                            "startTime": {"hour": 8, "minute": 0},
                            "endTime": {"hour": 10, "minute": 0},
                        },
                    }
                ],
            }
        ],
        pending_sections=[
            {
                "course_id": "STATS67",
                "section_code": "30000",
                "section_num": "A",
                "days": "TuTh",
                "start_time": "11:00",
                "end_time": "12:20",
                "final_exam": {
                    "examStatus": "SCHEDULED_FINAL",
                    "dayOfWeek": "Thu",
                    "month": 6,
                    "day": 12,
                    "startTime": {"hour": 9, "minute": 30},
                    "endTime": {"hour": 11, "minute": 30},
                },
            }
        ],
    )

    assert result["valid"] is False
    assert result["unknowns"] == []
    assert result["conflicts"] == [
        {
            "type": "final_exam_conflict",
            "scope": "pending_schedule",
            "message": "COMPSCI161 A final exam conflicts with pending STATS67 A final exam",
            "sections": [
                {
                    "course_id": "COMPSCI161",
                    "section_code": "10000",
                    "section_num": "A",
                    "window": "MW 09:00–10:20",
                    "final_exam": "Thu 6/12 08:00–10:00",
                },
                {
                    "course_id": "STATS67",
                    "section_code": "30000",
                    "section_num": "A",
                    "window": "TuTh 11:00–12:20",
                    "final_exam": "Thu 6/12 09:30–11:30",
                },
            ],
        }
    ]


def test_validate_schedule_bundle_reports_tba_final_exam_as_unknown():
    result = validate_schedule_bundle(
        [
            {
                "course_id": "COMPSCI161",
                "primary_code": "10000",
                "sections": [
                    {
                        "course_id": "COMPSCI161",
                        "section_code": "10000",
                        "section_num": "A",
                        "days": "MW",
                        "start_time": "09:00",
                        "end_time": "10:20",
                        "final_exam": {"examStatus": "TBA_FINAL"},
                    }
                ],
            }
        ],
        pending_sections=[
            {
                "course_id": "STATS67",
                "section_code": "30000",
                "section_num": "A",
                "days": "TuTh",
                "start_time": "11:00",
                "end_time": "12:20",
                "final_exam": {
                    "examStatus": "SCHEDULED_FINAL",
                    "dayOfWeek": "Thu",
                    "month": 6,
                    "day": 12,
                    "startTime": {"hour": 9, "minute": 30},
                    "endTime": {"hour": 11, "minute": 30},
                },
            }
        ],
    )

    assert result["valid"] is False
    assert result["conflicts"] == []
    assert result["unknowns"] == [
        {
            "type": "final_exam_unknown",
            "scope": "bundle",
            "message": "COMPSCI161 A final exam time is unknown",
            "sections": [
                {
                    "course_id": "COMPSCI161",
                    "section_code": "10000",
                    "section_num": "A",
                    "window": "MW 09:00–10:20",
                    "final_exam": "Final TBA",
                }
            ],
        },
        {
            "type": "final_exam_unknown",
            "scope": "pending_schedule",
            "message": (
                "Cannot determine whether COMPSCI161 A final exam conflicts "
                "with pending STATS67 A final exam"
            ),
            "sections": [
                {
                    "course_id": "COMPSCI161",
                    "section_code": "10000",
                    "section_num": "A",
                    "window": "MW 09:00–10:20",
                    "final_exam": "Final TBA",
                },
                {
                    "course_id": "STATS67",
                    "section_code": "30000",
                    "section_num": "A",
                    "window": "TuTh 11:00–12:20",
                    "final_exam": "Thu 6/12 09:30–11:30",
                },
            ],
        },
    ]


def test_agent_and_chat_do_not_define_private_schedule_parsers():
    import app.agent.tools as agent_tools
    import app.routers.chat as chat_router

    agent_defs = {
        node.name
        for node in ast.walk(ast.parse(inspect.getsource(agent_tools)))
        if isinstance(node, ast.FunctionDef)
    }
    chat_defs = {
        node.name
        for node in ast.walk(ast.parse(inspect.getsource(chat_router)))
        if isinstance(node, ast.FunctionDef)
    }

    assert not {"_parse_days", "_parse_time", "_section_overlap"} & agent_defs
    assert "_parse_days" not in chat_defs
