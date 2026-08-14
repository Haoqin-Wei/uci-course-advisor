"""Deterministic conversation default-term mutations.

Only functions in this module may write conversation term metadata. Chat
questions, LLM output, tool calls, and answer success never call these
mutators.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app import observability
from app.data import sessions
from app.terms.models import ResolvedTerm
from app.terms.service import TermResolutionService


logger = logging.getLogger(__name__)


class TermSelectionError(ValueError):
    """A selector request cannot be applied without changing state."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sync_automatic_default(
    user_id: str,
    session_id: str,
    automatic: ResolvedTerm,
) -> dict:
    """Refresh an auto conversation at a request/turn boundary."""
    meta = sessions.get_session_meta(user_id, session_id)
    if meta.get("term_mode") == "manual":
        return meta
    if (
        meta.get("default_term") == automatic.canonical_name
        and meta.get("term_mode") == "auto"
    ):
        return meta
    return sessions.update_session_meta(
        user_id,
        session_id,
        default_term=automatic.canonical_name,
        term_mode="auto",
        term_source=automatic.source,
        term_updated_at=_now_iso(),
        term_updated_by="auto_sync",
        term_schema_version=sessions.TERM_SCHEMA_VERSION,
    )


def set_manual_default(
    user_id: str,
    session_id: str,
    value: str,
    service: TermResolutionService,
) -> dict:
    """Apply a canonical, published selector choice."""
    current = sessions.get_session_meta(user_id, session_id)
    resolution = service.resolve_explicit(value)
    if resolution.error is not None or resolution.kind != "single":
        message = (
            resolution.error.message
            if resolution.error is not None
            else "expected exactly one UCI term"
        )
        raise TermSelectionError("invalid_term", message)
    selected = resolution.terms[0]
    if not service.is_selectable_term(selected.key):
        raise TermSelectionError(
            "term_unavailable",
            f"{selected.canonical_name} is not published or available yet",
        )
    if (
        current.get("term_mode") == "manual"
        and current.get("default_term") == selected.canonical_name
        and current.get("term_updated_by") == "user_ui"
    ):
        return current
    updated = sessions.update_session_meta(
        user_id,
        session_id,
        default_term=selected.canonical_name,
        term_mode="manual",
        term_source="user_ui",
        term_updated_at=_now_iso(),
        term_updated_by="user_ui",
        term_schema_version=sessions.TERM_SCHEMA_VERSION,
    )
    observability.increment("term.selector_change")
    observability.log_event(
        logger,
        logging.INFO,
        "conversation_default_term_changed",
        session_id=session_id,
        default_term=selected.canonical_name,
        actor="user_ui",
        result="success",
    )
    return updated


def restore_automatic_default(
    user_id: str,
    session_id: str,
    service: TermResolutionService,
) -> dict:
    """Return a conversation to auto and immediately use current automatic."""
    sessions.get_session_meta(user_id, session_id)
    automatic = service.automatic_term()
    updated = sessions.update_session_meta(
        user_id,
        session_id,
        default_term=automatic.canonical_name,
        term_mode="auto",
        term_source=automatic.source,
        term_updated_at=_now_iso(),
        term_updated_by="auto_sync",
        term_schema_version=sessions.TERM_SCHEMA_VERSION,
    )
    observability.increment("term.selector_reset")
    observability.log_event(
        logger,
        logging.INFO,
        "conversation_default_term_restored_auto",
        session_id=session_id,
        default_term=automatic.canonical_name,
        actor="user_ui",
        result="success",
    )
    return updated
