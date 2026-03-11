"""Webhook delivery tracking repository."""

from datetime import datetime, timezone

import structlog
from nanoid import generate as nanoid
from supabase import Client

logger = structlog.get_logger(__name__)


class WebhookDeliveryRepository:
    """Tracks webhook delivery attempts and their provider message IDs."""

    def __init__(self, client: Client) -> None:
        self._sb = client

    def create(
        self,
        endpoint_id: str,
        event_id: str,
        provider_message_id: str | None = None,
        status: str = "pending",
        delivery_method: str = "convoy",
    ) -> dict:
        """Record a new webhook delivery attempt."""
        delivery_id = nanoid(size=21)
        row = {
            "id": delivery_id,
            "endpoint_id": endpoint_id,
            "event_id": event_id,
            "provider_message_id": provider_message_id,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        result = self._sb.table("webhook_deliveries").insert(row).execute()
        logger.info(
            "webhook_delivery_created",
            delivery_id=delivery_id,
            endpoint_id=endpoint_id,
            event_id=event_id,
        )
        return result.data[0]

    def update_status(self, delivery_id: str, status: str) -> dict | None:
        """Update the status of a delivery (e.g. sent, failed, delivered)."""
        result = (
            self._sb.table("webhook_deliveries")
            .update({"status": status})
            .eq("id", delivery_id)
            .execute()
        )
        if result.data:
            logger.info("webhook_delivery_status_updated", delivery_id=delivery_id, status=status)
            return result.data[0]
        return None

    def set_provider_message_id(self, delivery_id: str, provider_message_id: str) -> dict | None:
        """Set the provider message ID after a successful send."""
        result = (
            self._sb.table("webhook_deliveries")
            .update({"provider_message_id": provider_message_id, "status": "sent"})
            .eq("id", delivery_id)
            .execute()
        )
        return result.data[0] if result.data else None

    def get_by_event(self, event_id: str) -> list[dict]:
        """Get all delivery records for a given event."""
        result = (
            self._sb.table("webhook_deliveries")
            .select("*")
            .eq("event_id", event_id)
            .order("created_at", desc=True)
            .execute()
        )
        return result.data

    def get_by_endpoint(
        self,
        endpoint_id: str,
        *,
        limit: int = 50,
        status: str | None = None,
    ) -> list[dict]:
        """Get recent deliveries for an endpoint, optionally filtered by status."""
        query = (
            self._sb.table("webhook_deliveries")
            .select("*")
            .eq("endpoint_id", endpoint_id)
        )
        if status is not None:
            query = query.eq("status", status)
        query = query.order("created_at", desc=True).limit(limit)
        return query.execute().data

    def get_by_id(self, delivery_id: str) -> dict | None:
        """Get a single delivery by ID."""
        result = (
            self._sb.table("webhook_deliveries")
            .select("*")
            .eq("id", delivery_id)
            .execute()
        )
        return result.data[0] if result.data else None

    def get_by_provider_message_id(self, provider_message_id: str) -> dict | None:
        """Get a single delivery by its provider (Convoy) message ID."""
        result = (
            self._sb.table("webhook_deliveries")
            .select("*")
            .eq("provider_message_id", provider_message_id)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def list_paginated(
        self,
        *,
        endpoint_id: str | None = None,
        event_id: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List deliveries with optional filters and keyset pagination."""
        query = (
            self._sb.table("webhook_deliveries")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit + 1)
        )

        if cursor:
            cursor_row = (
                self._sb.table("webhook_deliveries")
                .select("created_at")
                .eq("id", cursor)
                .execute()
            )
            if cursor_row.data:
                query = query.lt("created_at", cursor_row.data[0]["created_at"])

        if endpoint_id:
            query = query.eq("endpoint_id", endpoint_id)
        if event_id:
            query = query.eq("event_id", event_id)
        if status:
            query = query.eq("status", status)

        return query.execute().data

    def get_stats_for_endpoint(self, endpoint_id: str) -> dict:
        """Get delivery counts grouped by status for an endpoint."""
        result = (
            self._sb.table("webhook_deliveries")
            .select("status")
            .eq("endpoint_id", endpoint_id)
            .execute()
        )
        rows = result.data

        counts = {"pending": 0, "sent": 0, "delivered": 0, "failed": 0}
        for row in rows:
            s = row.get("status", "pending")
            if s in counts:
                counts[s] += 1

        total = len(rows)
        success = counts["delivered"] + counts["sent"]
        success_rate = round(success / total, 4) if total > 0 else 0.0

        return {
            "endpoint_id": endpoint_id,
            "total": total,
            **counts,
            "success_rate": success_rate,
        }
