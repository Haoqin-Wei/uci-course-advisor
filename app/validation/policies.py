"""
Policy — given a ValidationReport, decide how to apply it.

Policy:
  - Issue.suggested_action drives the final action.
  - BLOCK outranks REMOVE, REMOVE outranks ANNOTATE, ANNOTATE outranks KEEP.
  - Feature flags can still disable REMOVE/BLOCK if needed during rollout.

Thresholds are constants here for easy tuning.
"""

from __future__ import annotations

from app.validation.types import ValidationReport, SuggestedAction


# REMOVE/BLOCK are implemented. Leave the switches here for emergency
# rollback if a validator proves too aggressive in private beta.
ENABLE_REWRITE = True
ENABLE_BLOCK = True

# When BLOCK is enabled, this many errors triggers it.
BLOCK_THRESHOLD_ERRORS = 2


def decide_action(report: ValidationReport) -> SuggestedAction:
    suggested = [issue.suggested_action for issue in report.issues]
    if ENABLE_BLOCK and (
        SuggestedAction.BLOCK in suggested
        or len(report.errors) >= BLOCK_THRESHOLD_ERRORS
    ):
        return SuggestedAction.BLOCK
    if ENABLE_REWRITE and SuggestedAction.REMOVE in suggested:
        return SuggestedAction.REMOVE
    if SuggestedAction.ANNOTATE in suggested:
        return SuggestedAction.ANNOTATE
    if report.issues:
        return SuggestedAction.ANNOTATE
    return SuggestedAction.KEEP
