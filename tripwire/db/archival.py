"""Nonce archival background task.

Moves old, confirmed nonces to the nonces_archive table to prevent
unbounded growth. Runs daily as an asyncio background task.
"""

from __future__ import annotations

import asyncio

import structlog
from supabase import Client

logger = structlog.get_logger(__name__)

_DEFAULT_AGE_DAYS = 30
_DEFAULT_BATCH_SIZE = 5000
_POLL_INTERVAL = 86400  # 24 hours


class NonceArchiver:
    """Async background task that archives old nonces daily."""

    def __init__(
        self,
        supabase: Client,
        age_days: int = _DEFAULT_AGE_DAYS,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        self._sb = supabase
        self._age_days = age_days
        self._batch_size = batch_size
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None:
            logger.warning("nonce_archiver_already_running")
            return
        self._task = asyncio.create_task(
            self._run_loop(), name="nonce-archiver"
        )
        logger.info("nonce_archiver_started", age_days=self._age_days)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("nonce_archiver_stopped")

    async def _run_loop(self) -> None:
        while True:
            try:
                archived = await self._archive_batch()
                if archived > 0:
                    logger.info("nonces_archived", count=archived)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("nonce_archival_failed")

            try:
                await asyncio.sleep(_POLL_INTERVAL)
            except asyncio.CancelledError:
                raise

    async def _archive_batch(self) -> int:
        """Run the archive_old_nonces DB function."""
        result = await asyncio.to_thread(
            self._sb.rpc,
            "archive_old_nonces",
            {
                "age_threshold": f"{self._age_days} days",
                "batch_size": self._batch_size,
            },
        )
        # rpc returns a PostgrestResponse; the function returns an integer
        response = result.execute()
        if response.data is not None:
            return int(response.data)
        return 0
