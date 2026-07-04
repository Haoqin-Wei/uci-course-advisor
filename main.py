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

from app.routers import chat, system_info, memory, sessions, onboarding, auth, health
from app.memory import get_memory_manager
from app import config, observability
from app.auth import security

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Memory provider self-initializes per session on first request.
    # Warm the Anteater all-courses cache in the background so the
    # first user to hit Step 4 of the wizard doesn't pay a ~40s wait
    # for the ~90-page cursor-paginated fetch.
    import threading
    def _warm():
        try:
            from app.data.uci_general.anteater_programs import list_all_courses
            list_all_courses()
        except Exception as e:
            observability.increment("data.refresh_failures", source="anteater_warmup")
            observability.log_event(
                logger,
                logging.WARNING,
                "data_refresh_failure",
                source="anteater_warmup",
                error=f"{type(e).__name__}: {e}",
            )
    threading.Thread(target=_warm, daemon=True, name="anteater-warmup").start()

    yield
    # On shutdown, flush any in-memory state to disk.
    get_memory_manager().shutdown()


app = FastAPI(
    title="UCI Course Advisor",
    description="Initial demo for course recommendation assistant",
    version="0.1.0",
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
        response.headers["X-Trace-Id"] = trace_id
        status_code = response.status_code
        return response
    finally:
        duration_ms = observability.elapsed_ms(started_at)
        observability.increment(
            "http.requests",
            method=request.method,
            status=status_code,
            route=request.url.path,
        )
        observability.observe_ms(
            "http.request_ms",
            duration_ms,
            method=request.method,
            route=request.url.path,
        )
        observability.log_event(
            logger,
            logging.INFO,
            "http_request",
            method=request.method,
            path=request.url.path,
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

# ── Static files ─────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def serve_frontend():
    return FileResponse("static/index.html")
