"""API rate limiting for TripWire endpoints.

Uses slowapi (Starlette/FastAPI rate limiter) with in-memory storage.
Rate limits are keyed per API key (from Authorization header) with
fallback to client IP for unauthenticated requests.
"""

from __future__ import annotations

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

logger = structlog.get_logger(__name__)

# ── Rate limit constants ─────────────────────────────────────

INGEST_LIMIT = "100/minute"
CRUD_LIMIT = "30/minute"


# ── Key function ─────────────────────────────────────────────


def _get_rate_limit_key(request: Request) -> str:
    """Extract rate-limit key from request.

    Prefers the API key from the Authorization Bearer header for per-key
    limiting.  Falls back to the client IP address for unauthenticated
    requests (e.g. ingest endpoints using Goldsky webhook secret).
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and len(auth) > 7:
        return f"apikey:{auth[7:]}"
    return get_remote_address(request)


# ── Limiter instance ─────────────────────────────────────────

limiter = Limiter(
    key_func=_get_rate_limit_key,
    # headers_enabled is False because our endpoints return Pydantic models,
    # not Response objects.  The Retry-After header is set in the 429 handler.
    headers_enabled=False,
)


# ── Custom 429 handler ───────────────────────────────────────


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Return a JSON 429 response with Retry-After header and log the hit."""
    # Retry-After in seconds -- default to 60s (1 minute window)
    retry_after = getattr(exc, "retry_after", None) or 60

    logger.warning(
        "rate_limit_exceeded",
        path=request.url.path,
        method=request.method,
        key=_get_rate_limit_key(request),
        detail=str(exc.detail),
    )

    response = JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
    )
    response.headers["Retry-After"] = str(int(retry_after))
    return response
