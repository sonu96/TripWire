"""Request/response logging middleware with correlation IDs."""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from tripwire.config.logging import request_id_var

logger = structlog.get_logger(__name__)


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
        request_id_var.set(rid)

        # Bind to structlog context so child loggers inherit it
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
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            logger.exception(
                "request_failed",
                method=method,
                path=path,
                duration_ms=duration_ms,
            )
            raise

        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        status = response.status_code

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
