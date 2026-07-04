"""Validate structured recommendation cards against catalog/tool data."""

from __future__ import annotations

from app.catalog.normalization import parse_course_mention
from app.data import policies
from app.validation.types import (
    Issue, Severity, SuggestedAction, ValidationContext,
)
from app.validation.validators.base import Validator


class CardGroundingValidator(Validator):
    @property
    def name(self) -> str:
        return "card_grounding"

    def check(self, ctx: ValidationContext) -> list[Issue]:
        issues: list[Issue] = []
        for card in ctx.cards or []:
            course_id = card.get("course_id") or ""
            ref = parse_course_mention(course_id)
            if not ref or not ctx.catalog.course_exists(ref):
                issues.append(self._issue(
                    code="CARD_UNKNOWN_COURSE",
                    severity=Severity.ERROR,
                    message=f"Recommendation card references unknown course {course_id!r}.",
                    course_id=course_id,
                    suggested_action=SuggestedAction.REMOVE,
                ))
                continue

            source_sections = ctx.catalog.get_sections(ref)
            by_code = {
                value: section
                for section in source_sections
                for value in (section.section_code, section.section_num)
                if value
            }

            primary_code = card.get("primary_code")
            if primary_code and primary_code not in by_code:
                issues.append(self._issue(
                    code="CARD_INVALID_SECTION_CODE",
                    severity=Severity.ERROR,
                    message=(
                        f"{ref.display()} card uses section {primary_code}, but that "
                        f"section is not in {ctx.catalog.target_term.display()} catalog data."
                    ),
                    course_id=ref.display(),
                    evidence={"section": primary_code},
                    suggested_action=SuggestedAction.REMOVE,
                ))

            for section in card.get("sections") or []:
                self._check_section_payload(
                    issues,
                    ref_display=ref.display(),
                    section=section,
                    by_code=by_code,
                )

            self._check_deadlines(issues, card, ref.display())
            self._check_provenance(issues, card, ref.display())

        return issues

    def _check_section_payload(
        self,
        issues: list[Issue],
        *,
        ref_display: str,
        section: dict,
        by_code: dict,
    ) -> None:
        code = section.get("section_code") or section.get("section_num")
        if not code:
            return
        source = by_code.get(code)
        if not source:
            issues.append(self._issue(
                code="CARD_SECTION_NOT_IN_CATALOG",
                severity=Severity.ERROR,
                message=f"{ref_display} card includes section {code}, not found in catalog data.",
                course_id=ref_display,
                evidence={"section": code},
                suggested_action=SuggestedAction.REMOVE,
            ))
            return

        comparisons = {
            "days": (section.get("days"), source.days),
            "start_time": (section.get("start_time"), source.start_time),
            "end_time": (section.get("end_time"), source.end_time),
            "status": (section.get("status"), source.status),
            "seats_open": (section.get("seats_open"), source.seats_open),
        }
        for field, (actual, expected) in comparisons.items():
            if actual is None or expected is None:
                continue
            if actual != expected:
                issues.append(self._issue(
                    code="CARD_SECTION_FIELD_MISMATCH",
                    severity=Severity.ERROR,
                    message=(
                        f"{ref_display} section {code} card field {field}={actual!r} "
                        f"does not match catalog value {expected!r}."
                    ),
                    course_id=ref_display,
                    evidence={"section": code, "field": field, "actual": actual, "expected": expected},
                    suggested_action=SuggestedAction.REMOVE,
                ))

        card_instructors = set(section.get("instructors") or [])
        if card_instructors and not card_instructors.issubset(set(source.instructors)):
            issues.append(self._issue(
                code="CARD_INSTRUCTOR_MISMATCH",
                severity=Severity.ERROR,
                message=(
                    f"{ref_display} section {code} card instructors are not grounded "
                    f"in catalog data."
                ),
                course_id=ref_display,
                evidence={
                    "section": code,
                    "actual": sorted(card_instructors),
                    "expected": list(source.instructors),
                },
                suggested_action=SuggestedAction.REMOVE,
            ))

    def _check_deadlines(self, issues: list[Issue], card: dict, course_id: str) -> None:
        deadlines = card.get("term_deadlines")
        term = card.get("term") or ctx_term(card)
        if not deadlines or not term:
            return
        calendar = policies.get_term_calendar(term) or {}
        for key in ("free_window_end", "late_add_drop_end", "change_grading_end", "finals_begin"):
            if deadlines.get(key) is None or calendar.get(key) is None:
                continue
            if deadlines.get(key) != calendar.get(key):
                issues.append(self._issue(
                    code="CARD_POLICY_DATE_MISMATCH",
                    severity=Severity.ERROR,
                    message=(
                        f"{course_id} card policy date {key}={deadlines.get(key)!r} "
                        f"does not match policy data {calendar.get(key)!r}."
                    ),
                    course_id=course_id,
                    evidence={"field": key, "actual": deadlines.get(key), "expected": calendar.get(key)},
                    suggested_action=SuggestedAction.REMOVE,
                ))

    def _check_provenance(self, issues: list[Issue], card: dict, course_id: str) -> None:
        if card.get("data_coverage") or card.get("course_provenance"):
            return
        issues.append(self._issue(
            code="CARD_MISSING_PROVENANCE",
            severity=Severity.WARN,
            message=f"{course_id} card has no data provenance attached.",
            course_id=course_id,
            suggested_action=SuggestedAction.ANNOTATE,
        ))

    def _issue(
        self,
        *,
        code: str,
        severity: Severity,
        message: str,
        course_id: str,
        evidence: dict | None = None,
        suggested_action: SuggestedAction,
    ) -> Issue:
        return Issue(
            validator=self.name,
            code=code,
            severity=severity,
            message=message,
            evidence={"course_id": course_id, **(evidence or {})},
            suggested_action=suggested_action,
        )


def ctx_term(card: dict) -> str | None:
    return card.get("term")
