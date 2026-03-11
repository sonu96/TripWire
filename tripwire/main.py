"""TripWire application entry point.

Initialises logging, Supabase, Convoy, identity resolver, nonce repository,
and the event processor, then wires the full ingestion → policy → dispatch
pipeline together.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from tripwire import __version__
from tripwire.api.middleware import RequestLoggingMiddleware
from tripwire.api.ratelimit import limiter, rate_limit_exceeded_handler
from tripwire.api.routes.endpoints import router as endpoints_router
from tripwire.api.routes.events import router as events_router
from tripwire.api.routes.ingest import router as ingest_router
from tripwire.api.routes.stats import router as stats_router
from tripwire.api.routes.subscriptions import router as subscriptions_router
from tripwire.config.logging import setup_logging
from tripwire.config.settings import settings
from tripwire.db.client import get_supabase_client
from tripwire.db.repositories.endpoints import EndpointRepository
from tripwire.db.repositories.events import EventRepository
from tripwire.db.repositories.nonces import NonceRepository
from tripwire.db.repositories.webhooks import WebhookDeliveryRepository
from tripwire.identity.resolver import create_resolver
from tripwire.ingestion.processor import EventProcessor
from tripwire.notify.realtime import RealtimeNotifier
from tripwire.webhook.provider import create_webhook_provider

# Configure structlog BEFORE any logger is created
setup_logging(log_level=settings.log_level, app_env=settings.app_env)

logger = structlog.get_logger(__name__)


# ── Lifespan ──────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle for the FastAPI application."""

    # -- Startup -------------------------------------------------
    logger.info(
        "tripwire_starting",
        version=__version__,
        env=settings.app_env,
        port=settings.app_port,
    )

    # Supabase client
    supabase = get_supabase_client()
    app.state.supabase = supabase
    logger.info("supabase_ready")

    # Webhook provider (Convoy in production, LogOnly in dev without key)
    webhook_provider = create_webhook_provider(settings)
    app.state.webhook_provider = webhook_provider
    logger.info("webhook_provider_ready")

    # Warn if Goldsky webhook secret is missing in non-development environments
    if settings.app_env != "development" and not settings.goldsky_webhook_secret:
        logger.warning(
            "goldsky_webhook_secret_missing",
            env=settings.app_env,
            msg="GOLDSKY_WEBHOOK_SECRET is empty — ingest endpoints will reject requests",
        )

    # Identity resolver (ERC-8004 or mock based on APP_ENV)
    resolver = create_resolver(settings)
    app.state.identity_resolver = resolver
    logger.info("identity_resolver_ready")

    # Nonce deduplication repository
    nonce_repo = NonceRepository(supabase)
    app.state.nonce_repo = nonce_repo
    logger.info("nonce_repo_ready")

    # Realtime notifier (Notify-mode delivery via Supabase Realtime)
    realtime_notifier = RealtimeNotifier(supabase)
    app.state.realtime_notifier = realtime_notifier
    logger.info("realtime_notifier_ready")

    # Repositories
    endpoint_repo = EndpointRepository(supabase)
    event_repo = EventRepository(supabase)
    delivery_repo = WebhookDeliveryRepository(supabase)

    # Event processor — the end-to-end pipeline orchestrator
    processor = EventProcessor(
        endpoint_repo=endpoint_repo,
        event_repo=event_repo,
        nonce_repo=nonce_repo,
        delivery_repo=delivery_repo,
        identity_resolver=resolver,
        realtime_notifier=realtime_notifier,
        webhook_provider=webhook_provider,
    )
    app.state.processor = processor
    logger.info("event_processor_ready")

    # Mark application as ready for traffic
    app.state.ready = True
    app.state.started_at = time.time()
    logger.info("tripwire_ready")

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
        version=__version__,
        lifespan=lifespan,
    )

    # Rate limiter (must be set on app.state before adding middleware)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

    # Middleware (order matters — outermost first)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(SlowAPIMiddleware)
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
        logger.error(
            "unhandled_exception",
            path=request.url.path,
            method=request.method,
            error=str(exc),
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    # Mount route groups
    app.include_router(endpoints_router, prefix="/api/v1")
    app.include_router(subscriptions_router, prefix="/api/v1")
    app.include_router(events_router, prefix="/api/v1")
    app.include_router(ingest_router, prefix="/api/v1")
    app.include_router(stats_router, prefix="/api/v1")

    # ── Operational endpoints (not business logic) ─────────────

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "tripwire", "version": __version__}

    @app.get("/health/detailed")
    async def health_detailed(request: Request):
        """Deep health check — probes Supabase and webhook provider."""
        components: dict[str, dict] = {}

        # Check Supabase connectivity
        try:
            sb = request.app.state.supabase
            sb.table("events").select("id").limit(1).execute()
            components["supabase"] = {"status": "healthy"}
        except Exception as exc:
            components["supabase"] = {"status": "unhealthy", "error": str(exc)}

        # Check webhook provider availability
        try:
            wp = request.app.state.webhook_provider
            # LogOnlyProvider is always healthy; ConvoyProvider has _api_key attr
            if hasattr(wp, "_api_key"):
                components["webhook_provider"] = {"status": "healthy", "type": "convoy"}
            else:
                components["webhook_provider"] = {"status": "healthy", "type": "log_only"}
        except Exception as exc:
            components["webhook_provider"] = {"status": "unhealthy", "error": str(exc)}

        # Check identity resolver status
        try:
            resolver = request.app.state.identity_resolver
            resolver_type = type(resolver).__name__
            components["identity_resolver"] = {"status": "healthy", "type": resolver_type}
        except Exception as exc:
            components["identity_resolver"] = {"status": "unhealthy", "error": str(exc)}

        # Determine overall status
        statuses = [c["status"] for c in components.values()]
        if all(s == "healthy" for s in statuses):
            overall = "healthy"
        elif any(s == "unhealthy" for s in statuses):
            overall = "unhealthy"
        else:
            overall = "degraded"

        status_code = 200 if overall == "healthy" else 503
        uptime = time.time() - getattr(request.app.state, "started_at", time.time())

        return JSONResponse(
            status_code=status_code,
            content={
                "status": overall,
                "version": __version__,
                "uptime_seconds": round(uptime, 1),
                "components": components,
            },
        )

    @app.get("/ready")
    async def readiness(request: Request):
        """Readiness probe — returns 200 only after lifespan startup completes."""
        if getattr(request.app.state, "ready", False):
            return {"ready": True}
        return JSONResponse(status_code=503, content={"ready": False})

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
