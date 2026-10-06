"""FastAPI app factory: middleware, auth, lifespan, route registration."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from investment_agent.api.routes import health
from investment_agent.api.throttle import AuthFailureThrottle
from investment_agent.config import Settings
from investment_agent.logging import configure_logging
from investment_agent.storage.database import init_engine


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)
    init_engine(settings.database_url)
    yield


# CSP for the /app UI: templates and app.js use no inline scripts or styles.
WEB_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


class SecurityHeadersMiddleware:
    """Add baseline security headers; CSP only on the browser UI (/app)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_web = str(scope.get("path", "")).startswith("/app")

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
            "Multi-strategy stock analysis system. Daily Discovery pipeline +"
            " on-demand Attribution. All endpoints require X-API-Key header"
            " except /healthz. Same-origin web UI lives under /app."
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
        attribution,
        dashboard,
        listings,
        pipelines,
        reports,
        research,
        strategies,
        universes,
        watchlist,
    )

    app.include_router(pipelines.router)
    app.include_router(attribution.router)
    app.include_router(reports.router)
    app.include_router(research.router)
    app.include_router(dashboard.router)
    app.include_router(listings.router)
    app.include_router(strategies.router)
    app.include_router(universes.router)
    app.include_router(watchlist.router)
    app.include_router(admin.router)
