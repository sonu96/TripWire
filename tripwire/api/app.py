"""TripWire FastAPI application."""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from tripwire.config.settings import settings

from .routes.endpoints import router as endpoints_router
from .routes.events import router as events_router
from .routes.subscriptions import router as subscriptions_router

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and tear down application resources."""
    # Startup — init Supabase client
    from supabase import create_client

    app.state.supabase = create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )
    logger.info("supabase_client_initialized")

    # Startup — init Svix client
    from svix.api import Svix, SvixOptions

    app.state.svix = Svix(
        settings.svix_api_key,
        SvixOptions(),
    )
    logger.info("svix_client_initialized")

    logger.info("tripwire_started", env=settings.app_env, port=settings.app_port)
    yield

    # Shutdown
    logger.info("tripwire_shutting_down")


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


# Health check
@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "tripwire"}


# Mount all routers under /v1 prefix
from fastapi import APIRouter

v1_router = APIRouter(prefix="/v1")
v1_router.include_router(endpoints_router)
v1_router.include_router(subscriptions_router)
v1_router.include_router(events_router)

app.include_router(v1_router)
