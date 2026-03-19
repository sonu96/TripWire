"""Event storage repository with cursor pagination.

Supports both Keeper (payment) events and Pulse (generic trigger) events
via the event-neutral schema (migration 026).
"""

from datetime import datetime, timezone
from typing import Any

import structlog
from nanoid import generate as nanoid
from supabase import Client

logger = structlog.get_logger(__name__)


class EventRepository:
    """CRUD operations for the events table.

    Handles both legacy payment events (Keeper) and generic trigger events
    (Pulse).  New columns added in migration 026: ``event_type``,
    ``decoded_fields``, ``source``, ``trigger_id``, ``product_source``.
    """

    def __init__(self, client: Client) -> None:
        self._sb = client

    # ── Legacy / Keeper insert (unchanged interface) ─────────────

    def insert(self, event: dict) -> dict:
        """Insert a single event row. Returns the inserted row.

        This is the original insert path used by the Keeper payment pipeline.
        It works unchanged — new columns have server-side defaults.
        """
        result = self._sb.table("events").insert(event).execute()
        logger.info("event_inserted", event_id=event.get("id"))
        return result.data[0]

    # ── Generic / Pulse insert ───────────────────────────────────

    def insert_generic_event(self, event: dict) -> str:
        """Insert an event-neutral event (Pulse triggers).

        Accepts a dict with at minimum:
            - chain_id (int)
            - event_type (str): e.g. 'Transfer', 'Swap'
            - status (str): lifecycle status
        And optionally:
            - decoded_fields (dict): arbitrary decoded event data
            - trigger_id (str): the trigger that matched this event
            - source (str): 'onchain' | 'facilitator' | 'manual'
            - tx_hash, block_number, block_hash, log_index (onchain data)
            - Any payment columns (from_address, to_address, amount, etc.)

        Auto-generates an ``id`` if not provided.  Sets ``product_source``
        to ``'pulse'`` unless explicitly overridden.

        Returns the generated/provided event ID.
        """
        if "id" not in event:
            event["id"] = nanoid(size=21)

        # Default product_source to 'pulse' for generic events
        event.setdefault("product_source", "pulse")
        # Default source to 'onchain'
        event.setdefault("source", "onchain")
        # Default decoded_fields to empty object
        event.setdefault("decoded_fields", {})

        result = self._sb.table("events").insert(event).execute()
        event_id = event["id"]
        logger.info(
            "generic_event_inserted",
            event_id=event_id,
            event_type=event.get("event_type"),
            trigger_id=event.get("trigger_id"),
            product_source=event.get("product_source"),
        )
        return event_id

    # ── Reads ────────────────────────────────────────────────────

    def get_by_id(self, event_id: str) -> dict | None:
        """Fetch a single event by ID."""
        result = self._sb.table("events").select("*").eq("id", event_id).execute()
        return result.data[0] if result.data else None

    def list_by_event_type(
        self,
        event_type: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        product_source: str | None = None,
    ) -> list[dict]:
        """List events filtered by ``event_type``.

        Supports keyset pagination via ``cursor`` (an event ID whose
        ``created_at`` marks the page boundary).  Optionally filter by
        ``product_source`` ('keeper' or 'pulse').

        Returns up to ``limit`` rows ordered by ``created_at`` descending.
        """
        query = (
            self._sb.table("events")
            .select("*")
            .eq("event_type", event_type)
            .order("created_at", desc=True)
            .limit(limit)
        )

        if cursor:
            cursor_row = (
                self._sb.table("events")
                .select("created_at")
                .eq("id", cursor)
                .execute()
            )
            if cursor_row.data:
                query = query.lt("created_at", cursor_row.data[0]["created_at"])

        if product_source:
            query = query.eq("product_source", product_source)

        result = query.execute()
        return result.data

    def list_by_trigger_id(
        self,
        trigger_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        status: str | None = None,
    ) -> list[dict]:
        """List events matched by a specific trigger.

        Returns events whose ``trigger_id`` matches, ordered by
        ``created_at`` descending.  Supports keyset pagination via
        ``cursor`` and optional ``status`` filter.
        """
        query = (
            self._sb.table("events")
            .select("*")
            .eq("trigger_id", trigger_id)
            .order("created_at", desc=True)
            .limit(limit)
        )

        if cursor:
            cursor_row = (
                self._sb.table("events")
                .select("created_at")
                .eq("id", cursor)
                .execute()
            )
            if cursor_row.data:
                query = query.lt("created_at", cursor_row.data[0]["created_at"])

        if status:
            query = query.eq("status", status)

        result = query.execute()
        return result.data

    def list_by_product_source(
        self,
        product_source: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List events for a specific product surface ('keeper' or 'pulse').

        Useful for operational dashboards and product-level event feeds.
        """
        query = (
            self._sb.table("events")
            .select("*")
            .eq("product_source", product_source)
            .order("created_at", desc=True)
            .limit(limit)
        )

        if cursor:
            cursor_row = (
                self._sb.table("events")
                .select("created_at")
                .eq("id", cursor)
                .execute()
            )
            if cursor_row.data:
                query = query.lt("created_at", cursor_row.data[0]["created_at"])

        result = query.execute()
        return result.data

    # ── Updates (unchanged) ──────────────────────────────────────

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

