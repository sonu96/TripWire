"""Nonce deduplication repository."""

import structlog
from supabase import Client

logger = structlog.get_logger()


class NonceRepository:
    """Nonce tracking for ERC-3009 transfer deduplication.

    Uses upsert with on_conflict on the (chain_id, nonce, authorizer) unique
    constraint to ensure idempotent inserts.
    """

    def __init__(self, client: Client) -> None:
        self._sb = client

    def record_nonce(self, chain_id: int, nonce: str, authorizer: str) -> bool:
        """Record a nonce. Returns True if this was a new (non-duplicate) nonce.

        Uses upsert with on_conflict so duplicate nonces are silently ignored.
        The caller can compare the returned flag to decide whether to process
        the associated transfer.
        """
        # First check if it already exists
        existing = (
            self._sb.table("nonces")
            .select("nonce")
            .eq("chain_id", chain_id)
            .eq("nonce", nonce)
            .eq("authorizer", authorizer.lower())
            .execute()
        )
        if existing.data:
            logger.debug("nonce_duplicate", chain_id=chain_id, nonce=nonce)
            return False

        # Upsert to handle race conditions gracefully
        self._sb.table("nonces").upsert(
            {
                "chain_id": chain_id,
                "nonce": nonce,
                "authorizer": authorizer.lower(),
            },
            on_conflict="chain_id,nonce,authorizer",
        ).execute()

        logger.info("nonce_recorded", chain_id=chain_id, nonce=nonce)
        return True

    def exists(self, chain_id: int, nonce: str, authorizer: str) -> bool:
        """Check whether a nonce has already been recorded."""
        result = (
            self._sb.table("nonces")
            .select("nonce")
            .eq("chain_id", chain_id)
            .eq("nonce", nonce)
            .eq("authorizer", authorizer.lower())
            .execute()
        )
        return bool(result.data)
