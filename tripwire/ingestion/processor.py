"""End-to-end ingestion pipeline orchestrator.

Receives a raw Goldsky-decoded event and runs the full pipeline:
  decode → nonce dedup → finality → identity → policy → dispatch
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from supabase import Client

from tripwire.api.policies.engine import evaluate_policy
from tripwire.db.repositories.nonces import NonceRepository
from tripwire.identity.resolver import IdentityResolver
from tripwire.ingestion.decoder import decode_transfer_event
from tripwire.ingestion.finality import check_finality
from tripwire.types.models import (
    Endpoint,
    EndpointPolicies,
    WebhookEventType,
)
from tripwire.webhook.dispatcher import (
    build_transfer_data,
    dispatch_event,
    match_endpoints,
)

logger = structlog.get_logger(__name__)


class EventProcessor:
    """Orchestrates the full ingestion→dispatch pipeline."""

    def __init__(
        self,
        supabase: Client,
        identity_resolver: IdentityResolver,
        nonce_repo: NonceRepository,
    ) -> None:
        self._sb = supabase
        self._resolver = identity_resolver
        self._nonce_repo = nonce_repo

    async def process_event(self, raw_log: dict[str, Any]) -> dict[str, Any]:
        """Process a single Goldsky-decoded event through the full pipeline.

        Returns a summary dict with processing results.
        """
        # 1. Decode the raw event into an ERC3009Transfer
        try:
            transfer = decode_transfer_event(raw_log)
        except Exception:
            logger.exception("decode_failed", raw_log=raw_log)
            return {"status": "error", "reason": "decode_failed"}

        tx_hash = transfer.tx_hash
        chain_id = transfer.chain_id

        logger.info(
            "processing_event",
            tx_hash=tx_hash,
            chain_id=chain_id.value,
            authorizer=transfer.authorizer,
        )

        # 2. Nonce deduplication
        is_new = self._nonce_repo.record_nonce(
            chain_id=chain_id.value,
            nonce=transfer.nonce,
            authorizer=transfer.authorizer,
        )
        if not is_new:
            logger.info("duplicate_nonce", tx_hash=tx_hash, nonce=transfer.nonce)
            return {"status": "duplicate", "tx_hash": tx_hash}

        # 3. Finality check
        try:
            finality = await check_finality(transfer)
        except Exception:
            logger.exception("finality_check_failed", tx_hash=tx_hash)
            finality = None

        # 4. Identity resolution
        identity = None
        try:
            identity = await self._resolver.resolve(
                transfer.authorizer, chain_id.value
            )
        except Exception:
            logger.warning("identity_resolve_failed", authorizer=transfer.authorizer)

        # 5. Match endpoints for this transfer's recipient + chain
        endpoints = self._fetch_matching_endpoints(transfer)
        if not endpoints:
            logger.info("no_matching_endpoints", tx_hash=tx_hash)
            # Still record the event even if no endpoints match
            event_id = self._record_event(
                transfer, finality, identity, event_type=WebhookEventType.PAYMENT_CONFIRMED
            )
            return {
                "status": "no_endpoints",
                "tx_hash": tx_hash,
                "event_id": event_id,
            }

        matched = match_endpoints(transfer, endpoints)
        if not matched:
            logger.info("no_endpoints_matched_filters", tx_hash=tx_hash)
            event_id = self._record_event(
                transfer, finality, identity, event_type=WebhookEventType.PAYMENT_CONFIRMED
            )
            return {
                "status": "no_match",
                "tx_hash": tx_hash,
                "event_id": event_id,
            }

        # 6. Policy evaluation — filter out endpoints that fail policy
        transfer_data = build_transfer_data(transfer)
        approved_endpoints: list[Endpoint] = []

        for ep in matched:
            policies = ep.policies or EndpointPolicies()
            allowed, reason = evaluate_policy(transfer_data, identity, policies)
            if allowed:
                approved_endpoints.append(ep)
            else:
                logger.info(
                    "policy_rejected",
                    endpoint_id=ep.id,
                    tx_hash=tx_hash,
                    reason=reason,
                )

        # Determine event type based on finality
        if finality and finality.is_finalized:
            event_type = WebhookEventType.PAYMENT_CONFIRMED
        elif finality:
            event_type = WebhookEventType.PAYMENT_PENDING
        else:
            event_type = WebhookEventType.PAYMENT_CONFIRMED

        # 7. Record the event
        event_id = self._record_event(transfer, finality, identity, event_type)

        # 8. Dispatch webhooks via Svix
        message_ids: list[str] = []
        if approved_endpoints:
            try:
                message_ids = await dispatch_event(
                    transfer=transfer,
                    matched_endpoints=approved_endpoints,
                    event_type=event_type,
                    finality=finality,
                    identity=identity,
                )
            except Exception:
                logger.exception("dispatch_failed", tx_hash=tx_hash)

            # Record webhook deliveries
            for i, ep in enumerate(approved_endpoints):
                msg_id = message_ids[i] if i < len(message_ids) else None
                self._record_delivery(
                    endpoint_id=ep.id,
                    event_id=event_id,
                    svix_message_id=msg_id,
                )

        logger.info(
            "event_processed",
            tx_hash=tx_hash,
            event_id=event_id,
            endpoints_matched=len(matched),
            endpoints_approved=len(approved_endpoints),
            webhooks_sent=len(message_ids),
            finalized=finality.is_finalized if finality else None,
        )

        return {
            "status": "processed",
            "tx_hash": tx_hash,
            "event_id": event_id,
            "endpoints_matched": len(matched),
            "endpoints_approved": len(approved_endpoints),
            "webhooks_sent": len(message_ids),
            "message_ids": message_ids,
        }

    async def process_batch(
        self, raw_logs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Process a batch of Goldsky-decoded events."""
        results = []
        for raw_log in raw_logs:
            result = await self.process_event(raw_log)
            results.append(result)
        return results

    # ── Internal helpers ────────────────────────────────────────

    def _fetch_matching_endpoints(self, transfer) -> list[Endpoint]:
        """Fetch active endpoints whose recipient matches the transfer."""
        result = (
            self._sb.table("endpoints")
            .select("*")
            .eq("recipient", transfer.to_address.lower())
            .eq("active", True)
            .execute()
        )
        return [Endpoint(**row) for row in result.data]

    def _record_event(self, transfer, finality, identity, event_type) -> str:
        """Insert an event row into the events table."""
        event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # JSONB data column (used by the events API routes)
        data: dict[str, Any] = {
            "chain_id": transfer.chain_id.value,
            "tx_hash": transfer.tx_hash,
            "from_address": transfer.from_address,
            "to_address": transfer.to_address,
            "amount": transfer.value,
            "nonce": transfer.nonce,
            "token": transfer.token,
        }
        if finality:
            data["finality"] = {
                "confirmations": finality.confirmations,
                "required_confirmations": finality.required_confirmations,
                "is_finalized": finality.is_finalized,
            }
        if identity:
            data["identity"] = identity.model_dump()

        row: dict[str, Any] = {
            "id": event_id,
            "type": event_type.value,
            "data": data,
            "created_at": now,
            # Structured columns
            "chain_id": transfer.chain_id.value,
            "tx_hash": transfer.tx_hash,
            "block_number": transfer.block_number,
            "block_hash": transfer.block_hash,
            "log_index": transfer.log_index if hasattr(transfer, "log_index") else 0,
            "from_address": transfer.from_address,
            "to_address": transfer.to_address,
            "amount": transfer.value,
            "authorizer": transfer.authorizer,
            "nonce": transfer.nonce,
            "token": transfer.token,
            "status": "confirmed" if (finality and finality.is_finalized) else "pending",
            "finality_depth": finality.confirmations if finality else 0,
        }

        if identity:
            row["identity_data"] = identity.model_dump()

        if finality and finality.is_finalized:
            row["confirmed_at"] = now

        try:
            self._sb.table("events").insert(row).execute()
        except Exception:
            logger.exception("event_record_failed", event_id=event_id)

        return event_id

    def _record_delivery(
        self, endpoint_id: str, event_id: str, svix_message_id: str | None
    ) -> None:
        """Record a webhook delivery attempt."""
        from nanoid import generate as nanoid

        row = {
            "id": nanoid(size=21),
            "endpoint_id": endpoint_id,
            "event_id": event_id,
            "svix_message_id": svix_message_id,
            "status": "sent" if svix_message_id else "failed",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            self._sb.table("webhook_deliveries").insert(row).execute()
        except Exception:
            logger.exception(
                "delivery_record_failed",
                endpoint_id=endpoint_id,
                event_id=event_id,
            )
