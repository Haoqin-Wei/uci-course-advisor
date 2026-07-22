"""Transactional conversation auto/pinned term metadata updates."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app import observability
from app.data import sessions
from app.terms.service import QueryTermResolution


logger = logging.getLogger(__name__)


def commit_conversation_resolution(
    user_id: str,
    session_id: str,
    resolution: QueryTermResolution,
    *,
    answer_succeeded: bool,
    validation_blocked: bool = False,
) -> dict:
    """Commit a single-term change only after a successful, unblocked answer."""
    meta = sessions.get_session_meta(user_id, session_id)
    if (
        not answer_succeeded
        or validation_blocked
        or resolution.error is not None
        or resolution.kind != "single"
        or not resolution.all_available
    ):
        return meta

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    requested = resolution.terms[0]
    if resolution.parsed.reset_to_auto:
        updated = sessions.update_session_meta(
            user_id,
            session_id,
            term_scope=resolution.automatic.canonical_name,
            term_mode="auto",
            term_source=resolution.automatic.source,
            term_updated_at=now,
        )
        observability.log_event(
            logger,
            logging.INFO,
            "conversation_term_reset_auto",
            session_id=session_id,
            term=resolution.automatic.canonical_name,
        )
        return updated

    updated = sessions.update_session_meta(
        user_id,
        session_id,
        term_scope=requested.canonical_name,
        term_mode="pinned",
        term_source=requested.source,
        term_updated_at=now,
    )
    observability.log_event(
        logger,
        logging.INFO,
        "conversation_term_pinned",
        session_id=session_id,
        term=requested.canonical_name,
        source=requested.source,
    )
    return updated

