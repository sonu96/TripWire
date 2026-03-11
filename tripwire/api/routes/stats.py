"""Application stats endpoint — processing counts and DB metrics."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import structlog
from fastapi import APIRouter, Depends, Request

from tripwire.api import get_supabase
from tripwire.api.auth import require_api_key
from tripwire.api.ratelimit import CRUD_LIMIT, limiter

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["stats"], dependencies=[Depends(require_api_key)])


@router.get("/stats")
@limiter.limit(CRUD_LIMIT)
async def get_stats(request: Request, sb=Depends(get_supabase)):
    """Return application-level processing statistics from the database."""

    # Total events count
    total_result = sb.table("events").select("id", count="exact").execute()
    total_events = total_result.count if hasattr(total_result, "count") and total_result.count is not None else len(total_result.data)

    # Events in last hour
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    recent_result = (
        sb.table("events")
        .select("id", count="exact")
        .gte("created_at", one_hour_ago)
        .execute()
    )
    events_last_hour = recent_result.count if hasattr(recent_result, "count") and recent_result.count is not None else len(recent_result.data)

    # Active endpoints count
    endpoints_result = (
        sb.table("endpoints")
        .select("id", count="exact")
        .eq("active", True)
        .execute()
    )
    active_endpoints = endpoints_result.count if hasattr(endpoints_result, "count") and endpoints_result.count is not None else len(endpoints_result.data)

    # Last event timestamp
    last_event_result = (
        sb.table("events")
        .select("created_at")
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
