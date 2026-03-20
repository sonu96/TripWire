"""Webhook delivery status routes."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from tripwire.api import get_supabase
from tripwire.api.auth import require_wallet_auth, WalletAuthContext
from tripwire.api.ratelimit import CRUD_LIMIT, limiter
from tripwire.db.repositories.webhooks import WebhookDeliveryRepository
from tripwire.types.models import execution_state_from_status
from tripwire.webhook.convoy_client import retry_message

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["deliveries"])


# ── Helper: verify endpoint ownership ────────────────────────


def _verify_endpoint_ownership(endpoint_row: dict, wallet_address: str) -> None:
    """Raise 403 if the endpoint does not belong to the authenticated wallet."""
    if endpoint_row.get("owner_address", "").lower() != wallet_address.lower():
        raise HTTPException(status_code=403, detail="Not authorized to access this endpoint")


def _enrich_deliveries(rows: list[dict], sb) -> list[dict]:
    """Enrich delivery rows with execution state from their parent events."""
    event_ids = list({r["event_id"] for r in rows if r.get("event_id")})
    if not event_ids:
        return rows
    try:
        ev_result = sb.table("events").select("id,status").in_("id", event_ids).execute()
        status_map = {e["id"]: e.get("status", "pending") for e in ev_result.data}
    except Exception:
        return rows
    for row in rows:
        status = status_map.get(row.get("event_id"), "pending")
        state, safe, _ = execution_state_from_status(status)
        row["execution_state"] = state.value
        row["safe_to_execute"] = safe
    return rows


def _get_wallet_endpoint_ids(sb, wallet_address: str) -> list[str]:
    """Return all endpoint IDs owned by the given wallet address."""
    result = (
        sb.table("endpoints")
        .select("id")
        .eq("owner_address", wallet_address)
        .execute()
    )
    return [row["id"] for row in result.data]


# ── Response models ──────────────────────────────────────────


class DeliveryResponse(BaseModel):
    id: str
    endpoint_id: str
    event_id: str
    provider_message_id: str | None = None
    status: str
    created_at: str
    execution_state: str | None = None
    safe_to_execute: bool | None = None


class DeliveryListResponse(BaseModel):
    data: list[DeliveryResponse]
    cursor: str | None = None
    has_more: bool = False


class DeliveryStatsResponse(BaseModel):
    endpoint_id: str
    total: int
    pending: int
    sent: int
    delivered: int
    failed: int
    success_rate: float


# ── Routes ───────────────────────────────────────────────────


@router.get("/deliveries", response_model=DeliveryListResponse)
@limiter.limit(CRUD_LIMIT)
async def list_deliveries(
    request: Request,
    endpoint_id: str | None = Query(None, description="Filter by endpoint"),
    event_id: str | None = Query(None, description="Filter by event"),
    status: str | None = Query(None, description="Filter by status"),
    cursor: str | None = Query(None, description="Cursor for pagination (delivery id)"),
    limit: int = Query(50, ge=1, le=200),
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """List deliveries with optional filters and cursor pagination.

    Only returns deliveries belonging to the authenticated wallet's endpoints.
    """

    # If a specific endpoint_id is provided, verify ownership
    if endpoint_id:
        ep = sb.table("endpoints").select("*").eq("id", endpoint_id).execute()
        if not ep.data:
            raise HTTPException(status_code=404, detail="Endpoint not found")
        _verify_endpoint_ownership(ep.data[0], wallet_auth.wallet_address)

    # Scope deliveries to wallet's endpoints
    wallet_endpoint_ids = _get_wallet_endpoint_ids(sb, wallet_auth.wallet_address)
    if not wallet_endpoint_ids:
        return DeliveryListResponse(data=[], cursor=None, has_more=False)

    # Build the query manually to add the in_ filter for ownership
    query = (
        sb.table("webhook_deliveries")
        .select("*")
        .in_("endpoint_id", wallet_endpoint_ids)
        .order("created_at", desc=True)
        .limit(limit + 1)
    )

    if cursor:
        cursor_row = (
            sb.table("webhook_deliveries")
            .select("created_at")
            .eq("id", cursor)
            .execute()
        )
        if cursor_row.data:
            query = query.lt("created_at", cursor_row.data[0]["created_at"])

    if endpoint_id:
        query = query.eq("endpoint_id", endpoint_id)
    if event_id:
        query = query.eq("event_id", event_id)
    if status:
        query = query.eq("status", status)

    rows = query.execute().data

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    rows = _enrich_deliveries(rows, sb)
    next_cursor = rows[-1]["id"] if has_more and rows else None
    return DeliveryListResponse(data=rows, cursor=next_cursor, has_more=has_more)


@router.get("/deliveries/{delivery_id}", response_model=DeliveryResponse)
@limiter.limit(CRUD_LIMIT)
async def get_delivery(
    request: Request,
    delivery_id: str,
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """Get a single delivery by ID (must belong to an endpoint owned by the authenticated wallet)."""
    repo = WebhookDeliveryRepository(sb)
    row = repo.get_by_id(delivery_id)
    if not row:
        raise HTTPException(status_code=404, detail="Delivery not found")

    # Verify ownership through the parent endpoint
    ep = sb.table("endpoints").select("*").eq("id", row["endpoint_id"]).execute()
    if not ep.data:
        raise HTTPException(status_code=404, detail="Parent endpoint not found")
    _verify_endpoint_ownership(ep.data[0], wallet_auth.wallet_address)

    enriched = _enrich_deliveries([row], sb)
    return DeliveryResponse(**enriched[0])


@router.get("/endpoints/{endpoint_id}/deliveries", response_model=DeliveryListResponse)
@limiter.limit(CRUD_LIMIT)
async def list_endpoint_deliveries(
    request: Request,
    endpoint_id: str,
    status: str | None = Query(None, description="Filter by status"),
    cursor: str | None = Query(None, description="Cursor for pagination (delivery id)"),
    limit: int = Query(50, ge=1, le=200),
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """List deliveries for a specific endpoint (must belong to authenticated wallet)."""
    # Verify endpoint exists and ownership
    ep = sb.table("endpoints").select("*").eq("id", endpoint_id).execute()
    if not ep.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    _verify_endpoint_ownership(ep.data[0], wallet_auth.wallet_address)

    repo = WebhookDeliveryRepository(sb)
    rows = repo.list_paginated(
        endpoint_id=endpoint_id,
        status=status,
        cursor=cursor,
        limit=limit,
    )

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    rows = _enrich_deliveries(rows, sb)
    next_cursor = rows[-1]["id"] if has_more and rows else None
    return DeliveryListResponse(data=rows, cursor=next_cursor, has_more=has_more)


@router.get("/endpoints/{endpoint_id}/deliveries/stats", response_model=DeliveryStatsResponse)
@limiter.limit(CRUD_LIMIT)
async def get_delivery_stats(
    request: Request,
    endpoint_id: str,
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """Get delivery stats (counts by status, success rate) for an endpoint."""
    # Verify endpoint exists and ownership
    ep = sb.table("endpoints").select("*").eq("id", endpoint_id).execute()
    if not ep.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    _verify_endpoint_ownership(ep.data[0], wallet_auth.wallet_address)

    repo = WebhookDeliveryRepository(sb)
    stats = repo.get_stats_for_endpoint(endpoint_id)
    return DeliveryStatsResponse(**stats)


@router.post("/deliveries/{delivery_id}/retry", status_code=202)
@limiter.limit(CRUD_LIMIT)
async def retry_delivery(
    request: Request,
    delivery_id: str,
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """Retry a failed delivery via Convoy (must belong to an endpoint owned by the authenticated wallet)."""
    repo = WebhookDeliveryRepository(sb)
    delivery = repo.get_by_id(delivery_id)
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")

    # Verify ownership through the parent endpoint
    ep = sb.table("endpoints").select("*").eq("id", delivery["endpoint_id"]).execute()
    if not ep.data:
        raise HTTPException(status_code=404, detail="Parent endpoint not found")
    _verify_endpoint_ownership(ep.data[0], wallet_auth.wallet_address)

    if delivery["status"] not in ("failed",):
        raise HTTPException(status_code=400, detail="Only failed deliveries can be retried")

    # Look up the endpoint to get the convoy_project_id
    if not ep.data[0].get("convoy_project_id"):
        raise HTTPException(status_code=400, detail="Endpoint has no Convoy project configured")

    convoy_project_id = ep.data[0]["convoy_project_id"]

    # NOTE: provider_message_id stores the Convoy *event* ID, not the
    # event-delivery ID.  retry_message() handles the lookup internally
    # by querying Convoy for the event deliveries of this event.
    provider_message_id = delivery.get("provider_message_id")
    if not provider_message_id:
        raise HTTPException(status_code=400, detail="Delivery has no provider message ID for retry")

    try:
        await retry_message(app_id=convoy_project_id, delivery_id=provider_message_id)
    except Exception:
        logger.exception("delivery_retry_failed", delivery_id=delivery_id)
        raise HTTPException(status_code=502, detail="Failed to retry delivery via Convoy")

    # Update status to pending since it's being retried
    repo.update_status(delivery_id, "pending")

    logger.info("delivery_retry_requested", delivery_id=delivery_id, convoy_project_id=convoy_project_id)
    return {"detail": "Retry requested", "delivery_id": delivery_id}
