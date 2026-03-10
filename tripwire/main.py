"""TripWire application entry point.

Initialises Supabase, Svix, and the FastAPI app, then wires the
ingestion -> policy -> dispatch pipeline together.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
import uvicorn
from fastapi import FastAPI

from tripwire.api.routes.endpoints import router as endpoints_router
from tripwire.api.routes.events import router as events_router
from tripwire.api.routes.subscriptions import router as subscriptions_router
from tripwire.config.settings import settings
from tripwire.db.client import get_supabase_client
from tripwire.webhook.svix_client import init_svix

logger = structlog.get_logger()


# ── Lifespan ──────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle for the FastAPI application."""

    # -- Startup -------------------------------------------------
    logger.info("tripwire_starting", env=settings.app_env, port=settings.app_port)

    # Supabase client (stored on app.state so route handlers can access it)
    app.state.supabase = get_supabase_client()
    logger.info("supabase_ready")

    # Svix client (module-level singleton; routes use it via svix_client helpers)
    init_svix()
    logger.info("svix_ready")

    yield

    # -- Shutdown ------------------------------------------------
    logger.info("tripwire_shutting_down")


# ── App factory ───────────────────────────────────────────────


def create_app() -> FastAPI:
    """Build and return the configured FastAPI application."""
    app = FastAPI(
        title="TripWire",
        description="x402 Execution Middleware -- Stripe Webhooks for x402",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Mount route groups
    app.include_router(endpoints_router, prefix="/api/v1")
    app.include_router(subscriptions_router, prefix="/api/v1")
    app.include_router(events_router, prefix="/api/v1")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()

# ── CLI entry point ───────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "tripwire.main:app",
        host="0.0.0.0",
        port=settings.app_port,
        log_level=settings.log_level,
        reload=settings.app_env == "development",
    )
