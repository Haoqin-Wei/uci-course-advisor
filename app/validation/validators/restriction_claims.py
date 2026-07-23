"""Validate restriction-policy claims against fetched structured evidence."""

from __future__ import annotations

from datetime import datetime
import logging
import re
from urllib.parse import urlsplit

from app import observability
from app.validation.types import (
    Issue,
    Severity,
    SuggestedAction,
    ValidationContext,
)
from app.validation.validators.base import Validator

logger = logging.getLogger(__name__)

_DATE_TIME = re.compile(
    r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})"
    r"(?:[ T]+(\d{1,2}):(\d{2}))?"
)
_URL = re.compile(r"https?://[^\s)\]>]+")
_TYPE_PATTERNS = {
    "school_major": re.compile(
        r"school\s*/?\s*major|major restriction|专业限制|院系限制|学校限制",
        re.I,
    ),
    "new_only": re.compile(r"new only|\bNORS?\b|新生(?:预留|限制)", re.I),
    "class_level": re.compile(r"class[- ]level|standing|年级限制", re.I),
    "repeat": re.compile(r"repeat restriction|重修限制", re.I),
    "course_specific": re.compile(r"course restriction|课程专属限制", re.I),
}
_ELIGIBLE_CLAIM = re.compile(
    r"\b(?:you are|you're|is|are)\s+(?:currently\s+)?eligible\b"
    r"|\b(?:you can|can enroll|may enroll)\b"
    r"|你(?:现在)?(?:可以|能)(?:选|注册|加课)|符合资格",
    re.I,
)
_EARLY_ELIGIBILITY_CLAIM = re.compile(
    r"无需等(?:到)?|不用等(?:到)?|不必等(?:到)?"
    r"|原本就不适用|一开始就不适用"
    r"|学期初.{0,16}(?:即可|可以|能)(?:选|注册|加课)"
    r"|before.{0,30}(?:can|may|eligible|enroll)"
    r"|already eligible",
    re.I,
)


class RestrictionClaimValidator(Validator):
    @property
    def name(self) -> str:
        return "restriction_claims"

    def check(self, ctx: ValidationContext) -> list[Issue]:
        bundle = ctx.restriction_evidence
        if not bundle:
            return []
        if bundle.get("evidence_status") in {"unavailable", "partial"}:
            return []

        issues: list[Issue] = []
        events = bundle.get("events") or []
        primary = next(
            (
                event
                for event in events
                if event.get("event_id") == bundle.get("primary_event_id")
            ),
            None,
        )
        known_by_type = _known_datetimes_by_type(events)
        known_all = {
            value
            for values in known_by_type.values()
            for value in values
        }

        if primary and primary.get("effective_at"):
            primary_value = _minute_key(primary["effective_at"])
            answer_values = {
                _match_minute_key(match)
                for match in _DATE_TIME.finditer(ctx.llm_answer)
            }
            if not any(
                _claim_matches_known(value, {primary_value})
                for value in answer_values
            ):
                issues.append(
                    Issue(
                        validator=self.name,
                        code="RESTRICTION_PRIMARY_FACT_MISSING",
                        severity=Severity.ERROR,
                        message=(
                            "The final answer omitted the verified primary "
                            "restriction date."
                        ),
                        evidence={
                            "restriction_type": primary.get("restriction_type"),
                            "effective_at": primary.get("effective_at"),
                        },
                        suggested_action=SuggestedAction.BLOCK,
                    )
                )

        for match in _DATE_TIME.finditer(ctx.llm_answer):
            value = _match_minute_key(match)
            window = _claim_context(ctx.llm_answer, match.start(), match.end())
            claimed_types = {
                restriction_type
                for restriction_type, pattern in _TYPE_PATTERNS.items()
                if pattern.search(window)
            }
            if not _claim_matches_known(value, known_all) and claimed_types:
                issues.append(
                    _date_issue(
                        "RESTRICTION_UNSUPPORTED_DATE",
                        "The answer states a restriction date that is absent "
                        "from fetched evidence.",
                        value,
                        claimed_types,
                        known_by_type,
                    )
                )
                continue
            for restriction_type in claimed_types:
                allowed = known_by_type.get(restriction_type) or set()
                if allowed and not _claim_matches_known(value, allowed):
                    issues.append(
                        _date_issue(
                            "RESTRICTION_TYPE_DATE_MISMATCH",
                            "The answer assigns a fetched date to the wrong "
                            "restriction type.",
                            value,
                            {restriction_type},
                            known_by_type,
                        )
                    )

        if primary:
            for exception in primary.get("exceptions") or []:
                course_ids = [
                    exception.get("course_id"),
                    *(exception.get("course_ids") or []),
                ]
                for course_id in filter(None, course_ids):
                    if _course_key(course_id) not in _course_key(ctx.llm_answer):
                        issues.append(
                            Issue(
                                validator=self.name,
                                code="RESTRICTION_EXCEPTION_MISSING",
                                severity=Severity.ERROR,
                                message=(
                                    f"The final answer omitted verified exception "
                                    f"{course_id}."
                                ),
                                evidence={"course_id": course_id},
                                suggested_action=SuggestedAction.BLOCK,
                            )
                        )

        eligibility = bundle.get("eligibility") or {}
        if (
            eligibility.get("eligible") is not True
            and _ELIGIBLE_CLAIM.search(ctx.llm_answer)
        ):
            issues.append(
                Issue(
                    validator=self.name,
                    code="RESTRICTION_ELIGIBILITY_UNGROUNDED",
                    severity=Severity.ERROR,
                    message=(
                        "The answer asserts enrollment eligibility without "
                        "explicit supporting evidence."
                    ),
                    evidence={"eligibility": eligibility},
                    suggested_action=SuggestedAction.BLOCK,
                )
            )
        eligibility_event = next(
            (
                event
                for event in events
                if event.get("event_id") == eligibility.get("evidence_event_id")
            ),
            None,
        )
        if (
            eligibility_event
            and eligibility_event.get("effective_at")
            and _EARLY_ELIGIBILITY_CLAIM.search(ctx.llm_answer)
        ):
            issues.append(
                Issue(
                    validator=self.name,
                    code="RESTRICTION_ELIGIBILITY_TIME_UNGROUNDED",
                    severity=Severity.ERROR,
                    message=(
                        "The answer claims enrollment access before the "
                        "verified eligibility event takes effect."
                    ),
                    evidence={
                        "effective_at": eligibility_event.get("effective_at"),
                        "event_id": eligibility_event.get("event_id"),
                    },
                    suggested_action=SuggestedAction.BLOCK,
                )
            )

        source_urls = {
            source.get("url")
            for source in bundle.get("sources") or []
            if source.get("url")
        }
        source_keys = {_url_key(url) for url in source_urls}
        cited_urls = {match.group(0).rstrip(".,;") for match in _URL.finditer(ctx.llm_answer)}
        for cited_url in cited_urls:
            if _url_key(cited_url) not in source_keys:
                issues.append(
                    Issue(
                        validator=self.name,
                        code="RESTRICTION_UNFETCHED_SOURCE",
                        severity=Severity.ERROR,
                        message=(
                            "The answer cites a URL that was not fetched by "
                            "the restriction workflow."
                        ),
                        evidence={"url": cited_url},
                        suggested_action=SuggestedAction.BLOCK,
                    )
                )

        observability.log_event(
            logger,
            logging.INFO if not issues else logging.WARNING,
            "restriction_claim_validated",
            evidence_status=bundle.get("evidence_status"),
            event_count=len(events),
            issue_count=len(issues),
            issue_codes=[issue.code for issue in issues],
        )
        return _deduplicate_issues(issues)


def _known_datetimes_by_type(
    events: list[dict],
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for event in events:
        restriction_type = event.get("restriction_type")
        effective_at = event.get("effective_at")
        if not restriction_type or not effective_at:
            continue
        result.setdefault(restriction_type, set()).add(_minute_key(effective_at))
    return result


def _minute_key(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        match = _DATE_TIME.search(str(value))
        return _match_minute_key(match) if match else str(value)
    return parsed.strftime("%Y-%m-%d %H:%M")


def _match_minute_key(match: re.Match[str]) -> str:
    year, month, day, hour, minute = match.groups()
    date_key = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    if hour is None:
        return date_key
    return (
        f"{date_key} "
        f"{int(hour or 0):02d}:{int(minute or 0):02d}"
    )


def _claim_matches_known(value: str, known_values: set[str]) -> bool:
    if value in known_values:
        return True
    if len(value) == 10:
        return any(known.startswith(f"{value} ") for known in known_values)
    return False


def _date_issue(
    code: str,
    message: str,
    value: str,
    claimed_types: set[str],
    known_by_type: dict[str, set[str]],
) -> Issue:
    return Issue(
        validator="restriction_claims",
        code=code,
        severity=Severity.ERROR,
        message=message,
        evidence={
            "claimed_datetime": value,
            "claimed_types": sorted(claimed_types),
            "known_by_type": {
                key: sorted(values) for key, values in known_by_type.items()
            },
        },
        suggested_action=SuggestedAction.BLOCK,
    )


def _claim_context(answer: str, start: int, end: int) -> str:
    line_start = answer.rfind("\n", 0, start) + 1
    line_end = answer.find("\n", end)
    if line_end < 0:
        line_end = len(answer)
    return answer[line_start:line_end]


def _course_key(value: str) -> str:
    upper = (value or "").upper()
    upper = re.sub(r"I\s*&\s*C\s*SCI", "ICS", upper)
    return re.sub(r"[^A-Z0-9]", "", upper)


def _url_key(value: str) -> tuple[str, str]:
    parsed = urlsplit(value)
    path = parsed.path.rstrip("/") or "/"
    return (parsed.netloc.lower(), path)


def _deduplicate_issues(issues: list[Issue]) -> list[Issue]:
    result = []
    seen = set()
    for issue in issues:
        key = (issue.code, str(issue.evidence))
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result
