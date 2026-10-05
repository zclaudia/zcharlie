"""FastAPI app factory: middleware, auth, lifespan, route registration."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from investment_agent.api.routes import health
from investment_agent.config import Settings
from investment_agent.logging import configure_logging
from investment_agent.storage.database import init_engine


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)
    init_engine(settings.database_url)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Investment Agent",
        version="1.0.0",
        description=(
            "Multi-strategy stock analysis system. Daily Discovery pipeline +"
            " on-demand Attribution. All endpoints require X-API-Key header"
            " except /healthz. Same-origin web UI lives under /app."
        ),
        lifespan=_lifespan,
    )
    app.include_router(health.router)
    _register_protected_routes(app)
    _register_web(app)
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
