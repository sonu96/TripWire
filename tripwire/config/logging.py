"""Centralized structlog configuration for TripWire.

Call `setup_logging()` once at application startup (before any logger is used)
to configure structlog with:
  - Timestamped, JSON-formatted output in production
  - Colored, human-readable output in development
  - Log-level filtering from settings
  - Request-scoped context vars (request_id, chain_id, tx_hash)
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

import structlog

# ── Context vars for request-scoped data ──────────────────────
# Bind these in middleware; they auto-propagate through async calls.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
chain_id_var: ContextVar[int | None] = ContextVar("chain_id", default=None)
tx_hash_var: ContextVar[str | None] = ContextVar("tx_hash", default=None)


def _add_context_vars(
    logger: logging.Logger, method_name: str, event_dict: dict
) -> dict:
    """Inject context vars into every log entry when set."""
    rid = request_id_var.get()
    if rid is not None:
        event_dict.setdefault("request_id", rid)

    cid = chain_id_var.get()
    if cid is not None:
        event_dict.setdefault("chain_id", cid)

    txh = tx_hash_var.get()
    if txh is not None:
        event_dict.setdefault("tx_hash", txh)

    return event_dict


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
        _add_context_vars,
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
    for name in ("httpx", "httpcore", "hpack", "supabase", "svix"):
        logging.getLogger(name).setLevel(max(level, logging.WARNING))
