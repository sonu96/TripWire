"""Realtime notifier for Notify-mode event delivery.

Inserts events into the `realtime_events` table so that Supabase Realtime
automatically pushes them to subscribed clients via WebSocket.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import structlog
from supabase import Client

from tripwire.types.models import (
    AgentIdentity,
    ERC3009Transfer,
    Endpoint,
    FinalityStatus,
    TransferData,
    WebhookEventType,
    build_finality_data,
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

        data: dict = {
            "transfer": transfer_data.model_dump(),
            "timestamp": int(time.time()),
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

        data: dict = {
            "transfer": transfer_data.model_dump(),
            "timestamp": int(time.time()),
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
