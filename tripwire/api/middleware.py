"""Request/response logging middleware with correlation IDs."""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from tripwire.observability.metrics import tripwire_request_duration_seconds

logger = structlog.get_logger(__name__)


def _get_path_template(request: Request) -> str:
    """Return the FastAPI route path template for low-cardinality metric labels.

    Uses the matched route object (e.g. ``/api/v1/endpoints/{endpoint_id}``)
    so that any ID format (UUID, nanoid, etc.) is already parameterised.
    Falls back to the first path segment + ``/{...}`` for unmatched routes (404s).
    """
    route = request.scope.get("route")
    if route is not None and hasattr(route, "path"):
        return route.path
    # No matched route — collapse to first segment to avoid cardinality explosion
    parts = request.url.path.strip("/").split("/", 1)
    return f"/{parts[0]}/{{...}}" if parts and parts[0] else request.url.path


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Assigns a request_id to every request and logs request/response pairs.

    The request_id is:
      - Set in the context var so all downstream logs include it automatically
      - Returned in the X-Request-ID response header for client correlation
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Use client-supplied request ID if present, otherwise generate one
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]

        # Bind to structlog contextvars so all downstream loggers include it
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=rid)

        start = time.perf_counter()
        method = request.method
        path = request.url.path

        logger.info(
            "request_started",
            method=method,
            path=path,
            client=request.client.host if request.client else None,
        )

        try:
            response = await call_next(request)
        except Exception:
            duration_s = time.perf_counter() - start
            duration_ms = round(duration_s * 1000, 1)
            # Record histogram for unhandled exceptions as 500
            path_template = _get_path_template(request)
            tripwire_request_duration_seconds.labels(
                method=method,
                path_template=path_template,
                status_code="500",
            ).observe(duration_s)
            logger.exception(
                "request_failed",
                method=method,
                path=path,
                duration_ms=duration_ms,
            )
            raise

        duration_s = time.perf_counter() - start
        duration_ms = round(duration_s * 1000, 1)
        status = response.status_code

        # Record Prometheus request duration
        path_template = _get_path_template(request)
        tripwire_request_duration_seconds.labels(
            method=method,
            path_template=path_template,
            status_code=str(status),
        ).observe(duration_s)

        log = logger.info if status < 400 else logger.warning
        log(
            "request_completed",
            method=method,
            path=path,
            status=status,
            duration_ms=duration_ms,
        )

        response.headers["X-Request-ID"] = rid
        return response
