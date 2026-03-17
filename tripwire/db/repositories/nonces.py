"""Nonce deduplication repository."""

import structlog
from supabase import Client

logger = structlog.get_logger(__name__)


class NonceRepository:
    """Nonce tracking for ERC-3009 transfer deduplication.

    Uses the ``record_nonce_with_reorg`` Postgres function for reorg-aware
    deduplication.  Reorged nonces can be reclaimed when the same
    authorization is re-broadcast.
    """

    def __init__(self, client: Client) -> None:
        self._sb = client

    def record_nonce(
        self,
        chain_id: int,
        nonce: str,
        authorizer: str,
        event_id: str | None = None,
    ) -> bool:
        """Record a nonce. Returns True if this was a new (non-duplicate) nonce.

        Delegates to the ``record_nonce_with_reorg`` Postgres function which
        handles both fresh inserts and reclamation of reorged nonces.
        """
        result = self._sb.rpc(
            "record_nonce_with_reorg",
            {
                "p_chain_id": chain_id,
                "p_nonce": nonce,
                "p_authorizer": authorizer.lower(),
                "p_event_id": event_id,
            },
        ).execute()

        is_new = bool(result.data)
        if is_new:
            logger.info("nonce_recorded", chain_id=chain_id, nonce=nonce)
        else:
            logger.debug("nonce_duplicate", chain_id=chain_id, nonce=nonce)
        return is_new

    def record_nonce_or_correlate(
        self,
        chain_id: int,
        nonce: str,
        authorizer: str,
        event_id: str | None = None,
        source: str = "goldsky",
    ) -> tuple[bool, str | None, str | None]:
        """Record a nonce with correlation support.

        Calls the ``record_nonce_or_correlate`` Postgres function which:
        - Inserts if new → returns (True, None, None)
        - Reclaims if reorged → returns (True, None, None)
        - Returns correlation info if active duplicate →
          (False, existing_event_id, existing_source)

        This enables the Goldsky path to detect that a facilitator already
        claimed the nonce and promote the pre_confirmed event rather than
        silently dropping the real onchain event.
        """
        result = self._sb.rpc(
            "record_nonce_or_correlate",
            {
                "p_chain_id": chain_id,
                "p_nonce": nonce,
                "p_authorizer": authorizer.lower(),
                "p_event_id": event_id,
                "p_source": source,
            },
        ).execute()

        # The RPC returns a list with one row:
        # [{"is_new": bool, "existing_event_id": str|null, "existing_source": str|null}]
        if result.data and isinstance(result.data, list) and len(result.data) > 0:
            row = result.data[0]
            is_new = bool(row.get("is_new", False))
            existing_event_id = row.get("existing_event_id")
            existing_source = row.get("existing_source")
        else:
            # Fallback: treat empty result as duplicate with no correlation
            is_new = False
            existing_event_id = None
            existing_source = None

        if is_new:
            logger.info(
                "nonce_recorded",
                chain_id=chain_id,
                nonce=nonce,
                source=source,
            )
        else:
            logger.debug(
                "nonce_duplicate_or_correlate",
                chain_id=chain_id,
                nonce=nonce,
                existing_event_id=existing_event_id,
                existing_source=existing_source,
            )
        return is_new, existing_event_id, existing_source

    def invalidate_by_event_id(self, event_id: str) -> bool:
        """Mark a nonce as reorged by its linked event_id.

        Returns True if a nonce was found and marked reorged.
        """
        from datetime import datetime, timezone

        result = (
            self._sb.table("nonces")
            .update({"reorged_at": datetime.now(timezone.utc).isoformat()})
            .eq("event_id", event_id)
            .is_("reorged_at", "null")
            .execute()
        )
        invalidated = bool(result.data)
        if invalidated:
            logger.info("nonce_invalidated_by_event", event_id=event_id)
        return invalidated

    def archive_old(self, age_days: int = 30, batch_size: int = 5000) -> int:
        """Archive old nonces via the ``archive_old_nonces`` DB function."""
        result = self._sb.rpc(
            "archive_old_nonces",
            {
                "age_threshold": f"{age_days} days",
                "batch_size": batch_size,
            },
        ).execute()
        if result.data is not None:
            return int(result.data)
        return 0

