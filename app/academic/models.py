"""Allow-listed request models for locally parsed transcript data.

The browser is responsible for reading the PDF. These models intentionally
have no fields for file names, raw text, names, student IDs, or source URLs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class AcademicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TranscriptCourseInput(AcademicModel):
    course_id: str = Field(min_length=2, max_length=32)
    department: str = Field(min_length=2, max_length=24)
    course_number: str = Field(min_length=1, max_length=12)
    title: str = Field(default="", max_length=160)
    units: Optional[float] = Field(default=None, ge=0, le=30)
    grade: str = Field(min_length=1, max_length=8)
    grade_points: Optional[float] = Field(default=None, ge=0, le=200)
    credit_code: Optional[str] = Field(default=None, max_length=8)
    effective_term: Optional[str] = Field(default=None, max_length=48)
    confidence: float = Field(default=1.0, ge=0, le=1)


class ExamCreditInput(AcademicModel):
    exam_type: Literal["AP", "IB", "A_LEVEL"]
    subject: str = Field(min_length=1, max_length=120)
    score: Optional[float] = Field(default=None, ge=0, le=100)
    units: Optional[float] = Field(default=None, ge=0, le=60)
    exam_date: Optional[str] = Field(default=None, max_length=16)
    uci_equivalent_course: Optional[str] = Field(default=None, max_length=32)
    confidence: float = Field(default=1.0, ge=0, le=1)


class TransferCreditInput(AcademicModel):
    institution_name: Optional[str] = Field(default=None, max_length=160)
    units: float = Field(ge=0, le=500)
    terms_through: Optional[str] = Field(default=None, max_length=24)
    uci_equivalent_course: Optional[str] = Field(default=None, max_length=32)
    confidence: float = Field(default=1.0, ge=0, le=1)


class UniversityRequirementInput(AcademicModel):
    requirement_code: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=80)
    status_date: Optional[str] = Field(default=None, max_length=16)


class TranscriptSummaryInput(AcademicModel):
    official_uc_gpa: Optional[float] = Field(default=None, ge=0, le=4.3)
    grade_units_attempted: Optional[float] = Field(default=None, ge=0, le=1000)
    total_units_passed: Optional[float] = Field(default=None, ge=0, le=1000)
    units_completed: Optional[float] = Field(default=None, ge=0, le=1000)


class TranscriptImportRequest(AcademicModel):
    parser_version: str = Field(min_length=1, max_length=32)
    printed_at: Optional[datetime] = None
    courses: list[TranscriptCourseInput] = Field(default_factory=list, max_length=400)
    exam_credits: list[ExamCreditInput] = Field(default_factory=list, max_length=50)
    transfer_credits: list[TransferCreditInput] = Field(default_factory=list, max_length=50)
    university_requirements: list[UniversityRequirementInput] = Field(
        default_factory=list,
        max_length=50,
    )
    summary: TranscriptSummaryInput = Field(default_factory=TranscriptSummaryInput)
    skipped_count: int = Field(default=0, ge=0, le=1000)
    client_request_id: Optional[str] = Field(default=None, min_length=8, max_length=64)
