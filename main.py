"""
UCI Course Recommendation Assistant — FastAPI Entry Point
"""

from contextlib import asynccontextmanager
import logging
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.routers import academic, auth, chat, health, memory, onboarding, sessions, system_info
from app.memory import get_memory_manager
from app import config, observability
from app.auth import security

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Memory provider self-initializes per session on first request.
    from app.data.sessions import migrate_term_metadata
    migrate_term_metadata()

    import threading
    def _sync_terms():
        try:
            from app.terms.sync import get_term_synchronizer
            get_term_synchronizer().sync_if_due()
        except Exception as e:
            observability.increment("term.sync", result="startup_error")
            observability.log_event(
                logger,
                logging.WARNING,
                "term_sync_failed",
                phase="startup",
                error=f"{type(e).__name__}: {e}",
            )
    stop_term_sync = threading.Event()

    def _refresh_terms_periodically():
        while not stop_term_sync.is_set():
            _sync_terms()
            # sync_if_due owns the cache interval. Long-running deployments
            # must refresh calendars without requiring an application restart.
            if stop_term_sync.wait(3600):
                break

    threading.Thread(target=_refresh_terms_periodically, daemon=True, name="term-state-sync").start()

    try:
        yield
    finally:
        stop_term_sync.set()
        get_memory_manager().shutdown()


app = FastAPI(
    title="UCI Course Advisor",
    description="Private-beta course planning assistant for UCI students",
    version="0.8.0",
    lifespan=lifespan,
)


def _origin_allowed(origin: str | None, host: str | None) -> bool:
    if not origin:
        return False
    parsed = urlsplit(origin)
    origin_host = parsed.netloc
    if host and origin_host == host:
        return True
    allowed = config.allowed_origins()
    normalized = origin.rstrip("/") if origin else ""
    return normalized in allowed


def _set_security_headers(response) -> None:
    """Apply browser protections to API, HTML, and static responses."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "img-src 'self' data:; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "connect-src 'self'; "
        "worker-src 'self' blob:"
    )
    if config.is_production():
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"


def _request_route_label(request: Request) -> str:
    """Return a low-cardinality route label without user-controlled IDs."""
    route_template = getattr(request.scope.get("route"), "path", None)
    if route_template:
        return str(route_template)
    path = request.url.path
    if path.startswith("/api/"):
        group = path.split("/", 3)[2]
        return f"/api/{group}"
    return path


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """Production CSRF/origin guard and isolated guest-cookie bootstrap."""
    trace_id = request.headers.get("x-trace-id") or observability.new_trace_id()
    trace_token = observability.set_trace_id(trace_id)
    started_at = observability.now()
    status_code = 500
    try:
        if (
            config.csrf_protection_enabled()
            and request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
        ):
            origin = request.headers.get("origin") or request.headers.get("referer")
            if not _origin_allowed(origin, request.headers.get("host")):
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "Cross-origin request blocked"},
                )
                status_code = response.status_code
                _set_security_headers(response)
                response.headers["X-Trace-Id"] = trace_id
                return response

        response = await call_next(request)

        has_session = bool(request.cookies.get(security.SESSION_COOKIE_NAME))
        guest_cookie = request.cookies.get(security.GUEST_COOKIE_NAME)
        has_valid_guest = bool(guest_cookie and security.verify_guest(guest_cookie))
        if (
            config.allow_guest_users()
            and not config.allow_shared_demo()
            and not has_session
            and not has_valid_guest
        ):
            guest_id = security.new_guest_id()
            response.set_cookie(
                key=security.GUEST_COOKIE_NAME,
                value=security.sign_guest(guest_id),
                max_age=security.GUEST_MAX_AGE_S,
                httponly=True,
                samesite="lax",
                secure=config.cookie_secure(),
                path="/",
            )
        # Static assets use stable filenames, so force browsers to revalidate
        # them on every page load.  This prevents a new index.html from being
        # paired with an older JavaScript bundle after a deployment.
        if request.url.path == "/":
            response.headers["Cache-Control"] = "no-store, max-age=0"
        elif request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        _set_security_headers(response)
        response.headers["X-Trace-Id"] = trace_id
        status_code = response.status_code
        return response
    finally:
        duration_ms = observability.elapsed_ms(started_at)
        route_label = _request_route_label(request)
        observability.increment(
            "http.requests",
            method=request.method,
            status=status_code,
            route=route_label,
        )
        observability.observe_ms(
            "http.request_ms",
            duration_ms,
            method=request.method,
            route=route_label,
        )
        observability.log_event(
            logger,
            logging.INFO,
            "http_request",
            method=request.method,
            path=route_label,
            status=status_code,
            duration_ms=duration_ms,
        )
        observability.reset_trace_id(trace_token)

# ── Routers ──────────────────────────────────────────────
app.include_router(chat.router, prefix="/api")
app.include_router(system_info.router)
app.include_router(memory.router)
app.include_router(sessions.router)
app.include_router(onboarding.router)
app.include_router(auth.router)
app.include_router(health.router)
app.include_router(academic.router)

# ── Static files ─────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def serve_frontend():
    return FileResponse(
        "static/index.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/privacy", include_in_schema=False)
async def serve_privacy_notice():
    return FileResponse("static/privacy.html", headers={"Cache-Control": "no-cache"})


@app.get("/terms", include_in_schema=False)
async def serve_terms():
    return FileResponse("static/terms.html", headers={"Cache-Control": "no-cache"})
