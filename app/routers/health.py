"""Health and readiness endpoints for private-beta operations."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import observability
from app.catalog.coverage import get_coverage_manifest
from app.memory import get_memory_manager
from app.terms.service import get_term_resolution_service

router = APIRouter()


@router.get("/health/live")
def live() -> dict:
    return {
        "status": "live",
        "trace_id": observability.get_trace_id(),
    }


@router.get("/health/ready")
def ready():
    checks: dict[str, dict] = {}

    try:
        manifest = get_coverage_manifest()
        terms = manifest.get("terms") or []
        checks["catalog"] = {
            "ok": bool(terms),
            "terms": len(terms),
            "updated_at": manifest.get("updated_at"),
            "schema_version": manifest.get("schema_version"),
        }
    except Exception as exc:
        checks["catalog"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        manager = get_memory_manager()
        checks["memory"] = {"ok": manager is not None}
    except Exception as exc:
        checks["memory"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        term_state = get_term_resolution_service().automatic_state()
        checks["term_state"] = {
            "ok": bool(term_state.get("automatic_term")),
            **term_state,
        }
    except Exception as exc:
        checks["term_state"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    ok = all(item.get("ok") for item in checks.values())
    payload = {
        "status": "ready" if ok else "not_ready",
        "trace_id": observability.get_trace_id(),
        "checks": checks,
    }
    return JSONResponse(status_code=200 if ok else 503, content=payload)


@router.get("/health/metrics")
def metrics() -> dict:
    return {
        "trace_id": observability.get_trace_id(),
        "metrics": observability.snapshot_metrics(),
        "term_state": get_term_resolution_service().automatic_state(),
    }
