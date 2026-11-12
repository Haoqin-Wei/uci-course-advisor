"""
CatalogCache — process-wide, term-keyed CatalogView store.

Lifecycle:
  - On import / startup: register configured loaders
  - On first request for a term: build the CatalogView, cache it
  - Repeat requests for the same term: O(1) lookup
  - A loader may expose ``snapshot_token(term)``. When that marker changes
    (a term was synced, possibly by another process) the view is rebuilt.
    The marker is re-read at most every ``VERSION_CHECK_SECONDS``.

Public API:
    get_catalog(term)  → CatalogView | None
    available_terms()  → list[Term]   (via TermRegistry)
"""

from __future__ import annotations
import logging
import os
import time
from typing import Optional

from app.catalog.types import Provenance
from app.catalog.term import Term, get_term_registry
from app.catalog.view import CatalogView
from app.catalog.loaders.base import CatalogLoader
from app.catalog.loaders.catalog_store import CatalogStoreLoader

logger = logging.getLogger(__name__)

VERSION_CHECK_SECONDS = 30.0


def _version_check_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("CATALOG_VERSION_CHECK_SECONDS", VERSION_CHECK_SECONDS)))
    except ValueError:
        return VERSION_CHECK_SECONDS


class CatalogCache:
    def __init__(self):
        self._loaders: list[CatalogLoader] = []
        self._views: dict[Term, CatalogView] = {}
        self._tokens: dict[Term, tuple] = {}
        self._checked_at: dict[Term, float] = {}

    def register_loader(self, loader: CatalogLoader) -> None:
        self._loaders.append(loader)
        reg = get_term_registry()
        for t in loader.available_terms():
            reg.register(t)
        logger.info(
            "Registered loader %r; terms: %s",
            loader.name, [t.term_id for t in loader.available_terms()],
        )

    def _token(self, term: Term) -> tuple:
        return tuple(
            loader.snapshot_token(term) if hasattr(loader, "snapshot_token") else None
            for loader in self._loaders
        )

    def invalidate(self, term: Optional[Term] = None) -> None:
        """Drop cached views (one term, or all) so the next get() rebuilds."""
        for cache in (self._views, self._tokens, self._checked_at):
            if term is None:
                cache.clear()
            else:
                cache.pop(term, None)

    def get(self, term: Term) -> Optional[CatalogView]:
        now = time.monotonic()
        checked = self._checked_at.get(term)
        if checked is not None and now - checked < _version_check_seconds():
            return self._views.get(term)
        token = self._token(term)
        self._checked_at[term] = now
        if term in self._tokens and self._tokens[term] == token:
            return self._views.get(term)
        self._tokens[term] = token
        self._views.pop(term, None)
        if any(token):
            get_term_registry().register(term)  # Terms synced after startup.

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
            return None  # Negative result is re-checked after the same interval.

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
_cache.register_loader(CatalogStoreLoader())


def get_catalog_cache() -> CatalogCache:
    return _cache


def get_catalog(term: Term) -> Optional[CatalogView]:
    return _cache.get(term)
