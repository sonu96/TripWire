"""Webhook delivery status routes."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from tripwire.api import get_supabase
from tripwire.api.auth import require_wallet_auth
from tripwire.api.ratelimit import CRUD_LIMIT, limiter
from tripwire.db.repositories.webhooks import WebhookDeliveryRepository
from tripwire.webhook.convoy_client import retry_message

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["deliveries"], dependencies=[Depends(require_wallet_auth)])


# ── Response models ──────────────────────────────────────────


class DeliveryResponse(BaseModel):
    id: str
    endpoint_id: str
    event_id: str
    provider_message_id: str | None = None
    status: str
    created_at: str


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
    sb=Depends(get_supabase),
):
    """List deliveries with optional filters and cursor pagination."""
    repo = WebhookDeliveryRepository(sb)
    rows = repo.list_paginated(
        endpoint_id=endpoint_id,
        event_id=event_id,
        status=status,
        cursor=cursor,
        limit=limit,
    )

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    next_cursor = rows[-1]["id"] if has_more and rows else None
    return DeliveryListResponse(data=rows, cursor=next_cursor, has_more=has_more)


@router.get("/deliveries/{delivery_id}", response_model=DeliveryResponse)
@limiter.limit(CRUD_LIMIT)
async def get_delivery(request: Request, delivery_id: str, sb=Depends(get_supabase)):
    """Get a single delivery by ID."""
    repo = WebhookDeliveryRepository(sb)
    row = repo.get_by_id(delivery_id)
    if not row:
        raise HTTPException(status_code=404, detail="Delivery not found")
    return DeliveryResponse(**row)


@router.get("/endpoints/{endpoint_id}/deliveries", response_model=DeliveryListResponse)
@limiter.limit(CRUD_LIMIT)
async def list_endpoint_deliveries(
    request: Request,
    endpoint_id: str,
    status: str | None = Query(None, description="Filter by status"),
    cursor: str | None = Query(None, description="Cursor for pagination (delivery id)"),
    limit: int = Query(50, ge=1, le=200),
    sb=Depends(get_supabase),
):
    """List deliveries for a specific endpoint."""
    # Verify endpoint exists
    ep = sb.table("endpoints").select("id").eq("id", endpoint_id).execute()
    if not ep.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")

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

    next_cursor = rows[-1]["id"] if has_more and rows else None
    return DeliveryListResponse(data=rows, cursor=next_cursor, has_more=has_more)


@router.get("/endpoints/{endpoint_id}/deliveries/stats", response_model=DeliveryStatsResponse)
@limiter.limit(CRUD_LIMIT)
async def get_delivery_stats(
    request: Request,
    endpoint_id: str,
    sb=Depends(get_supabase),
):
    """Get delivery stats (counts by status, success rate) for an endpoint."""
    # Verify endpoint exists
    ep = sb.table("endpoints").select("id").eq("id", endpoint_id).execute()
    if not ep.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    repo = WebhookDeliveryRepository(sb)
    stats = repo.get_stats_for_endpoint(endpoint_id)
    return DeliveryStatsResponse(**stats)


@router.post("/deliveries/{delivery_id}/retry", status_code=202)
@limiter.limit(CRUD_LIMIT)
async def retry_delivery(request: Request, delivery_id: str, sb=Depends(get_supabase)):
    """Retry a failed delivery via Convoy."""
    repo = WebhookDeliveryRepository(sb)
    delivery = repo.get_by_id(delivery_id)
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")

    if delivery["status"] not in ("failed",):
        raise HTTPException(status_code=400, detail="Only failed deliveries can be retried")

    # Look up the endpoint to get the convoy_project_id
    endpoint = sb.table("endpoints").select("convoy_project_id").eq("id", delivery["endpoint_id"]).execute()
    if not endpoint.data or not endpoint.data[0].get("convoy_project_id"):
        raise HTTPException(status_code=400, detail="Endpoint has no Convoy project configured")

    convoy_project_id = endpoint.data[0]["convoy_project_id"]

    # Use the provider_message_id as the Convoy event delivery ID for retry
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
