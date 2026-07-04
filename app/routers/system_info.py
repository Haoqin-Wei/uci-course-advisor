"""
Read-only metadata endpoints consumed by the frontend on page load.

  GET /api/terms          List available terms + default
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

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from app import config, observability

router = APIRouter()
logger = logging.getLogger(__name__)


# ── /api/terms ───────────────────────────────────────────

@router.get("/api/terms")
def list_terms() -> dict:
    """
    Return the terms the backend actually has section data for, plus
    coverage status and which one is the default. Frontend uses this to
    populate the term dropdown without hard-coded term names.

    Response shape:
        {
          "terms": [
            {"name": "Spring 2026", "coverage_status": "complete", ...},
            {"name": "Fall 2026", "coverage_status": "partial", ...}
          ],
          "term_names": ["Spring 2026", "Fall 2026"],
          "default": "Spring 2026"
        }
    """
    try:
        from app.catalog.coverage import default_term_name, get_coverage_manifest

        manifest = get_coverage_manifest()
        terms = manifest["terms"]
        for item in terms:
            status = item.get("coverage_status")
            if status in {"partial", "stale", "unavailable"}:
                observability.increment("catalog.coverage_status", status=status)
                observability.log_event(
                    logger,
                    logging.WARNING,
                    "catalog_coverage",
                    term=item.get("name"),
                    status=status,
                    section_count=item.get("section_count"),
                    updated_at=item.get("updated_at"),
                )
        term_names = [item["name"] for item in terms]
        return {
            "terms": terms,
            "term_names": term_names,
            "default": default_term_name(manifest),
            "manifest": {
                "schema_version": manifest["schema_version"],
                "generated_at": manifest["generated_at"],
                "updated_at": manifest["updated_at"],
                "source": manifest["source"],
            },
        }
    except Exception as e:
        # Fallback: scan sections.csv directly if the catalog isn't
        # importable for whatever reason (e.g. validation module not wired).
        observability.increment("catalog.terms_fallback")
        observability.log_event(
            logger,
            logging.WARNING,
            "catalog_terms_fallback",
            error=f"{type(e).__name__}: {e}",
        )
        return _terms_from_csv_fallback(error=str(e))


def _terms_from_csv_fallback(error: Optional[str] = None) -> dict:
    """Read terms straight from data/uci/sections.csv as a last resort."""
    import csv
    from collections import Counter
    from pathlib import Path

    path = Path("data/uci/sections.csv")
    if not path.exists():
        return {"terms": [], "default": None, "error": error or "no sections.csv"}

    counter: Counter = Counter()
    with path.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            y, q = r.get("year"), r.get("quarter")
            if y and q:
                counter[f"{q} {y}"] += 1

    # Sort by (year DESC, quarter order). Most recent first.
    QUARTER_ORDER = {"Fall": 4, "Spring": 3, "Winter": 2,
                     "Summer2": 1.3, "Summer10wk": 1.2, "Summer1": 1.1}
    def sort_key(label: str):
        q, y = label.rsplit(" ", 1)
        return (-int(y), -QUARTER_ORDER.get(q, 0))
    terms = sorted(counter.keys(), key=sort_key)

    term_objects = [
        {
            "name": term,
            "coverage_status": "partial" if counter[term] < 1_000 else "complete",
            "section_count": counter[term],
        }
        for term in terms
    ]

    return {
        "terms": term_objects,
        "term_names": terms,
        "default": terms[0] if terms else None,
        **({"error": error} if error else {}),
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
