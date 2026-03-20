"""Standalone worker process entry point.

Starts the FastAPI app lifecycle (which initializes all shared deps and
background tasks based on process_role) but does NOT run uvicorn.
Instead, it blocks until a shutdown signal is received.

The process_role setting controls what starts during lifespan:
- "worker": only shared deps + background tasks (no HTTP routes/middleware)
- "all": everything (shared deps + background tasks + HTTP routes)

Usage:
    PROCESS_ROLE=worker python -m tripwire.worker
"""

from __future__ import annotations

import asyncio
import os
import signal

import structlog


logger = structlog.get_logger(__name__)


async def main() -> None:
    """Initialize the app lifespan and block until shutdown signal."""

    # Ensure process_role is set to "worker" if not already specified
    if not os.environ.get("PROCESS_ROLE"):
        os.environ["PROCESS_ROLE"] = "worker"

    from tripwire.main import create_app

    app = create_app()

    # Manually trigger the lifespan context manager — this runs all
    # startup logic (shared deps + worker-only background tasks) and
    # the yield keeps them alive until we exit the context.
    async with app.router.lifespan_context(app) as _:
        logger.info("worker_ready", process_role=os.environ.get("PROCESS_ROLE", "worker"))

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        await stop.wait()
        logger.info("worker_shutting_down")

    logger.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
