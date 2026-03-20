"""Finality poller — background task that promotes pending events to confirmed.

Runs as an asyncio background task started during the FastAPI lifespan.  Each
supported chain is polled on its own cadence (fast for L2s, slower for L1)
because finality semantics differ:

  - Arbitrum: ~250ms block time, 1 confirmation needed → poll every 5s
  - Base:     2s block time, 3 confirmations → poll every 10s
  - Ethereum: 12s block time, 12 confirmations → poll every 30s

For every pending event the poller:
  1. Fetches the current block number via JSON-RPC.
  2. Computes confirmations = current_block - event.block_number.
  3. Once confirmations >= required depth, transitions the event to
     ``confirmed`` and fires a ``payment.confirmed`` webhook.

Note: Manual reorg detection (block hash comparison) has been removed.
Goldsky Edge provides cross-node consensus, making manual reorg checks
redundant for our use case.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

from tripwire.config.settings import Settings
from tripwire.db.repositories.endpoints import EndpointRepository
from tripwire.db.repositories.events import EventRepository
from tripwire.db.repositories.nonces import NonceRepository
from tripwire.db.repositories.webhooks import WebhookDeliveryRepository
from tripwire.ingestion.finality import get_block_number, get_block_hash
from tripwire.types.models import (
    ERC3009Transfer,
    FINALITY_DEPTHS,
    ChainId,
    FinalityStatus,
    WebhookEventType,
)
from tripwire.observability.health import health_registry
from tripwire.webhook.dispatcher import dispatch_event
from tripwire.webhook.provider import WebhookProvider

logger = structlog.get_logger(__name__)

# Postgres advisory lock IDs for leader election.
# Each chain gets its own lock so Base, Ethereum, and Arbitrum can poll
# concurrently across replicas without blocking each other.
# Scheme: 840000 + chain_id  (e.g. 840000 + 8453 = 848453 for Base).
# Must not collide with ttl_sweeper._SWEEPER_LOCK_ID = 839202.
_FINALITY_LOCK_BASE_OFFSET = 840000


def _lock_id_for_chain(chain_id: int) -> int:
    """Generate a unique advisory lock ID per chain for finality polling."""
    return _FINALITY_LOCK_BASE_OFFSET + chain_id

# Maps ChainId → settings attribute name for poll interval
_CHAIN_INTERVAL_ATTR: dict[ChainId, str] = {
    ChainId.ARBITRUM: "finality_poll_interval_arbitrum",
    ChainId.BASE: "finality_poll_interval_base",
    ChainId.ETHEREUM: "finality_poll_interval_ethereum",
}


class FinalityPoller:
    """Background poller that confirms pending events once they reach
    the required finality depth.

    Spawns one asyncio task per chain so each chain runs on its own poll
    cadence.  Uses ``get_block_number()`` from ``finality.py`` for the
    current block height via JSON-RPC.
    """

    def __init__(
        self,
        event_repo: EventRepository,
        endpoint_repo: EndpointRepository,
        delivery_repo: WebhookDeliveryRepository,
        webhook_provider: WebhookProvider,
        settings: Settings,
        nonce_repo: NonceRepository | None = None,
    ) -> None:
        self._event_repo = event_repo
        self._endpoint_repo = endpoint_repo
        self._delivery_repo = delivery_repo
        self._webhook_provider = webhook_provider
        self._settings = settings
        self._nonce_repo = nonce_repo
        self._tasks: list[asyncio.Task[None]] = []
        # Set during poll cycle when asyncpg coordination pool is available
        self._pg_conn: Any | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn one polling task per supported chain."""
        if self._tasks:
            logger.warning("finality_poller_already_running")
            return

        health_registry.register("finality_poller")

        for chain_id, attr_name in _CHAIN_INTERVAL_ATTR.items():
            interval = getattr(self._settings, attr_name)
            task = asyncio.create_task(
                self._chain_poll_loop(chain_id, interval),
                name=f"finality-poller-{chain_id.name.lower()}",
            )
            self._tasks.append(task)
            logger.info(
                "finality_poller_chain_started",
                chain=chain_id.name,
                interval_seconds=interval,
            )

        logger.info("finality_poller_started", chains=len(self._tasks))

    async def stop(self) -> None:
        """Cancel all chain polling tasks gracefully."""
        for task in self._tasks:
            task.cancel()

        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._tasks.clear()
        logger.info("finality_poller_stopped")

    # ------------------------------------------------------------------
    # Per-chain poll loop
    # ------------------------------------------------------------------

    async def _chain_poll_loop(self, chain_id: ChainId, interval: int) -> None:
        """Infinite loop: poll pending events for *chain_id* every *interval* seconds."""
        while True:
            try:
                await self._poll_chain(chain_id)
                health_registry.record_run("finality_poller")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("finality_poll_error", chain=chain_id.name)
                health_registry.record_error("finality_poller", f"poll error on {chain_id.name}")

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    # ------------------------------------------------------------------
    # Single poll iteration
    # ------------------------------------------------------------------

    async def _poll_chain(self, chain_id: ChainId) -> None:
        """Fetch pending events for *chain_id* and process each one.

        Acquires a Postgres advisory lock via asyncpg so only one instance
        polls when multiple TripWire replicas are running.  The lock is held
        on a dedicated connection for the entire poll cycle (not via
        PostgREST which uses connection pooling and would release it
        immediately).

        When DATABASE_URL is not configured (dev environments), the lock
        is skipped and the poll runs unconditionally.
        """
        # --- Leader election via per-chain advisory lock ---
        lock_id = _lock_id_for_chain(chain_id.value)
        try:
            lock_result = await asyncio.to_thread(
                lambda: self._event_repo._sb.rpc(
                    "try_acquire_leader_lock",
                    {"lock_id": lock_id},
                ).execute()
            )
            if not lock_result.data:
                logger.debug(
                    "finality_poll_skipped",
                    reason="another instance holds lock",
                    chain=chain_id.name,
                    lock_id=lock_id,
                )
                return
        except Exception:
            logger.debug("finality_lock_unavailable", chain=chain_id.name, lock_id=lock_id)
            return

        try:
            await self._poll_chain_inner(chain_id)
        finally:
            # Always release the advisory lock
            try:
                await asyncio.to_thread(
                    lambda: self._event_repo._sb.rpc(
                        "release_leader_lock",
                        {"lock_id": lock_id},
                    ).execute()
                )
            except Exception:
                logger.debug("finality_lock_release_failed", chain=chain_id.name, lock_id=lock_id)

    async def _poll_chain_inner(
        self, chain_id: ChainId, *, conn: Any | None = None
    ) -> None:
        """Core poll logic — called only after advisory lock is acquired.

        When *conn* is provided (asyncpg connection holding the advisory
        lock), pending events are fetched and updated via that connection.
        When *conn* is None (dev fallback), Supabase is used instead.
        """
        from tripwire.db.postgres import (
            fetch_pending_events,
            update_event_finality,
            update_event_status,
            fetch_event_endpoint_ids,
        )

        # Store conn for downstream methods to use during this poll cycle
        self._pg_conn = conn

        try:
            await self._poll_chain_inner_body(chain_id, conn)
        finally:
            self._pg_conn = None

    async def _poll_chain_inner_body(
        self, chain_id: ChainId, conn: Any | None
    ) -> None:
        """Actual poll logic body, separated for clean _pg_conn lifecycle."""
        from tripwire.db.postgres import (
            fetch_pending_events,
            update_event_finality,
            update_event_status,
            fetch_event_endpoint_ids,
        )

        # Query events table for status="pending" on this chain
        if conn is not None:
            pending_events = await fetch_pending_events(conn, chain_id.value)
        else:
            pending_events = await asyncio.to_thread(
                self._fetch_pending_events, chain_id
            )

        if not pending_events:
            return

        logger.debug(
            "finality_poll_pending",
            chain=chain_id.name,
            count=len(pending_events),
        )

        # Fetch current block once per poll cycle (avoid redundant RPC calls)
        try:
            current_block = await get_block_number(chain_id)
        except Exception:
            logger.exception(
                "finality_poll_block_fetch_failed", chain=chain_id.name
            )
            return

        required = FINALITY_DEPTHS[chain_id]

        # Batch fetch canonical block hashes for reorg detection.
        # Group events by block_number to avoid redundant RPC calls.
        unique_blocks = {e["block_number"] for e in pending_events}
        canonical_hashes: dict[int, str] = {}
        for block_num in unique_blocks:
            try:
                h = await get_block_hash(chain_id, block_num)
                if h:
                    canonical_hashes[block_num] = h.lower()
            except Exception:
                logger.warning(
                    "finality_poll_hash_fetch_failed",
                    chain=chain_id.name,
                    block_number=block_num,
                )

        for event in pending_events:
            try:
                # Reorg detection: compare stored block_hash vs canonical
                stored_hash = (event.get("block_hash") or "").lower()
                block_num = event["block_number"]
                canonical = canonical_hashes.get(block_num)

                if canonical and stored_hash and canonical != stored_hash:
                    await self._handle_reorg(event)
                    continue

                await self._process_event(event, chain_id, current_block, required)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "finality_poll_event_error",
                    event_id=event.get("id"),
                    chain=chain_id.name,
                )

    # ------------------------------------------------------------------
    # Per-event processing
    # ------------------------------------------------------------------

    async def _update_finality(self, event_id: str, depth: int) -> None:
        """Update finality depth via asyncpg conn if available, else Supabase."""
        if self._pg_conn is not None:
            from tripwire.db.postgres import update_event_finality
            await update_event_finality(self._pg_conn, event_id, depth)
        else:
            await asyncio.to_thread(
                self._event_repo.update_finality, event_id, depth
            )

    async def _update_status(
        self, event_id: str, new_status: str, confirmed_at: datetime | None = None
    ) -> None:
        """Update event status via asyncpg conn if available, else Supabase."""
        if self._pg_conn is not None:
            from tripwire.db.postgres import update_event_status
            await update_event_status(self._pg_conn, event_id, new_status, confirmed_at)
        else:
            await asyncio.to_thread(
                self._event_repo.update_status, event_id, new_status, confirmed_at
            )

    async def _get_endpoint_ids(self, event_id: str) -> list[str]:
        """Fetch endpoint IDs via asyncpg conn if available, else Supabase."""
        if self._pg_conn is not None:
            from tripwire.db.postgres import fetch_event_endpoint_ids
            return await fetch_event_endpoint_ids(self._pg_conn, event_id)
        return await asyncio.to_thread(
            self._event_repo.get_endpoint_ids, event_id
        )

    async def _process_event(
        self,
        event: dict[str, Any],
        chain_id: ChainId,
        current_block: int,
        required: int,
    ) -> None:
        """Check finality depth for a single event (pending or confirmed).

        Handles two transitions in the unified lifecycle:
          - pending  + finality reached → confirmed  + fire payment.confirmed
          - confirmed + finality reached → finalized + fire payment.finalized
        """
        event_id: str = event["id"]
        block_number: int = event["block_number"]
        current_status: str = event.get("status", "pending")

        confirmations = max(0, current_block - block_number)

        # ── Finality promotion ───────────────────────────────────────
        if confirmations >= required:
            # Update finality depth before status transition
            await self._update_finality(event_id, confirmations)

            if current_status == "pending":
                # pending → confirmed: first finality threshold crossed
                logger.info(
                    "event_confirmed",
                    event_id=event_id,
                    confirmations=confirmations,
                    required=required,
                )
                await self._transition_event(
                    event, "confirmed", WebhookEventType.PAYMENT_CONFIRMED
                )
            elif current_status == "confirmed":
                # confirmed → finalized: promoted event now has full finality
                logger.info(
                    "event_finalized",
                    event_id=event_id,
                    confirmations=confirmations,
                    required=required,
                )
                await self._transition_event(
                    event, "finalized", WebhookEventType.PAYMENT_FINALIZED
                )
        else:
            # Update depth even if not yet final (useful for dashboards)
            await self._update_finality(event_id, confirmations)
            logger.debug(
                "event_still_waiting",
                event_id=event_id,
                current_status=current_status,
                confirmations=confirmations,
                required=required,
            )

    # ------------------------------------------------------------------
    # State transitions & webhook dispatch
    # ------------------------------------------------------------------

    async def _transition_event(
        self,
        event: dict[str, Any],
        new_status: str,
        webhook_event_type: WebhookEventType,
    ) -> None:
        """Update event status in the database and fire the appropriate webhook."""
        event_id: str = event["id"]

        # 1. Update status in the DB (uses asyncpg conn if available)
        confirmed_at = datetime.now(timezone.utc) if new_status == "confirmed" else None
        try:
            await self._update_status(event_id, new_status, confirmed_at)
        except Exception:
            logger.exception(
                "finality_poll_status_update_failed",
                event_id=event_id,
                new_status=new_status,
            )
            return

        # 2. Dispatch webhook to ALL matched endpoints via join table (#7)
        try:
            endpoint_ids = await self._get_endpoint_ids(event_id)
        except Exception:
            logger.exception(
                "finality_poll_endpoint_ids_fetch_failed",
                event_id=event_id,
            )
            # Fall back to legacy endpoint_id column
            endpoint_ids = []
            legacy_id = event.get("endpoint_id")
            if legacy_id:
                endpoint_ids = [legacy_id]

        if not endpoint_ids:
            logger.debug(
                "finality_poll_no_endpoints",
                event_id=event_id,
                msg="No endpoints linked to event; skipping webhook dispatch",
            )
            return

        endpoints = []
        for eid in endpoint_ids:
            try:
                ep = await asyncio.to_thread(self._endpoint_repo.get_by_id, eid)
                if ep is not None:
                    endpoints.append(ep)
            except Exception:
                logger.exception(
                    "finality_poll_endpoint_fetch_failed",
                    event_id=event_id,
                    endpoint_id=eid,
                )

        if not endpoints:
            logger.warning(
                "finality_poll_no_active_endpoints",
                event_id=event_id,
                endpoint_ids=endpoint_ids,
            )
            return

        # 3. Reconstruct an ERC3009Transfer from the stored event row so we
        #    can reuse dispatch_event() without changing its signature.
        try:
            transfer = self._reconstruct_transfer(event)
        except Exception:
            logger.exception(
                "finality_poll_transfer_reconstruct_failed",
                event_id=event_id,
            )
            return

        # Build a FinalityStatus reflecting the new state
        finality = FinalityStatus(
            tx_hash=event.get("tx_hash", ""),
            chain_id=ChainId(event["chain_id"]),
            block_number=event.get("block_number", 0),
            confirmations=event.get("finality_depth", 0),
            required_confirmations=FINALITY_DEPTHS.get(
                ChainId(event["chain_id"]), 12
            ),
            is_finalized=(new_status in ("confirmed", "finalized")),
        )

        try:
            message_ids = await dispatch_event(
                transfer=transfer,
                matched_endpoints=endpoints,
                provider=self._webhook_provider,
                event_type=webhook_event_type,
                finality=finality,
            )

            # Record delivery for each endpoint
            for i, ep in enumerate(endpoints):
                msg_id = message_ids[i] if i < len(message_ids) else None
                try:
                    self._delivery_repo.create(
                        endpoint_id=ep.id,
                        event_id=event_id,
                        provider_message_id=msg_id,
                        status="sent" if msg_id else "failed",
                    )
                except Exception:
                    logger.exception(
                        "finality_poll_delivery_record_failed",
                        event_id=event_id,
                        endpoint_id=ep.id,
                    )

            logger.info(
                "finality_webhook_dispatched",
                event_id=event_id,
                event_type=webhook_event_type.value,
                webhooks_sent=len(message_ids),
                endpoints_count=len(endpoints),
            )
        except Exception:
            logger.exception(
                "finality_poll_dispatch_failed",
                event_id=event_id,
                webhook_event_type=webhook_event_type.value,
            )

    # ------------------------------------------------------------------
    # Reorg handling (#2)
    # ------------------------------------------------------------------

    async def _handle_reorg(self, event: dict[str, Any]) -> None:
        """Handle a detected reorg: mark event reorged, invalidate nonce, notify endpoints."""
        event_id: str = event["id"]
        logger.warning(
            "reorg_detected",
            event_id=event_id,
            block_number=event.get("block_number"),
            stored_hash=event.get("block_hash"),
        )

        # 1. Update event status to reorged (uses asyncpg conn if available)
        try:
            await self._update_status(event_id, "reorged")
        except Exception:
            logger.exception("reorg_status_update_failed", event_id=event_id)
            return

        # 2. Invalidate the nonce so it can be reused
        if self._nonce_repo is not None:
            try:
                await asyncio.to_thread(
                    self._nonce_repo.invalidate_by_event_id, event_id
                )
            except Exception:
                logger.exception("reorg_nonce_invalidation_failed", event_id=event_id)

        # 3. Dispatch payment.reorged to all linked endpoints
        try:
            endpoint_ids = await self._get_endpoint_ids(event_id)
        except Exception:
            logger.exception("reorg_endpoint_ids_fetch_failed", event_id=event_id)
            endpoint_ids = []
            legacy_id = event.get("endpoint_id")
            if legacy_id:
                endpoint_ids = [legacy_id]

        if not endpoint_ids:
            return

        endpoints = []
        for eid in endpoint_ids:
            try:
                ep = await asyncio.to_thread(self._endpoint_repo.get_by_id, eid)
                if ep is not None:
                    endpoints.append(ep)
            except Exception:
                logger.exception("reorg_endpoint_fetch_failed", endpoint_id=eid)

        if not endpoints:
            return

        try:
            transfer = self._reconstruct_transfer(event)
        except Exception:
            logger.exception("reorg_transfer_reconstruct_failed", event_id=event_id)
            return

        try:
            message_ids = await dispatch_event(
                transfer=transfer,
                matched_endpoints=endpoints,
                provider=self._webhook_provider,
                event_type=WebhookEventType.PAYMENT_REORGED,
            )
            logger.info(
                "reorg_webhook_dispatched",
                event_id=event_id,
                webhooks_sent=len(message_ids),
                endpoints_count=len(endpoints),
            )
        except Exception:
            logger.exception("reorg_dispatch_failed", event_id=event_id)

    # ------------------------------------------------------------------
    # Data access helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _reconstruct_transfer(event: dict[str, Any]) -> ERC3009Transfer:
        """Reconstruct an ERC3009Transfer from a stored event row.

        The events table stores the same structured columns that the original
        transfer had (chain_id, tx_hash, block_number, block_hash, etc.), so
        we can rebuild the model for dispatch_event() reuse.
        """
        return ERC3009Transfer(
            chain_id=ChainId(event["chain_id"]),
            tx_hash=event["tx_hash"],
            block_number=event["block_number"],
            block_hash=event.get("block_hash", ""),
            log_index=event.get("log_index", 0),
            from_address=event["from_address"],
            to_address=event["to_address"],
            value=str(event.get("amount", "0")),
            authorizer=event.get("authorizer", event["from_address"]),
            valid_after=0,
            valid_before=0,
            nonce=event.get("nonce", ""),
            token=event.get("token", ""),
            timestamp=0,
        )

    def _fetch_pending_events(self, chain_id: ChainId) -> list[dict[str, Any]]:
        """Query the events table for pending and confirmed events on *chain_id*.

        Fetches events with status IN ('pending', 'confirmed') that have a
        real block_number (> 0). This excludes pre_confirmed events (which
        have block_number=0 since they have no onchain tx yet) and picks up
        confirmed events that need finalization promotion.

        Uses the Supabase client through the EventRepository's underlying
        client to run a filtered query.
        """
        result = (
            self._event_repo._sb.table("events")
            .select("*")
            .in_("status", ["pending", "confirmed"])
            .gt("block_number", 0)
            .eq("chain_id", chain_id.value)
            .order("block_number", desc=False)
            .limit(100)
            .execute()
        )
        return result.data
