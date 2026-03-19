"""TTL sweeper for pre_confirmed events that never reached onchain confirmation.

Prevents events from being stuck in provisional state indefinitely when the
facilitator reports a payment but the onchain transaction never lands.

Runs as an asyncio background task alongside the finality poller.  Uses a
separate Postgres advisory lock (lock_id=839202) so only one instance sweeps
per cycle.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import structlog

from tripwire.config.settings import settings
from tripwire.observability.health import health_registry

logger = structlog.get_logger(__name__)

# Postgres advisory lock ID (must not collide with finality poller's 839201)
_SWEEPER_LOCK_ID = 839202


class PreConfirmedSweeper:
    """Background task that marks stale pre_confirmed events as payment.failed."""

    def __init__(
        self,
        supabase: Any,
        webhook_dispatcher: Any | None = None,
    ) -> None:
        self._supabase = supabase
        self._dispatcher = webhook_dispatcher
        self._running = False
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the sweeper background loop."""
        self._running = True
        health_registry.register("pre_confirmed_sweeper")
        logger.info(
            "pre_confirmed_sweeper_started",
            ttl_seconds=settings.pre_confirmed_ttl_seconds,
            interval_seconds=settings.pre_confirmed_sweep_interval_seconds,
        )
        self._task = asyncio.create_task(
            self._loop(), name="pre-confirmed-sweeper"
        )

    async def stop(self) -> None:
        """Stop the sweeper gracefully."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("pre_confirmed_sweeper_stopped")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._sweep_once()
                health_registry.record_run("pre_confirmed_sweeper")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pre_confirmed_sweep_error")
                health_registry.record_error(
                    "pre_confirmed_sweeper", "sweep error"
                )

            try:
                await asyncio.sleep(
                    settings.pre_confirmed_sweep_interval_seconds
                )
            except asyncio.CancelledError:
                raise

    # ------------------------------------------------------------------
    # Single sweep cycle
    # ------------------------------------------------------------------

    async def _sweep_once(self) -> None:
        """Find and expire stale pre_confirmed events.

        Acquires a Postgres advisory lock first so only one instance
        performs the sweep when multiple replicas are running.
        """
        # --- Leader election via advisory lock ---
        try:
            lock_result = self._supabase.rpc(
                "try_acquire_leader_lock",
                {"lock_id": _SWEEPER_LOCK_ID},
            ).execute()
            if not lock_result.data:
                logger.debug(
                    "sweeper_lock_skipped",
                    reason="another instance holds the lock",
                )
                return
        except Exception:
            logger.debug("sweeper_lock_unavailable")
            return

        try:
            cutoff_iso = _timestamp_to_iso(
                int(time.time()) - settings.pre_confirmed_ttl_seconds
            )

            # Query events with status 'pre_confirmed' created before cutoff
            result = (
                self._supabase.table("events")
                .select("*")
                .eq("status", "pre_confirmed")
                .lt("created_at", cutoff_iso)
                .limit(100)
                .execute()
            )

            stale_events: list[dict[str, Any]] = result.data or []

            if not stale_events:
                return

            logger.info("pre_confirmed_sweep_found", count=len(stale_events))

            for event in stale_events:
                await self._expire_event(event)
        finally:
            # Always release the lock
            try:
                self._supabase.rpc(
                    "release_leader_lock",
                    {"lock_id": _SWEEPER_LOCK_ID},
                ).execute()
            except Exception:
                logger.debug("sweeper_lock_release_failed")

    # ------------------------------------------------------------------
    # Per-event expiration
    # ------------------------------------------------------------------

    async def _expire_event(self, event: dict[str, Any]) -> None:
        """Mark a single pre_confirmed event as payment.failed."""
        event_id = event.get("id")
        try:
            self._supabase.table("events").update(
                {"status": "payment.failed"}
            ).eq("id", event_id).execute()

            created_at = event.get("created_at", "")
            age = int(time.time()) - _iso_to_timestamp(created_at)
            logger.warning(
                "pre_confirmed_expired",
                event_id=event_id,
                age_seconds=age,
            )

            # Dispatch payment.failed webhook to matched endpoints
            if self._dispatcher:
                endpoint_ids = self._get_endpoint_ids(event_id)
                for ep_id in endpoint_ids:
                    try:
                        await self._dispatcher.dispatch_failure_notification(
                            event, ep_id
                        )
                    except Exception:
                        logger.exception(
                            "sweep_dispatch_failed",
                            event_id=event_id,
                            endpoint_id=ep_id,
                        )

        except Exception:
            logger.exception(
                "pre_confirmed_expire_failed", event_id=event_id
            )

    def _get_endpoint_ids(self, event_id: str | None) -> list[str]:
        """Get endpoint IDs linked to this event via the join table."""
        if not event_id:
            return []
        try:
            result = (
                self._supabase.table("event_endpoints")
                .select("endpoint_id")
                .eq("event_id", event_id)
                .execute()
            )
            return [r["endpoint_id"] for r in (result.data or [])]
        except Exception:
            return []


# ------------------------------------------------------------------
# Timestamp helpers
# ------------------------------------------------------------------


def _timestamp_to_iso(ts: int) -> str:
    """Convert a UNIX timestamp to an ISO-8601 string (UTC)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _iso_to_timestamp(iso: str) -> int:
    """Convert an ISO-8601 string to a UNIX timestamp."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, AttributeError):
        return 0
