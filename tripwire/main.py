"""TripWire application entry point.

Initialises logging, Supabase, Convoy, identity resolver, nonce repository,
and the event processor, then wires the full ingestion → policy → dispatch
pipeline together.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from postgrest.exceptions import APIError as PostgrestAPIError
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from tripwire import __version__
from tripwire.api.middleware import RequestLoggingMiddleware
from tripwire.api.ratelimit import limiter, rate_limit_exceeded_handler
from tripwire.api.routes.auth import router as auth_router
from tripwire.api.routes.deliveries import router as deliveries_router
from tripwire.api.routes.endpoints import router as endpoints_router
from tripwire.api.routes.events import router as events_router
from tripwire.api.routes.facilitator import router as facilitator_router
from tripwire.api.routes.ingest import router as ingest_router
from tripwire.api.routes.stats import router as stats_router
from tripwire.api.routes.subscriptions import router as subscriptions_router
from tripwire.api.routes.well_known import router as well_known_router
from tripwire.config.logging import setup_logging
from tripwire.config.settings import settings
from tripwire.observability.tracing import setup_tracing, shutdown_tracing
from tripwire.db.client import get_supabase_client
from tripwire.observability.audit import AuditLogger
from tripwire.db.repositories.endpoints import EndpointRepository
from tripwire.db.repositories.events import EventRepository
from tripwire.db.repositories.nonces import NonceRepository
from tripwire.db.repositories.webhooks import WebhookDeliveryRepository
from tripwire.identity.resolver import create_resolver
from tripwire.ingestion.finality_poller import FinalityPoller
from tripwire.ingestion.processor import EventProcessor
from tripwire.notify.realtime import RealtimeNotifier
from tripwire.webhook.dlq_handler import DLQHandler
from tripwire.api.redis import get_redis
from tripwire.observability.health import health_registry
from tripwire.observability.metrics import tripwire_build_info
from tripwire.mcp.server import create_mcp_app
from tripwire.webhook.provider import create_webhook_provider

# Configure structlog BEFORE any logger is created
setup_logging(log_level=settings.log_level, app_env=settings.app_env)

logger = structlog.get_logger(__name__)

# Initialize Sentry early to catch startup errors
if settings.sentry_dsn.get_secret_value():
    from tripwire.observability.error_tracking import setup_sentry

    setup_sentry(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.app_env,
        version=__version__,
        traces_sample_rate=settings.sentry_traces_sample_rate,
    )


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

    # Optional OpenTelemetry distributed tracing (must be early so all spans are captured)
    if settings.otel_enabled:
        setup_tracing(
            service_name=settings.otel_service_name,
            version=__version__,
            environment=settings.app_env,
            otlp_endpoint=settings.otel_endpoint,
        )

    # Supabase client
    supabase = get_supabase_client()
    app.state.supabase = supabase
    logger.info("supabase_ready")

    # Audit logger (fire-and-forget writes to audit_log table)
    audit_logger = AuditLogger(supabase)
    app.state.audit_logger = audit_logger
    logger.info("audit_logger_ready")

    # Webhook provider (Convoy in production, LogOnly in dev without key)
    webhook_provider = create_webhook_provider(settings)
    app.state.webhook_provider = webhook_provider
    logger.info("webhook_provider_ready")

    # Warn if Goldsky webhook secret is missing in non-development environments
    if settings.app_env != "development" and not settings.goldsky_webhook_secret.get_secret_value():
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
        supabase_client=supabase,
    )
    app.state.processor = processor
    logger.info("event_processor_ready")

    # Dead Letter Queue handler (background poller for failed Convoy deliveries)
    dlq_handler: DLQHandler | None = None
    if settings.dlq_enabled and settings.convoy_api_key.get_secret_value():
        dlq_handler = DLQHandler(
            endpoint_repo=endpoint_repo,
            delivery_repo=delivery_repo,
            settings=settings,
        )
        await dlq_handler.start()
        app.state.dlq_handler = dlq_handler
        logger.info("dlq_handler_ready")
    else:
        logger.info(
            "dlq_handler_skipped",
            dlq_enabled=settings.dlq_enabled,
            convoy_configured=bool(settings.convoy_api_key.get_secret_value()),
        )

    # Finality poller (background task for confirming pending events & reorg detection)
    finality_poller: FinalityPoller | None = None
    if settings.finality_poller_enabled:
        finality_poller = FinalityPoller(
            event_repo=event_repo,
            endpoint_repo=endpoint_repo,
            delivery_repo=delivery_repo,
            webhook_provider=webhook_provider,
            settings=settings,
        )
        await finality_poller.start()
        app.state.finality_poller = finality_poller
        logger.info("finality_poller_ready")
    else:
        logger.info("finality_poller_skipped", enabled=False)

    # Set Prometheus build info
    tripwire_build_info.info({"version": __version__, "env": settings.app_env})

    # Mark application as ready for traffic
    app.state.ready = True
    app.state.started_at = time.time()
    logger.info("tripwire_ready")

    yield

    # -- Shutdown ------------------------------------------------
    # Stop finality poller if running
    if finality_poller is not None:
        await finality_poller.stop()

    # Stop DLQ handler if running
    if dlq_handler is not None:
        await dlq_handler.stop()

    # Flush and shut down OTel tracing (no-op if never initialised)
    shutdown_tracing()

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

    # Supabase / PostgREST exception handler
    @app.exception_handler(PostgrestAPIError)
    async def supabase_exception_handler(request: Request, exc: PostgrestAPIError):
        code = getattr(exc, "code", None) or ""
        message = getattr(exc, "message", str(exc))

        # Map PostgreSQL error codes to HTTP status codes
        pg_code_map = {
            "23505": (409, "Conflict: unique constraint violation"),
            "23503": (422, "Unprocessable: foreign key violation"),
            "23514": (422, "Unprocessable: check constraint violation"),
            "42501": (403, "Forbidden: insufficient privilege"),
        }

        if code in pg_code_map:
            status_code, detail = pg_code_map[code]
        elif str(code).startswith("PGRST"):
            status_code = 502
            detail = f"PostgREST error: {message}"
        else:
            status_code = 500
            detail = f"Database error: {message}"

        logger.error(
            "supabase_api_error",
            path=request.url.path,
            method=request.method,
            error_code=code,
            detail=detail,
        )
        return JSONResponse(
            status_code=status_code,
            content={"detail": detail, "error_code": code},
        )

    # Network exception handlers (httpx connectivity / timeout)
    @app.exception_handler(httpx.ConnectError)
    async def network_connect_exception_handler(request: Request, exc: httpx.ConnectError):
        logger.error(
            "network_connect_error",
            path=request.url.path,
            method=request.method,
            error=str(exc),
        )
        return JSONResponse(
            status_code=503,
            content={"detail": "Service temporarily unavailable"},
        )

    @app.exception_handler(httpx.TimeoutException)
    async def network_timeout_exception_handler(request: Request, exc: httpx.TimeoutException):
        logger.error(
            "network_timeout_error",
            path=request.url.path,
            method=request.method,
            error=str(exc),
        )
        return JSONResponse(
            status_code=503,
            content={"detail": "Service temporarily unavailable"},
        )

    # Middleware (order matters — outermost first)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # x402 payment gating (only enabled when treasury address is configured)
    if settings.tripwire_treasury_address:
        try:
            from x402.http import HTTPFacilitatorClient, FacilitatorConfig, PaymentOption
            from x402.http.middleware.fastapi import PaymentMiddlewareASGI
            from x402.http.types import RouteConfig
            from x402.mechanisms.evm.exact import ExactEvmServerScheme
            from x402.server import x402ResourceServer

            x402_server = x402ResourceServer(
                HTTPFacilitatorClient(
                    FacilitatorConfig(url=settings.x402_facilitator_url)
                )
            )
            x402_server.register(settings.x402_network, ExactEvmServerScheme())

            x402_routes = {
                "POST /api/v1/endpoints": RouteConfig(
                    accepts=[
                        PaymentOption(
                            scheme="exact",
                            price=settings.x402_registration_price,
                            network=settings.x402_network,
                            pay_to=settings.tripwire_treasury_address,
                        )
                    ]
                ),
            }
            app.add_middleware(PaymentMiddlewareASGI, routes=x402_routes, server=x402_server)
            logger.info(
                "x402_payment_gating_enabled",
                network=settings.x402_network,
                price=settings.x402_registration_price,
                pay_to=settings.tripwire_treasury_address,
            )
        except ImportError:
            logger.warning(
                "x402_payment_gating_unavailable",
                reason="x402 package not installed; run: pip install x402[fastapi,evm]",
            )
    else:
        logger.warning("x402_payment_gating_disabled", reason="tripwire_treasury_address is empty")

    # Global error handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        from tripwire.observability.error_tracking import capture_exception

        capture_exception(exc)
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
    app.include_router(auth_router)
    app.include_router(deliveries_router, prefix="/api/v1")
    app.include_router(endpoints_router, prefix="/api/v1")
    app.include_router(subscriptions_router, prefix="/api/v1")
    app.include_router(events_router, prefix="/api/v1")
    app.include_router(ingest_router, prefix="/api/v1")
    app.include_router(facilitator_router, prefix="/api/v1")
    app.include_router(stats_router, prefix="/api/v1")
    app.include_router(well_known_router)

    # ── MCP server (Model Context Protocol for AI agents) ───────
    mcp_sub_app = create_mcp_app()
    mcp_sub_app.state.parent_app = app
    app.mount("/mcp", mcp_sub_app)

    # ── Prometheus metrics endpoint ──────────────────────────────
    from prometheus_client import make_asgi_app as _make_metrics_app
    from starlette.responses import PlainTextResponse

    metrics_asgi = _make_metrics_app()

    if settings.metrics_bearer_token:
        _expected_auth = f"Bearer {settings.metrics_bearer_token}"

        async def _metrics_with_auth(scope, receive, send):
            if scope["type"] == "http":
                headers = dict(scope.get("headers", []))
                auth = headers.get(b"authorization", b"").decode()
                if auth != _expected_auth:
                    response = PlainTextResponse("Unauthorized", status_code=401)
                    await response(scope, receive, send)
                    return
            await metrics_asgi(scope, receive, send)

        app.mount("/metrics", _metrics_with_auth)
    else:
        app.mount("/metrics", metrics_asgi)

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

        # Check Redis connectivity
        try:
            r = get_redis()
            await r.ping()
            components["redis"] = {"status": "healthy"}
        except Exception as exc:
            components["redis"] = {"status": "unhealthy", "error": str(exc)}

        # Check identity resolver status
        try:
            resolver = request.app.state.identity_resolver
            resolver_type = type(resolver).__name__
            components["identity_resolver"] = {"status": "healthy", "type": resolver_type}
        except Exception as exc:
            components["identity_resolver"] = {"status": "unhealthy", "error": str(exc)}

        # Check background task health
        now = time.time()
        stale_threshold = 300  # 5 minutes
        bg_tasks = health_registry.get_all()
        bg_components: dict[str, dict] = {}
        for task_name, task_health in bg_tasks.items():
            if task_health.last_run_at is None:
                task_status = "unhealthy"
                seconds_since_last_run = None
            else:
                seconds_since_last_run = round(now - task_health.last_run_at, 1)
                task_status = (
                    "unhealthy" if seconds_since_last_run > stale_threshold else "healthy"
                )
            bg_components[task_name] = {
                "status": task_status,
                "running": task_health.running,
                "seconds_since_last_run": seconds_since_last_run,
                "error_count": task_health.error_count,
                "last_error": task_health.last_error,
            }
        components["background_tasks"] = bg_components

        # Determine overall status
        statuses: list[str] = []
        for key, value in components.items():
            if key == "background_tasks":
                for bt in value.values():
                    statuses.append(bt["status"])
            else:
                statuses.append(value["status"])

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
