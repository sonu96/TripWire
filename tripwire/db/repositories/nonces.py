"""Nonce deduplication repository."""

import structlog
from supabase import Client

logger = structlog.get_logger(__name__)


class NonceRepository:
    """Nonce tracking for ERC-3009 transfer deduplication.

    Uses upsert with on_conflict on the (chain_id, nonce, authorizer) unique
    constraint to ensure idempotent inserts.
    """

    def __init__(self, client: Client) -> None:
        self._sb = client

    def record_nonce(self, chain_id: int, nonce: str, authorizer: str) -> bool:
        """Record a nonce. Returns True if this was a new (non-duplicate) nonce.

        Uses a single upsert with ignore_duplicates=True so duplicate nonces
        are silently ignored at the DB level — no extra SELECT round-trip and
        no race condition between check-then-insert.
        """
        result = (
            self._sb.table("nonces")
            .upsert(
                {
                    "chain_id": chain_id,
                    "nonce": nonce,
                    "authorizer": authorizer.lower(),
                },
                on_conflict="chain_id,nonce,authorizer",
                ignore_duplicates=True,
            )
            .execute()
        )

        # When ignore_duplicates=True, a duplicate row returns empty data
        is_new = bool(result.data)
        if is_new:
            logger.info("nonce_recorded", chain_id=chain_id, nonce=nonce)
        else:
            logger.debug("nonce_duplicate", chain_id=chain_id, nonce=nonce)
        return is_new

