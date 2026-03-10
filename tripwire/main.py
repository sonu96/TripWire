"""TripWire application entry point.

Initialises Supabase, Svix, identity resolver, nonce repository,
and the event processor, then wires the full ingestion → policy → dispatch
pipeline together.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from tripwire.api.routes.endpoints import router as endpoints_router
from tripwire.api.routes.events import router as events_router
from tripwire.api.routes.ingest import router as ingest_router
from tripwire.api.routes.subscriptions import router as subscriptions_router
from tripwire.config.settings import settings
from tripwire.db.client import get_supabase_client
from tripwire.db.repositories.nonces import NonceRepository
from tripwire.identity.resolver import create_resolver
from tripwire.ingestion.processor import EventProcessor
from tripwire.webhook.svix_client import init_svix

logger = structlog.get_logger()


# ── Lifespan ──────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle for the FastAPI application."""

    # -- Startup -------------------------------------------------
    logger.info("tripwire_starting", env=settings.app_env, port=settings.app_port)

    # Supabase client
    supabase = get_supabase_client()
    app.state.supabase = supabase
    logger.info("supabase_ready")

    # Svix client (module-level singleton)
    init_svix()
    logger.info("svix_ready")

    # Identity resolver (ERC-8004 or mock based on APP_ENV)
    resolver = create_resolver(settings)
    app.state.identity_resolver = resolver
    logger.info("identity_resolver_ready")

    # Nonce deduplication repository
    nonce_repo = NonceRepository(supabase)
    app.state.nonce_repo = nonce_repo
    logger.info("nonce_repo_ready")

    # Event processor — the end-to-end pipeline orchestrator
    processor = EventProcessor(
        supabase=supabase,
        identity_resolver=resolver,
        nonce_repo=nonce_repo,
    )
    app.state.processor = processor
    logger.info("event_processor_ready")

    yield

    # -- Shutdown ------------------------------------------------
    # Close identity resolver HTTP client if it has one
    if hasattr(resolver, "close"):
        await resolver.close()

    logger.info("tripwire_shutting_down")


# ── App factory ───────────────────────────────────────────────


def create_app() -> FastAPI:
    """Build and return the configured FastAPI application."""
    app = FastAPI(
        title="TripWire",
        description="x402 Execution Middleware — Stripe Webhooks for x402",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Global error handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error("unhandled_exception", path=request.url.path, error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    # Mount route groups
    app.include_router(endpoints_router, prefix="/api/v1")
    app.include_router(subscriptions_router, prefix="/api/v1")
    app.include_router(events_router, prefix="/api/v1")
    app.include_router(ingest_router, prefix="/api/v1")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "tripwire"}

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
