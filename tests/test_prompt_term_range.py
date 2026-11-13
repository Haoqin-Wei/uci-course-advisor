from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.terms.intent import apply_semantic_plan
from app.terms.query_scope import resolve_query_scope


@pytest.mark.parametrize("message", ["ICS33在2023至2024年是不是只在Winter开", "Was ICS33 only offered in Winter from 2023 through 2024?",
                                      "ICS33在2023至2024年开课记录", "Show ICS33 offerings from 2023 through 2024"])
def test_explicit_offering_pattern_year_range_overrides_recent_history_and_old_focus(message):
    scope = resolve_query_scope(message, "2027 Winter", "2027 Winter",
                                {"terms": ["2025 Winter"], "course_ids": ["ICS33"]},
                                datetime(2026, 11, 20, 12, tzinfo=ZoneInfo("America/Los_Angeles")),
                                current_term="2026 Fall")
    assert scope.canonical_terms == ("2023 Winter", "2023 Spring", "2023 Fall", "2024 Winter", "2024 Spring", "2024 Fall")
    assert scope.intent == "offering_pattern"
    assert scope.explicit is True
    assert apply_semantic_plan(scope, {"intent": "offering_pattern"}, default_term="2027 Winter", current_term="2026 Fall", focus=None) == scope


def test_oversized_explicit_history_range_requests_clarification_instead_of_silent_default():
    scope = resolve_query_scope("ICS33在2019到2025年是不是只在Winter开", "2027 Winter", "2027 Winter", None,
                                datetime(2026, 11, 20, 12, tzinfo=ZoneInfo("America/Los_Angeles")), current_term="2026 Fall")
    assert scope.error is not None
    assert not scope.terms


def test_related_followup_keeps_previously_explicit_history_range_in_semantic_step():
    names = [f"{year} {quarter}" for year in (2023, 2024) for quarter in ("Winter", "Spring", "Fall")]
    focus = {"terms": names, "intent": "offering_pattern", "course_ids": ["ICS33"]}
    scope = resolve_query_scope("那ICS32呢", "2027 Winter", "2027 Winter", focus,
                                datetime(2026, 11, 20, 12, tzinfo=ZoneInfo("America/Los_Angeles")), current_term="2026 Fall")
    assert scope.canonical_terms == tuple(names)
    refined = apply_semantic_plan(scope, {"intent": "offering_pattern"}, default_term="2027 Winter", current_term="2026 Fall", focus=focus)
    assert refined.canonical_terms == tuple(names)
