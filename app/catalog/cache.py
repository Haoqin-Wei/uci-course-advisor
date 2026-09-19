"""
CatalogCache — process-wide, term-keyed CatalogView store.

Lifecycle:
  - On import / startup: register configured loaders
  - On first request for a term: build the CatalogView, cache it
  - Repeat requests for the same term: O(1) lookup

Public API:
    get_catalog(term)  → CatalogView | None
    available_terms()  → list[Term]   (via TermRegistry)
"""

from __future__ import annotations
import logging
from typing import Optional

from app.catalog.types import Provenance
from app.catalog.term import Term, get_term_registry
from app.catalog.view import CatalogView
from app.catalog.loaders.base import CatalogLoader
from app.catalog.loaders.uci_relational import UCIRelationalLoader

logger = logging.getLogger(__name__)


class CatalogCache:
    def __init__(self):
        self._loaders: list[CatalogLoader] = []
        self._views: dict[Term, CatalogView] = {}

    def register_loader(self, loader: CatalogLoader) -> None:
        self._loaders.append(loader)
        reg = get_term_registry()
        for t in loader.available_terms():
            reg.register(t)
        logger.info(
            "Registered loader %r; terms: %s",
            loader.name, [t.term_id for t in loader.available_terms()],
        )

    def get(self, term: Term) -> Optional[CatalogView]:
        if term in self._views:
            return self._views[term]

        all_courses = []
        all_sections = []
        primary_provenance: Optional[Provenance] = None

        for loader in self._loaders:
            if term not in loader.available_terms():
                continue
            try:
                courses, sections = loader.load(term)
            except Exception as e:
                logger.warning(
                    "Loader %r failed for term %s: %s",
                    loader.name, term.term_id, e,
                )
                continue
            all_courses.extend(courses)
            all_sections.extend(sections)
            if primary_provenance is None and sections:
                primary_provenance = sections[0].provenance

        if not all_courses and not all_sections:
            logger.warning("No data for term %s in any loader", term.term_id)
            return None

        if primary_provenance is None:
            primary_provenance = Provenance(
                source_term=term.term_id, target_term=term.term_id,
                loader="composite", source_file=None,
            )

        view = CatalogView(term, all_courses, all_sections, primary_provenance)
        self._views[term] = view
        return view


# ── Module-level singleton ───────────────────────────────

_cache = CatalogCache()
_cache.register_loader(UCIRelationalLoader())


def get_catalog_cache() -> CatalogCache:
    return _cache


def get_catalog(term: Term) -> Optional[CatalogView]:
    return _cache.get(term)
