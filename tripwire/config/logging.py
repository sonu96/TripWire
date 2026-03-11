"""Centralized structlog configuration for TripWire.

Call `setup_logging()` once at application startup (before any logger is used)
to configure structlog with:
  - Timestamped, JSON-formatted output in production
  - Colored, human-readable output in development
  - Log-level filtering from settings
  - Request-scoped context via structlog.contextvars (request_id, chain_id, tx_hash)
"""

from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(log_level: str = "info", app_env: str = "development") -> None:
    """Configure structlog + stdlib logging for the entire application.

    Args:
        log_level: Minimum log level (debug, info, warning, error, critical).
        app_env: "development" for colored console output, anything else for JSON.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Shared processors used by both structlog and stdlib
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if app_env == "development":
        # Pretty colored output for local dev
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(
            colors=sys.stderr.isatty()
        )
    else:
        # Structured JSON for production (parseable by log aggregators)
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure stdlib logging (uvicorn, httpx, supabase, etc.)
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet noisy third-party loggers
    for name in ("httpx", "httpcore", "hpack", "supabase"):
        logging.getLogger(name).setLevel(max(level, logging.WARNING))
