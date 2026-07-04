from __future__ import annotations

from app.catalog.types import Provenance
from app.catalog.view import CatalogView
from app.validation.apply import apply_report
from app.validation.orchestrator import validate
from app.validation.policies import decide_action
from app.validation.types import ValidationContext


def _spring_2025_fixture_catalog(minimal_catalog) -> CatalogView:
    return CatalogView(
        target_term=minimal_catalog.term,
        courses=list(minimal_catalog.courses),
        sections=list(minimal_catalog.sections),
        provenance=Provenance(
            source_term=minimal_catalog.term.term_id,
            target_term=minimal_catalog.term.term_id,
            loader="uci_relational_fixture",
            source_file=str(minimal_catalog.data_dir),
        ),
    )


def test_spring_2025_validation_flags_missing_course_and_instructor(
    minimal_catalog,
):
    catalog = _spring_2025_fixture_catalog(minimal_catalog)
    answer = (
        "For Spring 2025, I recommend **COMPSCI 161**. "
        "You could also take **COMPSCI 999** with Professor Nonexistent."
    )
    ctx = ValidationContext(
        llm_answer=answer,
        retrieved={
            "primary": [{"course": {"course_id": "COMPSCI 161"}}],
            "flagged": [],
            "total_found": 1,
        },
        catalog=catalog,
        session_state={"term": "Spring 2025", "major": "Computer Science"},
        user_message="recommend algorithms classes",
    )

    report = validate(ctx)
    issues = {issue.code: issue for issue in report.issues}

    assert report.overall == "fail"
    assert issues["HALLUCINATED_COURSE_ID"].severity.value == "error"
    assert issues["HALLUCINATED_COURSE_ID"].evidence == {
        "ref": "COMPSCI 999",
        "in_catalog": False,
        "in_retrieval": False,
    }
    assert issues["HALLUCINATED_COURSE_ID"].location["snippet"] == "COMPSCI 999"

    assert issues["UNKNOWN_INSTRUCTOR"].severity.value == "warn"
    assert issues["UNKNOWN_INSTRUCTOR"].evidence == {
        "surname": "NONEXISTENT",
        "candidates": [],
    }
    assert issues["UNKNOWN_INSTRUCTOR"].location["snippet"] == (
        "Professor Nonexistent"
    )


def test_spring_2025_validation_footer_surfaces_missing_course_and_instructor(
    minimal_catalog,
):
    catalog = _spring_2025_fixture_catalog(minimal_catalog)
    answer = (
        "Take **COMPSCI 161**. Avoid **COMPSCI 999** unless "
        "Professor Nonexistent confirms it exists."
    )
    ctx = ValidationContext(
        llm_answer=answer,
        retrieved={
            "primary": [{"course": {"course_id": "COMPSCI 161"}}],
            "flagged": [],
            "total_found": 1,
        },
        catalog=catalog,
        session_state={"term": "Spring 2025"},
        user_message="what should I take",
    )

    report = validate(ctx)
    action = decide_action(report)
    final_answer, cards, changed = apply_report(answer, [], report, action)

    assert action.value == "remove"
    assert changed is True
    assert cards == []
    assert "**🔍 Data check:**" in final_answer
    assert "LLM mentioned COMPSCI 999" in final_answer
    assert "LLM referenced 'Professor Nonexistent'" in final_answer
    assert "Avoid **** unless" in final_answer


def test_validation_removes_card_with_invalid_section_code(minimal_catalog):
    catalog = _spring_2025_fixture_catalog(minimal_catalog)
    answer = "Take COMPSCI 161 with section 99999."
    cards = [
        {
            "course_id": "COMPSCI161",
            "primary_code": "99999",
            "sections": [],
            "term": "Spring 2025",
            "data_coverage": {"coverage_status": "complete"},
        }
    ]
    ctx = ValidationContext(
        llm_answer=answer,
        retrieved={
            "primary": [{"course": {"course_id": "COMPSCI 161"}}],
            "flagged": [],
            "total_found": 1,
        },
        catalog=catalog,
        session_state={"term": "Spring 2025"},
        cards=cards,
        user_message="what should I take",
    )

    report = validate(ctx)
    issues = {issue.code: issue for issue in report.issues}
    action = decide_action(report)
    final_answer, final_cards, changed = apply_report(answer, cards, report, action)

    assert issues["CARD_INVALID_SECTION_CODE"].severity.value == "error"
    assert action.value == "remove"
    assert changed is True
    assert final_cards == []
    assert "CARD_INVALID_SECTION_CODE" not in final_answer
