"""Application stats endpoint — processing counts and DB metrics."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import structlog
from fastapi import APIRouter, Depends, Request

from tripwire.api import get_supabase
from tripwire.api.auth import require_wallet_auth, WalletAuthContext
from tripwire.api.ratelimit import CRUD_LIMIT, limiter

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["stats"])


@router.get("/stats")
@limiter.limit(CRUD_LIMIT)
async def get_stats(
    request: Request,
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """Return processing statistics scoped to the authenticated wallet's endpoints."""

    # Get all endpoint IDs owned by this wallet
    wallet_endpoints = (
        sb.table("endpoints")
        .select("id")
        .eq("owner_address", wallet_auth.wallet_address)
        .execute()
    )
    endpoint_ids = [row["id"] for row in wallet_endpoints.data]

    if not endpoint_ids:
        return {
            "total_events": 0,
            "events_last_hour": 0,
            "active_endpoints": 0,
            "last_event_at": None,
        }

    # Active endpoints count (owned by this wallet)
    active_endpoints_result = (
        sb.table("endpoints")
        .select("id", count="exact")
        .eq("active", True)
        .eq("owner_address", wallet_auth.wallet_address)
        .execute()
    )
    active_endpoints = (
        active_endpoints_result.count
        if hasattr(active_endpoints_result, "count") and active_endpoints_result.count is not None
        else len(active_endpoints_result.data)
    )

    # Total events count (only for wallet's endpoints)
    total_result = (
        sb.table("events")
        .select("id", count="exact")
        .in_("endpoint_id", endpoint_ids)
        .execute()
    )
    total_events = (
        total_result.count
        if hasattr(total_result, "count") and total_result.count is not None
        else len(total_result.data)
    )

    # Events in last hour (only for wallet's endpoints)
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    recent_result = (
        sb.table("events")
        .select("id", count="exact")
        .in_("endpoint_id", endpoint_ids)
        .gte("created_at", one_hour_ago)
        .execute()
    )
    events_last_hour = (
        recent_result.count
        if hasattr(recent_result, "count") and recent_result.count is not None
        else len(recent_result.data)
    )

    # Last event timestamp (only for wallet's endpoints)
    last_event_result = (
        sb.table("events")
        .select("created_at")
        .in_("endpoint_id", endpoint_ids)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    last_event_at = (
        last_event_result.data[0]["created_at"]
        if last_event_result.data
        else None
    )

    return {
        "total_events": total_events,
        "events_last_hour": events_last_hour,
        "active_endpoints": active_endpoints,
        "last_event_at": last_event_at,
    }
