"""Realtime notifier for Notify-mode event delivery.

Inserts events into the `realtime_events` table so that Supabase Realtime
automatically pushes them to subscribed clients via WebSocket.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from supabase import Client

from tripwire.types.models import (
    AgentIdentity,
    ERC3009Transfer,
    Endpoint,
    ExecutionBlock,
    ExecutionState,
    FinalityData,
    FinalityStatus,
    TransferData,
    TrustSource,
    WebhookEventType,
    build_finality_data,
    derive_execution_metadata,
)
from tripwire.webhook.dispatcher import build_transfer_data

logger = structlog.get_logger(__name__)


class RealtimeNotifier:
    """Pushes events to Supabase Realtime by inserting into `realtime_events`."""

    def __init__(self, supabase: Client) -> None:
        self._sb = supabase

    async def notify(
        self,
        endpoint: Endpoint,
        transfer: ERC3009Transfer,
        event_type: WebhookEventType,
        finality: FinalityStatus | None = None,
        identity: AgentIdentity | None = None,
    ) -> str:
        """Insert a realtime event row for a single endpoint.

        Returns the generated event id.
        """
        event_id = str(uuid.uuid4())

        # Build the same payload structure used by the webhook dispatcher
        transfer_data: TransferData = build_transfer_data(transfer)
        finality_data = build_finality_data(finality)

        execution = derive_execution_metadata(event_type, finality_data)

        data: dict = {
            "transfer": transfer_data.model_dump(),
            "timestamp": int(time.time()),
            "version": "v1",
            "execution": execution.model_dump(),
        }
        if finality_data is not None:
            data["finality"] = finality_data.model_dump()
        if identity is not None:
            data["identity"] = identity.model_dump()

        row = {
            "id": event_id,
            "endpoint_id": endpoint.id,
            "type": event_type.value,
            "data": data,
            "chain_id": transfer.chain_id.value,
            "recipient": transfer.to_address.lower(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            self._sb.table("realtime_events").insert(row).execute()
            logger.info(
                "realtime_event_inserted",
                event_id=event_id,
                endpoint_id=endpoint.id,
                tx_hash=transfer.tx_hash,
                event_type=event_type.value,
            )
        except Exception:
            logger.exception(
                "realtime_event_insert_failed",
                event_id=event_id,
                endpoint_id=endpoint.id,
                tx_hash=transfer.tx_hash,
            )

        return event_id

    async def notify_batch(
        self,
        endpoints: list[Endpoint],
        transfer: ERC3009Transfer,
        event_type: WebhookEventType,
        finality: FinalityStatus | None = None,
        identity: AgentIdentity | None = None,
    ) -> list[str]:
        """Insert realtime event rows for multiple endpoints in a single bulk insert.

        Returns a list of generated event ids.
        """
        if not endpoints:
            return []

        # Build the shared payload once (same for all endpoints)
        transfer_data: TransferData = build_transfer_data(transfer)
        finality_data = build_finality_data(finality)

        execution = derive_execution_metadata(event_type, finality_data)

        data: dict = {
            "transfer": transfer_data.model_dump(),
            "timestamp": int(time.time()),
            "version": "v1",
            "execution": execution.model_dump(),
        }
        if finality_data is not None:
            data["finality"] = finality_data.model_dump()
        if identity is not None:
            data["identity"] = identity.model_dump()

        now = datetime.now(timezone.utc).isoformat()
        rows = []
        event_ids = []
        for endpoint in endpoints:
            event_id = str(uuid.uuid4())
            event_ids.append(event_id)
            rows.append({
                "id": event_id,
                "endpoint_id": endpoint.id,
                "type": event_type.value,
                "data": data,
                "chain_id": transfer.chain_id.value,
                "recipient": transfer.to_address.lower(),
                "created_at": now,
            })

        try:
            self._sb.table("realtime_events").insert(rows).execute()
            logger.info(
                "realtime_events_batch_inserted",
                count=len(rows),
                tx_hash=transfer.tx_hash,
                event_type=event_type.value,
            )
        except Exception:
            logger.exception(
                "realtime_events_batch_insert_failed",
                count=len(rows),
                tx_hash=transfer.tx_hash,
            )

        return event_ids

    # ── Generic (Pulse) notification ─────────────────────────────────

    async def notify_generic(
        self,
        event: dict[str, Any],
        endpoint_ids: list[str],
        execution_block: dict[str, Any] | None = None,
        identity_data: dict[str, Any] | None = None,
    ) -> list[str]:
        """Notify subscribed clients of a generic onchain event (Pulse).

        Inserts into the same ``realtime_events`` table but with an
        event-neutral payload structure. The ``data`` column contains the
        full event dict under a ``"event"`` key rather than ``"transfer"``.

        Parameters
        ----------
        event:
            OnchainEvent-shaped dict. Should contain at least ``"event_id"``
            (or ``"id"``), ``"event_type"``, and ``"chain_id"``.
        endpoint_ids:
            List of endpoint IDs to notify.
        execution_block:
            Optional pre-built execution metadata dict. If ``None``, defaults
            to confirmed/not-safe.
        identity_data:
            Optional identity dict (AgentIdentity-shaped) for enrichment.

        Returns
        -------
        list[str]
            List of generated event IDs (one per endpoint).
        """
        if not endpoint_ids:
            return []

        event_type_str = event.get("event_type", "trigger.matched")
        chain_id = event.get("chain_id")
        tx_hash = event.get("tx_hash", "")
        contract_address = event.get("contract_address", "")

        # Build execution block
        if execution_block is not None:
            exec_data = execution_block
        else:
            exec_data = {
                "state": ExecutionState.CONFIRMED.value,
                "safe_to_execute": False,
                "trust_source": TrustSource.ONCHAIN.value,
                "finality": None,
            }

        data: dict[str, Any] = {
            "event": event,
            "timestamp": int(time.time()),
            "version": "v1",
            "execution": exec_data,
        }
        if identity_data is not None:
            data["identity"] = identity_data

        now = datetime.now(timezone.utc).isoformat()
        rows: list[dict[str, Any]] = []
        generated_ids: list[str] = []

        for ep_id in endpoint_ids:
            event_id = str(uuid.uuid4())
            generated_ids.append(event_id)
            rows.append({
                "id": event_id,
                "endpoint_id": ep_id,
                "type": event_type_str,
                "data": data,
                "chain_id": chain_id,
                "recipient": contract_address,
                "created_at": now,
            })

        try:
            self._sb.table("realtime_events").insert(rows).execute()
            logger.info(
                "realtime_generic_events_inserted",
                count=len(rows),
                event_type=event_type_str,
                tx_hash=tx_hash,
            )
        except Exception:
            logger.exception(
                "realtime_generic_events_insert_failed",
                count=len(rows),
                event_type=event_type_str,
                tx_hash=tx_hash,
            )

        return generated_ids
