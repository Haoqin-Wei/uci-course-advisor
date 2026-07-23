from __future__ import annotations

from app.routers import chat
from app.validation.types import ValidationContext, ValidationReport
from app.validation.validators.restriction_claims import RestrictionClaimValidator


def _bundle(*, eligible=True) -> dict:
    return {
        "evidence_status": "verified",
        "primary_event_id": "major",
        "events": [
            {
                "event_id": "major",
                "restriction_type": "school_major",
                "effective_at": "2026-09-18T12:00:00-07:00",
                "exceptions": [
                    {
                        "course_id": "I&C SCI 139W",
                        "text": "I&C SCI 139W remains restricted",
                    }
                ],
            },
            {
                "event_id": "nor",
                "restriction_type": "new_only",
                "effective_at": "2026-09-01T12:00:00-07:00",
                "exceptions": [],
            },
        ],
        "eligibility": {
            "eligible": eligible,
            "student_major": "CSE",
            "reason": "CSE is explicitly listed." if eligible else "Unknown.",
            "evidence_event_id": "major",
        },
        "sources": [
            {"url": "https://www.reg.uci.edu/perl/WebSoc"},
            {
                "url": (
                    "https://ics.uci.edu/"
                    "course-enrollment-restrictions/"
                )
            },
        ],
    }


def _context(answer: str, *, bundle: dict | None = None) -> ValidationContext:
    return ValidationContext(
        llm_answer=answer,
        retrieved={},
        catalog=None,  # RestrictionClaimValidator does not consult the catalog.
        session_state={},
        restriction_evidence=bundle or _bundle(),
    )


def test_restriction_validator_accepts_grounded_fact_block() -> None:
    answer = """
**直接答案：**I&C SCI School/Major 专业限制解除时间为 2026-09-18 12:00。

**相关但不同的限制：**New Only（NOR）限制解除时间为 2026-09-01 12:00。

**例外：**I&C SCI 139W remains restricted.

**来源：**[WebSoc](https://www.reg.uci.edu/perl/WebSoc)、
[ICS](https://ics.uci.edu/course-enrollment-restrictions/)。
"""

    issues = RestrictionClaimValidator().check(_context(answer))

    assert issues == []


def test_restriction_validator_accepts_repeated_markdown_date_without_time() -> None:
    answer = """
**直接答案：**I&C SCI School/Major 专业限制解除时间为 2026-09-18 12:00。

补充解释：专业限制会在 **2026-09-18** 解除。
New Only（NOR）则在 **2026-09-01** 解除。

**例外：**I&C SCI 139W remains restricted.
"""

    issues = RestrictionClaimValidator().check(_context(answer))

    assert issues == []


def test_restriction_validator_blocks_nor_date_used_as_major_date() -> None:
    answer = """
I&C SCI 专业限制解除时间为 2026-09-01 12:00。
I&C SCI 139W 是例外。
[WebSoc](https://www.reg.uci.edu/perl/WebSoc)
[ICS](https://ics.uci.edu/course-enrollment-restrictions/)
"""

    issues = RestrictionClaimValidator().check(_context(answer))
    codes = {issue.code for issue in issues}

    assert "RESTRICTION_PRIMARY_FACT_MISSING" in codes
    assert "RESTRICTION_TYPE_DATE_MISMATCH" in codes
    assert all(issue.suggested_action.value == "block" for issue in issues)


def test_restriction_validator_blocks_missing_exception_and_unfetched_url() -> None:
    answer = """
专业限制解除时间为 2026-09-18 12:00。
[WebSoc](https://www.reg.uci.edu/perl/WebSoc)
[Other](https://ics.uci.edu/not-fetched/)
"""

    issues = RestrictionClaimValidator().check(_context(answer))
    codes = {issue.code for issue in issues}

    assert "RESTRICTION_EXCEPTION_MISSING" in codes
    assert "RESTRICTION_UNFETCHED_SOURCE" in codes


def test_restriction_validator_blocks_unsupported_eligibility_claim() -> None:
    answer = """
专业限制解除时间为 2026-09-18 12:00。
I&C SCI 139W 是例外。You can enroll now.
[WebSoc](https://www.reg.uci.edu/perl/WebSoc)
[ICS](https://ics.uci.edu/course-enrollment-restrictions/)
"""

    issues = RestrictionClaimValidator().check(
        _context(answer, bundle=_bundle(eligible=None))
    )

    assert "RESTRICTION_ELIGIBILITY_UNGROUNDED" in {
        issue.code for issue in issues
    }


def test_restriction_validator_blocks_access_before_verified_event() -> None:
    issues = RestrictionClaimValidator().check(
        _context(
            """
专业限制解除时间为 2026-09-18 12:00。
你在学期初即可选课，无需等到 9/18。
I&C SCI 139W 是例外。
"""
        )
    )

    assert "RESTRICTION_ELIGIBILITY_TIME_UNGROUNDED" in {
        issue.code for issue in issues
    }


def test_restriction_validator_skips_unavailable_bundle() -> None:
    bundle = _bundle()
    bundle["evidence_status"] = "unavailable"

    issues = RestrictionClaimValidator().check(
        _context("No verified date is available.", bundle=bundle)
    )

    assert issues == []


def test_chat_validation_rewrites_conflicting_llm_text_to_verified_facts(
    monkeypatch,
) -> None:
    evidence = _bundle()
    verified_facts = {
        "summary_markdown": (
            "专业限制解除时间为 2026-09-18 12:00；"
            "I&C SCI 139W 是例外。"
        )
    }

    monkeypatch.setattr(chat, "get_catalog", lambda _term: object())
    monkeypatch.setattr(
        chat,
        "validate",
        lambda ctx: ValidationReport(
            RestrictionClaimValidator().check(ctx)
        ),
    )
    monkeypatch.setattr(chat, "write_log", lambda *_args, **_kwargs: None)

    answer, cards, report = chat._validate_response(
        answer=(
            "专业限制解除时间为 2026-09-01 12:00。"
            "I&C SCI 139W 是例外。"
        ),
        cards=[],
        retrieved={},
        state={"term": "2026 Fall"},
        user_message="ICS 专业限制什么时候解除？",
        term_str="2026 Fall",
        session_id="test",
        restriction_evidence=evidence,
        restriction_verified_facts=verified_facts,
    )

    assert answer == verified_facts["summary_markdown"]
    assert cards == []
    assert report is not None
    assert report["applied_action"] == "block"
