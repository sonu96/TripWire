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

    def insert_batch(self, events: list[dict]) -> list[dict]:
        """Bulk-insert events. Returns all inserted rows."""
        if not events:
            return []
        result = self._sb.table("events").insert(events).execute()
        logger.info("events_batch_inserted", count=len(result.data))
        return result.data

    def get_by_id(self, event_id: str) -> dict | None:
        """Fetch a single event by ID."""
        result = self._sb.table("events").select("*").eq("id", event_id).execute()
        return result.data[0] if result.data else None

    def get_by_tx_hash(self, chain_id: int, tx_hash: str) -> list[dict]:
        """Fetch all events for a given transaction."""
        result = (
            self._sb.table("events")
            .select("*")
            .eq("chain_id", chain_id)
            .eq("tx_hash", tx_hash)
            .execute()
        )
        return result.data

    def list_paginated(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        chain_id: int | None = None,
        status: str | None = None,
        to_address: str | None = None,
    ) -> dict[str, Any]:
        """Cursor-paginated event listing.

        Cursor is the `created_at` timestamp of the last item. Returns
        {"data": [...], "next_cursor": str | None}.
        """
        query = self._sb.table("events").select("*")

        if chain_id is not None:
            query = query.eq("chain_id", chain_id)
        if status is not None:
            query = query.eq("status", status)
        if to_address is not None:
            query = query.eq("to_address", to_address.lower())
        if cursor is not None:
            query = query.lt("created_at", cursor)

        query = query.order("created_at", desc=True).limit(limit)
        result = query.execute()

        rows = result.data
        next_cursor = rows[-1]["created_at"] if len(rows) == limit else None

        return {"data": rows, "next_cursor": next_cursor}

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

    def update_identity(self, event_id: str, identity_data: dict) -> dict | None:
        """Attach identity data to an event."""
        result = (
            self._sb.table("events")
            .update({"identity_data": identity_data})
            .eq("id", event_id)
            .execute()
        )
        return result.data[0] if result.data else None
