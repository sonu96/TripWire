"""Subscription (Notify mode) routes."""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from nanoid import generate as nanoid

from tripwire.types.models import CreateSubscriptionRequest, Subscription

router = APIRouter(tags=["subscriptions"])


def _supabase(request: Request):
    return request.app.state.supabase


@router.post(
    "/endpoints/{endpoint_id}/subscriptions",
    response_model=Subscription,
    status_code=201,
)
async def create_subscription(
    endpoint_id: str, body: CreateSubscriptionRequest, request: Request
):
    """Create a subscription for an endpoint (Notify mode)."""
    sb = _supabase(request)

    # Verify endpoint exists and is active
    ep = sb.table("endpoints").select("id, mode").eq("id", endpoint_id).eq("active", True).execute()
    if not ep.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    if ep.data[0]["mode"] != "notify":
        raise HTTPException(status_code=400, detail="Subscriptions are only for notify-mode endpoints")

    now = datetime.now(timezone.utc).isoformat()
    sub_id = nanoid(size=21)

    row = {
        "id": sub_id,
        "endpoint_id": endpoint_id,
        "filters": body.filters.model_dump(),
        "active": True,
        "created_at": now,
    }

    result = sb.table("subscriptions").insert(row).execute()
    return Subscription(**result.data[0])


@router.get(
    "/endpoints/{endpoint_id}/subscriptions",
    response_model=list[Subscription],
)
async def list_subscriptions(endpoint_id: str, request: Request):
    """List subscriptions for an endpoint."""
    sb = _supabase(request)

    ep = sb.table("endpoints").select("id").eq("id", endpoint_id).execute()
    if not ep.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    result = (
        sb.table("subscriptions")
        .select("*")
        .eq("endpoint_id", endpoint_id)
        .eq("active", True)
        .execute()
    )
    return result.data


@router.delete("/subscriptions/{subscription_id}", status_code=204)
async def remove_subscription(subscription_id: str, request: Request):
    """Deactivate a subscription."""
    sb = _supabase(request)

    existing = sb.table("subscriptions").select("id").eq("id", subscription_id).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Subscription not found")

    sb.table("subscriptions").update({
        "active": False,
    }).eq("id", subscription_id).execute()
