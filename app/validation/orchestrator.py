"""
Orchestrator — runs all validators against one context and aggregates
their Issues into a single ValidationReport.

Errors in one validator never crash the others — each is wrapped
in try/except. A crashed validator is logged but doesn't fail the turn.
"""

from __future__ import annotations
import logging

from app.validation.types import ValidationContext, ValidationReport
from app.validation.validators import PHASE_1_VALIDATORS

logger = logging.getLogger(__name__)


def validate(ctx: ValidationContext) -> ValidationReport:
    report = ValidationReport()
    for validator in PHASE_1_VALIDATORS:
        try:
            issues = validator.check(ctx)
            report.issues.extend(issues)
        except Exception as e:
            logger.warning(
                "Validator %r crashed: %s", validator.name, e,
                exc_info=True,
            )
    return report
