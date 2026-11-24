"""The campus AP chart says what an exam credit counts for; the transcript does not."""

from __future__ import annotations

import pytest

from app.catalog.course_equivalents import equivalents, expand
from app.data.uci_general.ap_credit import CHART, resolve
from tests.test_academic_transcript_import import _create_and_login, _payload


@pytest.mark.parametrize("subject, score, credited, ge_courses, ge_direct", [
    ("PSYCHOLOGY", 5, ["PSY 9", "PSCI 9", "COGS 7A"], ["PSY 9"], {}),
    ("PSYCHOLOGY", 3, [], [], {}),
    ("ECONOMICS:MI", 4, ["ECON 20A", "MGMT 4A"], ["ECON 20A"], {}),
    ("ECONOMICS:MA", 4, ["ECON 20B", "MGMT 4B"], ["ECON 20B"], {}),
    ("PHYSICS C:ME", 3, [], [], {}),
    ("PHYSICS C:ME", 4, ["PHYSICS 2"], ["PHYSICS 2"], {}),
    ("PHYSICS C:ME", 5, ["PHYSICS 3A"], ["PHYSICS 3A"], {}),
    ("PHYSICS C:E&M", 5, ["PHYSICS 3C"], ["PHYSICS 3C"], {}),
    ("CALCULUS BC", 5, ["MATH 2A", "MATH 2B", "MATH 5A", "MATH 5B"], ["MATH 2A", "MATH 2B"], {}),
    ("CALCULUS AB", 3, ["MATH 2A", "MATH 5A"], ["MATH 2A"], {}),
    ("PRECALCULUS", 5, [], [], {}),
    ("ENGLISH LANG/COMP", 4, ["WRITING 50"], ["WRITING 50"], {"IV": 1}),
    ("FRENCH LANGUAGE", 5, ["FRENCH 2A", "FRENCH 2B", "FRENCH 2C"], ["FRENCH 2A", "FRENCH 2B", "FRENCH 2C"],
     {"VI": 1, "VIII": 1}),
    ("SPANISH LIT", 4, ["SPANISH 3", "SPANISH 3H"], [], {"VI": 1, "VII": 1, "VIII": 1}),
    ("ART HISTORY", 4, ["ART HIS 40A", "ART HIS 40B"], ["ART HIS 40A", "ART HIS 40B"], {"IV": 2, "VIII": 1}),
])
def test_chart_rows(subject, score, credited, ge_courses, ge_direct):
    credit = resolve("AP", subject, score)
    assert credit["matched"] is True
    assert (credit["credited"], credit["ge_courses"], credit["ge_direct"]) == (credited, ge_courses, ge_direct)


def test_unknown_exam_low_score_and_other_exam_types_count_for_nothing():
    assert resolve("AP", "BASKET WEAVING", 5)["matched"] is False
    assert resolve("AP", "PSYCHOLOGY", 2) == {"matched": False, "exam": "Psychology", "credited": [],
                                              "ge_courses": [], "ge_direct": {}, "note": "no credit rule for this score"}
    assert resolve("IB", "PSYCHOLOGY", 7)["matched"] is False


def test_every_chart_rule_has_a_valid_score_band():
    for name, _, rules in CHART:
        assert rules, name
        for rule in rules:
            low, high = rule["scores"]
            assert 3 <= low <= high <= 5, name


def test_renamed_course_answers_to_both_ids():
    assert equivalents("SWE 43") == ("SWE 43", "IN4MATX 43")
    assert equivalents("in4matx  43") == ("IN4MATX 43", "SWE 43")
    assert equivalents("MATH 2A") == ("MATH 2A",)
    assert expand(["PSY 9", "MATH 2A", "PSCI 9"]) == ["PSY 9", "PSCI 9", "MATH 2A"]


def test_import_records_what_each_exam_counts_for(app_client):
    from app.academic.store import get_exam_credits

    user = _create_and_login(app_client)
    payload = _payload(printed_at="2026-09-18T15:23:00Z")
    payload["exam_credits"] = [
        {"exam_type": "AP", "subject": "PSYCHOLOGY", "score": 5, "units": 4, "exam_date": "05/24"},
        {"exam_type": "AP", "subject": "PHYSICS C:ME", "score": 3, "units": 4, "exam_date": "05/23"},
        {"exam_type": "AP", "subject": "UNLISTED EXAM", "score": 5, "units": 4, "exam_date": "05/23"},
    ]
    assert app_client.post("/api/academic/transcript/import", json=payload).status_code == 200
    credits = {credit["subject"]: credit for credit in get_exam_credits(user["id"])}
    assert credits["PSYCHOLOGY"]["matched"] is True
    assert credits["PSYCHOLOGY"]["credited_courses"] == ["PSY 9", "PSCI 9", "COGS 7A"]
    assert credits["PSYCHOLOGY"]["ge_courses"] == ["PSY 9"]
    assert credits["PHYSICS C:ME"]["matched"] is True and credits["PHYSICS C:ME"]["credited_courses"] == []
    assert credits["PHYSICS C:ME"]["note"] == "elective units only"
    assert credits["UNLISTED EXAM"]["matched"] is False
