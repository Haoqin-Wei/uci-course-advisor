"""Validation checks used before persisting or displaying grounded output."""

from app.validation.validators.base import Validator
from app.validation.validators.course_exists import CourseExistsValidator
from app.validation.validators.cards import CardGroundingValidator
from app.validation.validators.instructor import InstructorValidator
from app.validation.validators.offered_term import OfferedTermValidator
from app.validation.validators.consistency import ConsistencyValidator
from app.validation.validators.restriction_claims import RestrictionClaimValidator


PHASE_1_VALIDATORS: list[Validator] = [
    CourseExistsValidator(),
    CardGroundingValidator(),
    InstructorValidator(),
    OfferedTermValidator(),
    ConsistencyValidator(),
    RestrictionClaimValidator(),
]


__all__ = [
    "Validator",
    "CourseExistsValidator",
    "CardGroundingValidator",
    "InstructorValidator",
    "OfferedTermValidator",
    "ConsistencyValidator",
    "RestrictionClaimValidator",
    "PHASE_1_VALIDATORS",
]
