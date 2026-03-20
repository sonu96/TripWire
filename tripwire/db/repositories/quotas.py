"""Resource quota enforcement.

NOTE: Quota checks are best-effort under high concurrency. The SELECT COUNT
and subsequent INSERT happen in separate transactions, so concurrent requests
can both pass the pre-check before either commits.  For a pragmatic defense,
callers should re-check the quota after each insert (check-create-verify
pattern) to catch races at the cost of one extra query.  A fully atomic
solution would require Postgres advisory locks or serializable transactions.
"""

import structlog
from fastapi import HTTPException

from tripwire.config.settings import settings

logger = structlog.get_logger(__name__)


async def check_trigger_quota(supabase, wallet_address: str) -> None:
    """Raise 429 if wallet has reached trigger limit.

    This is a pre-flight check.  Under concurrent requests the count may be
    stale by the time the caller inserts a new row.  Callers creating
    multiple triggers in a loop should invoke this function before *each*
    insert to tighten the window (see ``register_middleware`` in tools.py).
    """
    result = supabase.table("triggers").select(
        "id", count="exact"
    ).eq("owner_address", wallet_address.lower()).eq("active", True).execute()

    count = getattr(result, "count", None)
    if count is None:
        count = len(result.data) if result.data else 0
    if count >= settings.max_triggers_per_wallet:
        logger.warning(
            "trigger_quota_exceeded",
            wallet=wallet_address,
            count=count,
            limit=settings.max_triggers_per_wallet,
        )
        raise HTTPException(
            status_code=429,
            detail=f"Trigger quota exceeded: {count}/{settings.max_triggers_per_wallet} active triggers",
        )


async def check_endpoint_quota(supabase, wallet_address: str) -> None:
    """Raise 429 if wallet has reached endpoint limit.

    Same best-effort caveat as ``check_trigger_quota`` applies here.
    """
    result = supabase.table("endpoints").select(
        "id", count="exact"
    ).eq("owner_address", wallet_address.lower()).eq("active", True).execute()

    count = getattr(result, "count", None)
    if count is None:
        count = len(result.data) if result.data else 0
    if count >= settings.max_endpoints_per_wallet:
        logger.warning(
            "endpoint_quota_exceeded",
            wallet=wallet_address,
            count=count,
            limit=settings.max_endpoints_per_wallet,
        )
        raise HTTPException(
            status_code=429,
            detail=f"Endpoint quota exceeded: {count}/{settings.max_endpoints_per_wallet} active endpoints",
        )
