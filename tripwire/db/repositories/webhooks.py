"""Webhook delivery tracking repository."""

from datetime import datetime, timezone

import structlog
from nanoid import generate as nanoid
from supabase import Client

logger = structlog.get_logger(__name__)


class WebhookDeliveryRepository:
    """Tracks webhook delivery attempts and their Svix message IDs."""

    def __init__(self, client: Client) -> None:
        self._sb = client

    def create(
        self,
        endpoint_id: str,
        event_id: str,
        svix_message_id: str | None = None,
        status: str = "pending",
    ) -> dict:
        """Record a new webhook delivery attempt."""
        delivery_id = nanoid(size=21)
        row = {
            "id": delivery_id,
            "endpoint_id": endpoint_id,
            "event_id": event_id,
            "svix_message_id": svix_message_id,
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

    def set_svix_message_id(self, delivery_id: str, svix_message_id: str) -> dict | None:
        """Set the Svix message ID after a successful send."""
        result = (
            self._sb.table("webhook_deliveries")
            .update({"svix_message_id": svix_message_id, "status": "sent"})
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
