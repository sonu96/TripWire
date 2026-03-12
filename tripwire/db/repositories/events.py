"""Event storage repository with cursor pagination."""

from datetime import datetime, timezone
from typing import Any

import structlog
from supabase import Client

logger = structlog.get_logger(__name__)


class EventRepository:
    """CRUD operations for the events table."""

    def __init__(self, client: Client) -> None:
        self._sb = client

    def insert(self, event: dict) -> dict:
        """Insert a single event row. Returns the inserted row."""
        result = self._sb.table("events").insert(event).execute()
        logger.info("event_inserted", event_id=event.get("id"))
        return result.data[0]

    def get_by_id(self, event_id: str) -> dict | None:
        """Fetch a single event by ID."""
        result = self._sb.table("events").select("*").eq("id", event_id).execute()
        return result.data[0] if result.data else None

    def update_status(
        self,
        event_id: str,
        status: str,
        confirmed_at: datetime | None = None,
    ) -> dict | None:
        """Update the status (and optionally confirmed_at) of an event."""
        updates: dict[str, Any] = {"status": status}
        if confirmed_at is not None:
            updates["confirmed_at"] = confirmed_at.isoformat()
        elif status == "confirmed":
            updates["confirmed_at"] = datetime.now(timezone.utc).isoformat()

        result = (
            self._sb.table("events")
            .update(updates)
            .eq("id", event_id)
            .execute()
        )
        if result.data:
            logger.info("event_status_updated", event_id=event_id, status=status)
            return result.data[0]
        return None

    def update_finality(self, event_id: str, depth: int) -> dict | None:
        """Update the finality_depth for an event."""
        result = (
            self._sb.table("events")
            .update({"finality_depth": depth})
            .eq("id", event_id)
            .execute()
        )
        return result.data[0] if result.data else None

