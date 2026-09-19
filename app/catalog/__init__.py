"""Catalog package — term-scoped, normalized course/section data."""

from app.catalog.types import (
    CourseRef, CourseRecord, SectionRecord, Provenance,
)
from app.catalog.term import Term, get_term_registry
from app.catalog.view import CatalogView
from app.catalog.cache import get_catalog, get_catalog_cache
from app.catalog.coverage import get_coverage_manifest, get_term_coverage
from app.catalog.normalization import (
    parse_course_mention, iter_course_mentions,
    normalize_instructor, instructor_last_name, iter_instructor_mentions,
)

__all__ = [
    "CourseRef", "CourseRecord", "SectionRecord", "Provenance",
    "Term", "get_term_registry",
    "CatalogView", "get_coverage_manifest", "get_term_coverage",
    "get_catalog", "get_catalog_cache",
    "parse_course_mention", "iter_course_mentions",
    "normalize_instructor", "instructor_last_name", "iter_instructor_mentions",
]
