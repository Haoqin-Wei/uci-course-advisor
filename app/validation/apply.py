"""
Apply a ValidationReport to the chat response.

Modes:
  - KEEP:     return the answer untouched
  - ANNOTATE: append a footer summarizing issues
  - REMOVE:   strip invalid answer spans and remove invalid cards
  - BLOCK:    discard answer/cards and return a grounded safety fallback
"""

from __future__ import annotations
import logging

from app.validation.types import (
    ValidationReport, SuggestedAction, Severity,
)

logger = logging.getLogger(__name__)


def apply_report(
    answer: str,
    cards: list[dict],
    report: ValidationReport,
    action: SuggestedAction,
) -> tuple[str, list[dict], bool]:
    """Return (final_answer, final_cards, was_modified)."""

    if action == SuggestedAction.KEEP or not report.issues:
        return answer, cards, False

    if action == SuggestedAction.ANNOTATE:
        return answer + _build_footer(report), cards, True

    if action == SuggestedAction.REMOVE:
        filtered_answer = _remove_problem_spans(answer, report)
        filtered_cards = _remove_problem_cards(cards, report)
        return filtered_answer + _build_footer(report), filtered_cards, True

    if action == SuggestedAction.BLOCK:
        logger.warning("BLOCK requested by validation; suppressing answer/cards")
        return _build_blocked_answer(report), [], True

    return answer, cards, False


def _remove_problem_spans(answer: str, report: ValidationReport) -> str:
    spans = []
    for issue in report.issues:
        if issue.suggested_action != SuggestedAction.REMOVE:
            continue
        loc = issue.location or {}
        start = loc.get("start")
        end = loc.get("end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end:
            spans.append((start, end))
    if not spans:
        return answer

    out = answer
    for start, end in sorted(spans, reverse=True):
        out = out[:start] + out[end:]
    return out


def _remove_problem_cards(cards: list[dict], report: ValidationReport) -> list[dict]:
    remove_ids = {
        str(issue.evidence.get("course_id") or issue.evidence.get("ref") or "").upper().replace(" ", "")
        for issue in report.issues
        if issue.suggested_action in {SuggestedAction.REMOVE, SuggestedAction.BLOCK}
    }
    if not remove_ids:
        return cards
    return [
        card for card in cards
        if str(card.get("course_id") or "").upper().replace(" ", "") not in remove_ids
    ]


def _build_blocked_answer(report: ValidationReport) -> str:
    return (
        "I can’t provide that answer reliably because validation found "
        "ungrounded course or schedule details. Please ask again with a "
        "specific term/course, and I’ll re-check against the local catalog."
    )


def _build_footer(report: ValidationReport) -> str:
    """Build a markdown footer summarizing validation findings."""
    if not report.issues:
        return ""

    sev_icons = {
        Severity.ERROR: "❌",
        Severity.WARN: "⚠️",
        Severity.INFO: "ℹ️",
    }

    lines = ["\n\n---\n", "**🔍 Data check:**"]
    by_sev: dict[Severity, list] = {s: [] for s in Severity}
    for issue in report.issues:
        by_sev[issue.severity].append(issue)

    for sev in (Severity.ERROR, Severity.WARN, Severity.INFO):
        for issue in by_sev[sev]:
            lines.append(f"- {sev_icons[sev]} {issue.message}")
    return "\n".join(lines)
