"""
Read-only metadata endpoints consumed by the frontend on page load.

  GET /api/term-state     Minimal backend-resolved automatic term
  GET /api/system_prompt  Current default LLM system prompt

These exist so the frontend doesn't have to hard-code terms or guess
what prompt the backend is sending. Both are completely optional —
the chat endpoint works fine without them — but they give the UI a
single source of truth.

Wire-up:
    # app/main.py (or wherever you register routers)
    from app.routers import system_info
    app.include_router(system_info.router)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app import config
from app.terms.service import get_term_resolution_service

router = APIRouter()


@router.get("/api/term-state")
def get_term_state() -> dict:
    """Return only the automatic-term fields consumed by the browser."""
    state = get_term_resolution_service().automatic_state()
    return {
        "automatic_term": state["automatic_term"],
        "source": state["source"],
        "status": state["status"],
        "next_cutoff": state["next_cutoff"],
    }


# ── /api/system_prompt ───────────────────────────────────

@router.get("/api/system_prompt")
def get_system_prompt() -> dict:
    """
    Return the default system prompt used for recommendation requests.
    Frontend uses this to seed the Settings modal's textarea so the user
    can edit a copy of the current prompt rather than starting from
    scratch.

    Response shape:
        {"prompt": "You are ZotAdvisor, …"}

    The frontend will fall back gracefully if this returns empty.
    """
    if not config.allow_custom_system_prompt():
        raise HTTPException(status_code=404, detail="System prompt endpoint is disabled")
    prompt = _resolve_default_prompt()
    return {"prompt": prompt or ""}


def _resolve_default_prompt() -> str:
    """
    Locate the default system prompt for the recommendation flow.
    The adapter exposes a stable accessor; this wrapper tolerates a
    missing/renamed adapter without crashing the endpoint.
    """
    try:
        from app.llm.adapter import get_default_answer_prompt
        v = get_default_answer_prompt()
        if isinstance(v, str) and v.strip():
            return v
    except (ImportError, AttributeError):
        pass

    # Fallback: try a few common constant names in adapter / prompts modules
    for module_path in ("app.llm.adapter", "app.llm.prompts"):
        try:
            mod = __import__(module_path, fromlist=["*"])
        except ImportError:
            continue
        for name in ("ANSWER_SYSTEM_PROMPT",
                     "RECOMMENDATION_SYSTEM_PROMPT",
                     "SYSTEM_PROMPT",
                     "DEFAULT_SYSTEM_PROMPT"):
            v = getattr(mod, name, None)
            if isinstance(v, str) and v.strip():
                return v

    return ""
