"""Finality poller — background task that promotes pending events to confirmed
and detects chain reorgs.

Runs as an asyncio background task started during the FastAPI lifespan.  Each
supported chain is polled on its own cadence (fast for L2s, slower for L1)
because finality semantics differ:

  - Arbitrum: ~250ms block time, 1 confirmation needed → poll every 5s
  - Base:     2s block time, 3 confirmations → poll every 10s
  - Ethereum: 12s block time, 12 confirmations → poll every 30s

For every pending event the poller:
  1. Fetches the current block number via JSON-RPC.
  2. Computes confirmations = current_block - event.block_number.
  3. Checks whether the block_hash at event.block_number still matches;
     a mismatch means a reorg occurred.
  4. Transitions the event to ``confirmed`` or ``reorged`` and fires the
     appropriate webhook (``payment.confirmed`` / ``payment.reorged``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

from tripwire.config.settings import Settings
from tripwire.db.repositories.endpoints import EndpointRepository
from tripwire.db.repositories.events import EventRepository
from tripwire.db.repositories.webhooks import WebhookDeliveryRepository
from tripwire.ingestion.finality import get_block_number, _get_rpc_client, _RPC_URLS
from tripwire.types.models import (
    ERC3009Transfer,
    FINALITY_DEPTHS,
    ChainId,
    FinalityStatus,
    WebhookEventType,
)
from tripwire.webhook.dispatcher import dispatch_event
from tripwire.webhook.provider import WebhookProvider

logger = structlog.get_logger(__name__)

# Maps ChainId → settings attribute name for poll interval
_CHAIN_INTERVAL_ATTR: dict[ChainId, str] = {
    ChainId.ARBITRUM: "finality_poll_interval_arbitrum",
    ChainId.BASE: "finality_poll_interval_base",
    ChainId.ETHEREUM: "finality_poll_interval_ethereum",
}


class FinalityPoller:
    """Background poller that confirms pending events and detects reorgs.

    Spawns one asyncio task per chain so each chain runs on its own poll
    cadence.  All tasks share the persistent httpx client from
    ``finality.py`` for RPC calls.
    """

    def __init__(
        self,
        event_repo: EventRepository,
        endpoint_repo: EndpointRepository,
        delivery_repo: WebhookDeliveryRepository,
        webhook_provider: WebhookProvider,
        settings: Settings,
    ) -> None:
        self._event_repo = event_repo
        self._endpoint_repo = endpoint_repo
        self._delivery_repo = delivery_repo
        self._webhook_provider = webhook_provider
        self._settings = settings
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn one polling task per supported chain."""
        if self._tasks:
            logger.warning("finality_poller_already_running")
            return

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
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("finality_poll_error", chain=chain_id.name)

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    # ------------------------------------------------------------------
    # Single poll iteration
    # ------------------------------------------------------------------

    async def _poll_chain(self, chain_id: ChainId) -> None:
        """Fetch pending events for *chain_id* and process each one."""
        # Query events table for status="pending" on this chain
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

        for event in pending_events:
            try:
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

    async def _process_event(
        self,
        event: dict[str, Any],
        chain_id: ChainId,
        current_block: int,
        required: int,
    ) -> None:
        """Check finality and reorg status for a single pending event."""
        event_id: str = event["id"]
        block_number: int = event["block_number"]
        stored_block_hash: str = event.get("block_hash", "")

        confirmations = max(0, current_block - block_number)

        # ── Reorg detection ──────────────────────────────────────────
        if stored_block_hash:
            try:
                live_block_hash = await self._fetch_block_hash(chain_id, block_number)
            except Exception:
                logger.exception(
                    "finality_poll_hash_fetch_failed",
                    event_id=event_id,
                    block_number=block_number,
                )
                # Can't determine reorg; skip this event and retry next cycle
                return

            if live_block_hash and live_block_hash != stored_block_hash:
                logger.warning(
                    "reorg_detected",
                    event_id=event_id,
                    block_number=block_number,
                    stored_hash=stored_block_hash,
                    live_hash=live_block_hash,
                )
                await self._transition_event(
                    event, "reorged", WebhookEventType.PAYMENT_REORGED
                )
                return

        # ── Finality promotion ───────────────────────────────────────
        if confirmations >= required:
            logger.info(
                "event_finalized",
                event_id=event_id,
                confirmations=confirmations,
                required=required,
            )
            # Update finality depth before status transition
            await asyncio.to_thread(
                self._event_repo.update_finality, event_id, confirmations
            )
            await self._transition_event(
                event, "confirmed", WebhookEventType.PAYMENT_CONFIRMED
            )
        else:
            # Update depth even if not yet final (useful for dashboards)
            await asyncio.to_thread(
                self._event_repo.update_finality, event_id, confirmations
            )
            logger.debug(
                "event_still_pending",
                event_id=event_id,
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

        # 1. Update status in the DB
        confirmed_at = datetime.now(timezone.utc) if new_status == "confirmed" else None
        try:
            await asyncio.to_thread(
                self._event_repo.update_status,
                event_id,
                new_status,
                confirmed_at,
            )
        except Exception:
            logger.exception(
                "finality_poll_status_update_failed",
                event_id=event_id,
                new_status=new_status,
            )
            return

        # 2. Dispatch webhook to matched endpoints
        endpoint_id = event.get("endpoint_id")
        if not endpoint_id:
            logger.debug(
                "finality_poll_no_endpoint",
                event_id=event_id,
                msg="No endpoint_id on event; skipping webhook dispatch",
            )
            return

        try:
            endpoint = await asyncio.to_thread(
                self._endpoint_repo.get_by_id, endpoint_id
            )
        except Exception:
            logger.exception(
                "finality_poll_endpoint_fetch_failed",
                event_id=event_id,
                endpoint_id=endpoint_id,
            )
            return

        if endpoint is None:
            logger.warning(
                "finality_poll_endpoint_not_found",
                event_id=event_id,
                endpoint_id=endpoint_id,
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
            is_finalized=(new_status == "confirmed"),
        )

        try:
            message_ids = await dispatch_event(
                transfer=transfer,
                matched_endpoints=[endpoint],
                provider=self._webhook_provider,
                event_type=webhook_event_type,
                finality=finality,
            )

            # Record delivery
            for msg_id in message_ids:
                try:
                    self._delivery_repo.create(
                        endpoint_id=endpoint_id,
                        event_id=event_id,
                        provider_message_id=msg_id,
                        status="sent" if msg_id else "failed",
                    )
                except Exception:
                    logger.exception(
                        "finality_poll_delivery_record_failed",
                        event_id=event_id,
                    )

            logger.info(
                "finality_webhook_dispatched",
                event_id=event_id,
                event_type=webhook_event_type.value,
                webhooks_sent=len(message_ids),
            )
        except Exception:
            logger.exception(
                "finality_poll_dispatch_failed",
                event_id=event_id,
                webhook_event_type=webhook_event_type.value,
            )

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
        """Query the events table for pending events on *chain_id*.

        Uses the Supabase client through the EventRepository's underlying
        client to run a filtered query.
        """
        result = (
            self._event_repo._sb.table("events")
            .select("*")
            .eq("status", "pending")
            .eq("chain_id", chain_id.value)
            .order("block_number", desc=False)
            .limit(100)
            .execute()
        )
        return result.data

    async def _fetch_block_hash(self, chain_id: ChainId, block_number: int) -> str:
        """Fetch the block hash for a specific block number via JSON-RPC.

        Uses eth_getBlockByNumber with ``false`` (no full txs) and extracts
        the ``hash`` field.  Returns empty string on unexpected responses.
        """
        rpc_url = _RPC_URLS[chain_id]
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getBlockByNumber",
            "params": [hex(block_number), False],
            "id": 1,
        }
        client = _get_rpc_client()
        resp = await client.post(rpc_url, json=payload)
        resp.raise_for_status()

        data = resp.json()
        if "error" in data:
            raise RuntimeError(
                f"RPC error fetching block hash on chain {chain_id}: {data['error']}"
            )

        block = data.get("result")
        if block is None:
            # Block not found (pruned or too far ahead) — treat as inconclusive
            logger.warning(
                "finality_poll_block_not_found",
                chain=chain_id.name,
                block_number=block_number,
            )
            return ""

        return block.get("hash", "")
