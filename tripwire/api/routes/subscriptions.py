"""Subscription (Notify mode) routes."""

from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from nanoid import generate as nanoid

from postgrest.exceptions import APIError as PostgrestAPIError

from tripwire.api import get_supabase
from tripwire.api.auth import require_wallet_auth, WalletAuthContext
from tripwire.api.ratelimit import CRUD_LIMIT, limiter
from tripwire.observability.audit import fire_and_forget
from tripwire.types.models import CreateSubscriptionRequest, Subscription

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["subscriptions"])


# ── Helper: verify endpoint ownership ────────────────────────


def _verify_endpoint_ownership(endpoint_row: dict, wallet_address: str) -> None:
    """Raise 403 if the endpoint does not belong to the authenticated wallet."""
    if endpoint_row.get("owner_address", "").lower() != wallet_address.lower():
        raise HTTPException(status_code=403, detail="Not authorized to access this endpoint")


@router.post(
    "/endpoints/{endpoint_id}/subscriptions",
    response_model=Subscription,
    status_code=201,
)
@limiter.limit(CRUD_LIMIT)
async def create_subscription(
    request: Request,
    endpoint_id: str,
    body: CreateSubscriptionRequest,
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """Create a subscription for an endpoint (Notify mode)."""

    # Verify endpoint exists, is active, and belongs to the authenticated wallet
    ep = sb.table("endpoints").select("*").eq("id", endpoint_id).eq("active", True).execute()
    if not ep.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    _verify_endpoint_ownership(ep.data[0], wallet_auth.wallet_address)

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

    try:
        result = sb.table("subscriptions").insert(row).execute()
    except PostgrestAPIError as exc:
        if exc.code == "23505":
            raise HTTPException(status_code=409, detail="Subscription already exists")
        raise
    logger.info("subscription_created", subscription_id=sub_id, endpoint_id=endpoint_id)
    fire_and_forget(request.app.state.audit_logger.log(
        action="subscription.created",
        actor=wallet_auth.wallet_address,
        resource_type="subscription",
        resource_id=sub_id,
        details={"endpoint_id": endpoint_id},
        ip_address=request.client.host if request.client else None,
    ))
    return Subscription(**result.data[0])


@router.get(
    "/endpoints/{endpoint_id}/subscriptions",
    response_model=list[Subscription],
)
@limiter.limit(CRUD_LIMIT)
async def list_subscriptions(
    request: Request,
    endpoint_id: str,
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """List subscriptions for an endpoint."""

    ep = sb.table("endpoints").select("*").eq("id", endpoint_id).execute()
    if not ep.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    _verify_endpoint_ownership(ep.data[0], wallet_auth.wallet_address)

    result = (
        sb.table("subscriptions")
        .select("*")
        .eq("endpoint_id", endpoint_id)
        .eq("active", True)
        .execute()
    )
    return result.data


@router.delete("/subscriptions/{subscription_id}", status_code=204)
@limiter.limit(CRUD_LIMIT)
async def remove_subscription(
    request: Request,
    subscription_id: str,
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """Deactivate a subscription."""

    existing = sb.table("subscriptions").select("*").eq("id", subscription_id).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Subscription not found")

    # Verify ownership through the parent endpoint
    ep = sb.table("endpoints").select("*").eq("id", existing.data[0]["endpoint_id"]).execute()
    if not ep.data:
        raise HTTPException(status_code=404, detail="Parent endpoint not found")
    _verify_endpoint_ownership(ep.data[0], wallet_auth.wallet_address)

    sb.table("subscriptions").update({
        "active": False,
    }).eq("id", subscription_id).execute()
    logger.info("subscription_deactivated", subscription_id=subscription_id)
    fire_and_forget(request.app.state.audit_logger.log(
        action="subscription.deleted",
        actor=wallet_auth.wallet_address,
        resource_type="subscription",
        resource_id=subscription_id,
        details={"endpoint_id": existing.data[0]["endpoint_id"]},
        ip_address=request.client.host if request.client else None,
    ))
