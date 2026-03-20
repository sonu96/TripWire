"""Endpoint registration routes."""

import secrets
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from nanoid import generate as nanoid
from pydantic import BaseModel, field_validator

from postgrest.exceptions import APIError as PostgrestAPIError

from tripwire.api import get_supabase
from tripwire.api.auth import require_wallet_auth, WalletAuthContext
from tripwire.api.ratelimit import CRUD_LIMIT, limiter
from tripwire.types.models import (
    Endpoint,
    EndpointMode,
    EndpointPolicies,
    RegisterEndpointRequest,
)
from tripwire.observability.audit import fire_and_forget
from tripwire.webhook.provider import WebhookProvider
from tripwire.db.repositories.quotas import check_endpoint_quota
from tripwire.cache import RedisCache
from tripwire.api.redis import get_redis

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/endpoints", tags=["endpoints"])


class EndpointListResponse(BaseModel):
    data: list[Endpoint]
    count: int


class UpdateEndpointRequest(BaseModel):
    url: str | None = None
    mode: EndpointMode | None = None
    chains: list[int] | None = None
    policies: EndpointPolicies | None = None
    active: bool | None = None

    @field_validator("url")
    @classmethod
    def url_must_be_safe(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from tripwire.api.validation import validate_endpoint_url

        return validate_endpoint_url(v)


# ── Helper: verify endpoint ownership ────────────────────────


def _verify_ownership(endpoint_row: dict, wallet_address: str) -> None:
    """Raise 403 if the endpoint does not belong to the authenticated wallet."""
    if endpoint_row.get("owner_address", "").lower() != wallet_address.lower():
        raise HTTPException(status_code=403, detail="Not authorized to access this endpoint")


# ── Routes ────────────────────────────────────────────────────


@router.post("", response_model=Endpoint, status_code=201)
@limiter.limit(CRUD_LIMIT)
async def register_endpoint(
    request: Request,
    body: RegisterEndpointRequest,
    wallet: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """Register a new webhook endpoint.

    The caller's verified wallet address becomes the owner_address.
    Also creates a corresponding Convoy application and endpoint so that
    webhook delivery is ready as soon as the endpoint is registered.
    """
    # Quota check before creating endpoint
    await check_endpoint_quota(sb, wallet.wallet_address)

    now = datetime.now(timezone.utc).isoformat()
    endpoint_id = nanoid(size=21)

    row = {
        "id": endpoint_id,
        "url": body.url,
        "mode": body.mode.value,
        "chains": body.chains,
        "recipient": body.recipient,
        "owner_address": wallet.wallet_address,
        "policies": (body.policies or EndpointPolicies()).model_dump(),
        "active": True,
        "created_at": now,
        "updated_at": now,
    }

    try:
        result = sb.table("endpoints").insert(row).execute()
    except PostgrestAPIError as exc:
        if exc.code == "23505":
            raise HTTPException(status_code=409, detail="Endpoint already exists")
        raise
    endpoint = Endpoint(**result.data[0])

    # Persist x402 payment details if the middleware verified a payment
    payment_tx_hash = getattr(request.state, "payment_tx_hash", None)
    payment_chain_id = getattr(request.state, "payment_chain_id", None)
    if payment_tx_hash:
        sb.table("endpoints").update({
            "registration_tx_hash": payment_tx_hash,
            "registration_chain_id": payment_chain_id,
        }).eq("id", endpoint_id).execute()
        endpoint.registration_tx_hash = payment_tx_hash
        endpoint.registration_chain_id = payment_chain_id
        logger.info(
            "x402_payment_recorded",
            endpoint_id=endpoint_id,
            tx_hash=payment_tx_hash,
            chain_id=payment_chain_id,
        )

    # Create webhook provider application + endpoint for delivery
    if body.mode == EndpointMode.EXECUTE:
        provider: WebhookProvider = request.app.state.webhook_provider
        try:
            convoy_project_id = await provider.create_app(
                developer_id=endpoint_id,
                name=f"tripwire-{endpoint_id}",
            )
            webhook_secret = secrets.token_hex(32)
            convoy_endpoint_id = await provider.create_endpoint(
                app_id=convoy_project_id,
                url=body.url,
                description=f"TripWire endpoint for {body.recipient}",
                secret=webhook_secret,
            )
            # Persist the provider IDs back to the endpoint row
            # webhook_secret is NOT stored in DB (Convoy is sole HMAC signer)
            sb.table("endpoints").update({
                "convoy_project_id": convoy_project_id,
                "convoy_endpoint_id": convoy_endpoint_id,
            }).eq("id", endpoint_id).execute()
            endpoint.convoy_project_id = convoy_project_id
            endpoint.convoy_endpoint_id = convoy_endpoint_id
            # Return the webhook secret once — caller must store it securely
            endpoint.webhook_secret = webhook_secret
            logger.info(
                "webhook_provider_wired",
                endpoint_id=endpoint_id,
                convoy_project_id=convoy_project_id,
                convoy_endpoint_id=convoy_endpoint_id,
            )
        except Exception:
            logger.exception("webhook_provider_setup_failed", endpoint_id=endpoint_id)
            # Convoy setup failed — delete the endpoint row so we don't leave
            # a broken endpoint that silently drops webhooks.
            try:
                sb.table("endpoints").delete().eq("id", endpoint_id).execute()
            except Exception:
                logger.exception("endpoint_cleanup_failed", endpoint_id=endpoint_id)
            raise HTTPException(
                status_code=502,
                detail="Webhook provider setup failed. Endpoint was not created.",
            )

    # Invalidate endpoint cache
    try:
        cache = RedisCache(get_redis(), prefix="tripwire:cache", default_ttl=30)
        await cache.invalidate_pattern("endpoints:*")
    except Exception:
        logger.debug("endpoint_cache_invalidation_failed")

    fire_and_forget(request.app.state.audit_logger.log(
        action="endpoint.created",
        actor=wallet.wallet_address,
        resource_type="endpoint",
        resource_id=endpoint_id,
        details={"url": body.url, "mode": body.mode.value},
        ip_address=request.client.host if request.client else None,
    ))
    return endpoint


@router.get("", response_model=EndpointListResponse)
@limiter.limit(CRUD_LIMIT)
async def list_endpoints(
    request: Request,
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """List all registered endpoints belonging to the authenticated wallet."""
    result = (
        sb.table("endpoints")
        .select("*")
        .eq("active", True)
        .eq("owner_address", wallet_auth.wallet_address)
        .execute()
    )
    return EndpointListResponse(data=result.data, count=len(result.data))


@router.get("/{endpoint_id}", response_model=Endpoint)
@limiter.limit(CRUD_LIMIT)
async def get_endpoint(
    request: Request,
    endpoint_id: str,
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """Get endpoint details (must belong to authenticated wallet)."""
    result = sb.table("endpoints").select("*").eq("id", endpoint_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    _verify_ownership(result.data[0], wallet_auth.wallet_address)
    return Endpoint(**result.data[0])


@router.patch("/{endpoint_id}", response_model=Endpoint)
@limiter.limit(CRUD_LIMIT)
async def update_endpoint(
    request: Request,
    endpoint_id: str,
    body: UpdateEndpointRequest,
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """Update an endpoint (must belong to authenticated wallet)."""

    # Verify exists and ownership
    existing = sb.table("endpoints").select("*").eq("id", endpoint_id).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    _verify_ownership(existing.data[0], wallet_auth.wallet_address)

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Serialize enum for Supabase storage (model_dump already converts
    # nested models like policies to dicts, but leaves enums as Enum objects)
    if "mode" in updates:
        updates["mode"] = updates["mode"].value

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()

    result = sb.table("endpoints").update(updates).eq("id", endpoint_id).execute()

    # Invalidate endpoint cache
    try:
        cache = RedisCache(get_redis(), prefix="tripwire:cache", default_ttl=30)
        await cache.invalidate_pattern("endpoints:*")
    except Exception:
        logger.debug("endpoint_cache_invalidation_failed")

    fire_and_forget(request.app.state.audit_logger.log(
        action="endpoint.updated",
        actor=wallet_auth.wallet_address,
        resource_type="endpoint",
        resource_id=endpoint_id,
        details={"fields": list(updates.keys())},
        ip_address=request.client.host if request.client else None,
    ))
    return Endpoint(**result.data[0])


@router.delete("/{endpoint_id}", status_code=204)
@limiter.limit(CRUD_LIMIT)
async def deactivate_endpoint(
    request: Request,
    endpoint_id: str,
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """Deactivate (soft-delete) an endpoint (must belong to authenticated wallet)."""

    existing = sb.table("endpoints").select("*").eq("id", endpoint_id).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    _verify_ownership(existing.data[0], wallet_auth.wallet_address)

    sb.table("endpoints").update({
        "active": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", endpoint_id).execute()

    # Invalidate endpoint cache
    try:
        cache = RedisCache(get_redis(), prefix="tripwire:cache", default_ttl=30)
        await cache.invalidate_pattern("endpoints:*")
    except Exception:
        logger.debug("endpoint_cache_invalidation_failed")

    fire_and_forget(request.app.state.audit_logger.log(
        action="endpoint.deleted",
        actor=wallet_auth.wallet_address,
        resource_type="endpoint",
        resource_id=endpoint_id,
        ip_address=request.client.host if request.client else None,
    ))
