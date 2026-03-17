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

    def promote_to_confirmed(
        self,
        event_id: str,
        tx_hash: str,
        block_number: int,
        block_hash: str,
        log_index: int,
    ) -> dict | None:
        """Promote a pre_confirmed event to confirmed with real onchain data.

        Updates the event row with the real tx_hash, block_number, block_hash,
        log_index, and transitions status from pre_confirmed to confirmed.
        Returns the updated row or None if the event was not found.
        """
        now = datetime.now(timezone.utc).isoformat()
        updates: dict[str, Any] = {
            "tx_hash": tx_hash,
            "block_number": block_number,
            "block_hash": block_hash,
            "log_index": log_index,
            "status": "confirmed",
            "confirmed_at": now,
        }

        result = (
            self._sb.table("events")
            .update(updates)
            .eq("id", event_id)
            .execute()
        )
        if result.data:
            logger.info(
                "event_promoted_to_confirmed",
                event_id=event_id,
                tx_hash=tx_hash,
                block_number=block_number,
            )
            return result.data[0]
        return None

    # ── event_endpoints join table (#7) ───────────────────────────

    def link_endpoints(self, event_id: str, endpoint_ids: list[str]) -> None:
        """Link an event to multiple endpoints via the join table."""
        if not endpoint_ids:
            return
        rows = [
            {"event_id": event_id, "endpoint_id": eid}
            for eid in endpoint_ids
        ]
        try:
            self._sb.table("event_endpoints").upsert(
                rows,
                on_conflict="event_id,endpoint_id",
                ignore_duplicates=True,
            ).execute()
        except Exception:
            logger.exception(
                "link_endpoints_failed",
                event_id=event_id,
                endpoint_ids=endpoint_ids,
            )

    def get_endpoint_ids(self, event_id: str) -> list[str]:
        """Return all endpoint IDs linked to an event."""
        result = (
            self._sb.table("event_endpoints")
            .select("endpoint_id")
            .eq("event_id", event_id)
            .execute()
        )
        return [row["endpoint_id"] for row in result.data]

