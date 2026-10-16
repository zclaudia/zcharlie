"""FastAPI app factory: middleware, auth, lifespan, route registration."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import structlog
from fastapi import FastAPI

from investment_agent.api.routes import health
from investment_agent.api.throttle import AuthFailureThrottle
from investment_agent.config import Settings
from investment_agent.gateway.manager import GatewayManager
from investment_agent.logging import configure_logging
from investment_agent.settings.loader import ensure_imported
from investment_agent.snapshots.directory import initialize_directory
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.repositories.stock_directory import StockDirectoryRepository

log = structlog.get_logger(__name__)

# The startup directory sync runs on a provider thread, and cancelling a task cannot
# interrupt work already handed to that thread. Waiting for it unconditionally let an
# in-flight sync hold up every shutdown: a reproduction that terminated the API while the
# baostock fetch was running took 19s, against 0.26s once the sync had finished, which is
# what made the clean-environment lifecycle smoke test flaky on its 15s budget. Give a
# cooperative task a moment to unwind, then stop waiting.
DIRECTORY_SHUTDOWN_GRACE_SECONDS = 2.0


def _consume_result(task: asyncio.Task[None]) -> None:
    """Retrieve a task's exception so a late failure does not warn as never-retrieved."""

    with suppress(BaseException):
        task.exception()


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)
    async with engine_lifespan(settings.database_url):
        # One-time import of env / app.yaml runtime values (a no-op afterwards;
        # concurrent API / worker / scheduler start-ups import exactly once).
        # A failure (typically a schema not yet migrated) must not take the
        # liveness probe down: it is logged and requests needing settings fail.
        try:
            async with session_scope() as session:
                await ensure_imported(session, settings)
        except Exception:
            log.exception("settings.import_failed", hint="run `ia migrate up`")
        # Mobile gateway link: follows the gateway_* runtime settings, idle when disabled.
        gateway = GatewayManager(_app, base=settings)
        _app.state.gateway = gateway
        await gateway.start()
        # Directory freshness on the shared engine (one sequential read); only
        # an actually empty directory pays for the background full sync, which
        # runs on its own NullPool engine (initialize_directory) so startup
        # still contributes exactly one pooled application connection.
        directory_task: asyncio.Task[None] | None = None
        try:
            async with session_scope() as session:
                _, directory_total = await StockDirectoryRepository(session).freshness()
            if not directory_total:
                directory_task = asyncio.create_task(
                    initialize_directory(database_url=settings.database_url, total=0)
                )
        except Exception:
            log.exception(
                "directory_initialization_failed", hint="retry `ia snapshot sync-universe --now`"
            )
        try:
            yield
        finally:
            await gateway.stop()
            if directory_task is not None:
                directory_task.cancel()
                finished, still_running = await asyncio.wait(
                    {directory_task}, timeout=DIRECTORY_SHUTDOWN_GRACE_SECONDS
                )
                for task in finished:
                    _consume_result(task)
                if still_running:
                    # The sync is inside a provider call that cancellation cannot reach.
                    # The process is going away; leaving it behind beats blocking the exit.
                    log.warning("directory_initialization_abandoned_on_shutdown")
                    for task in still_running:
                        task.add_done_callback(_consume_result)


# CSP for the /app UI: templates and app.js use no inline scripts or styles.
WEB_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)

MAX_REQUEST_BODY_BYTES = 1_000_000
"""Global request-body cap (413 before a handler reads anything).

Every schema field is individually bounded, but many accept free text; behind a
reverse proxy this middleware is the single backstop against oversized bodies.
1 MiB is far above the largest legitimate payload (portfolio import batches and
agent messages stay in the tens of KiB) and answers 413 without buffering.
Chunked requests without Content-Length pass through: real JSON clients always
send the header, and capping a stream mid-read cannot produce a clean 413.
"""

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RequestBodyLimitMiddleware:
    """Reject request bodies declared over :data:`MAX_REQUEST_BODY_BYTES`."""

    def __init__(self, app: Any, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        raw_length = headers.get("content-length")
        if raw_length is not None and raw_length.isdigit() and int(raw_length) > self.max_bytes:
            from fastapi.responses import JSONResponse

            response = JSONResponse({"detail": "request body too large"}, status_code=413)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """Add baseline security headers; CSP only on the browser UI (/app)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        is_web = path.startswith("/app")
        # Static UI files carry no version in their URLs: make browsers
        # revalidate (ETag -> 304) so an upgrade never runs stale JS modules.
        is_static = path.startswith("/app/static/")

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                extra: list[tuple[bytes, bytes]] = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"same-origin"),
                ]
                if is_web:
                    extra.append(
                        (b"content-security-policy", WEB_CONTENT_SECURITY_POLICY.encode())
                    )
                    extra.append((b"x-frame-options", b"DENY"))
                if is_static:
                    extra.append((b"cache-control", b"no-cache"))
                for name, value in extra:
                    if name not in present:
                        headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(*, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()  # type: ignore[call-arg]
    docs_enabled = settings.app_env != "prod"
    app = FastAPI(
        title="Investment Agent",
        version="1.0.0",
        description=(
            "Personal A-share research desk: evidence-first single-stock research,"
            " watchlist refresh and change tracking. All endpoints require"
            " X-API-Key header except /healthz. Same-origin web UI lives under /app."
        ),
        lifespan=_lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.state.auth_throttle = AuthFailureThrottle()
    app.include_router(health.router)
    _register_protected_routes(app)
    _register_web(app)
    # Added before SecurityHeadersMiddleware so the 413 response still gets
    # the security headers (the last-added middleware is the outermost).
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def _register_web(app: FastAPI) -> None:
    """Register the same-origin browser UI (session-cookie auth)."""

    from investment_agent.web.routes import register_web

    register_web(app)


def _register_protected_routes(app: FastAPI) -> None:
    """Register routes that require X-API-Key auth."""
    from investment_agent.api.routes import (
        admin,
        agent,
        calendar,
        candidates,
        dashboard,
        data_health,
        gateway,
        holding_rules,
        industries,
        industry_peers,
        investor,
        market,
        portfolio,
        research,
        research_materials,
        rule_templates,
        stocks,
        verification,
        watchlist,
    )
    from investment_agent.api.routes import (
        settings as settings_routes,
    )

    app.include_router(research.router)
    app.include_router(research_materials.router)
    app.include_router(dashboard.router)
    app.include_router(data_health.router)
    app.include_router(calendar.router)
    app.include_router(gateway.router)
    app.include_router(market.router)
    app.include_router(investor.router)
    from investment_agent.api.routes import methods
    app.include_router(methods.router)
    from investment_agent.api.routes import disclosures
    app.include_router(disclosures.router)
    app.include_router(portfolio.router)
    from investment_agent.api.routes import investment_assessments
    app.include_router(investment_assessments.router)
    app.include_router(holding_rules.router)
    app.include_router(agent.router)
    app.include_router(stocks.router)
    app.include_router(industry_peers.router)
    app.include_router(industries.router)
    app.include_router(watchlist.router)
    app.include_router(admin.router)
    app.include_router(verification.router)
    app.include_router(settings_routes.router)
    app.include_router(rule_templates.router)
    for router in candidates.routers:
        app.include_router(router)
