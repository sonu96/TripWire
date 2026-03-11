"""Endpoint registration routes."""

import secrets
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from nanoid import generate as nanoid
from pydantic import BaseModel

from tripwire.api import get_supabase
from tripwire.api.auth import generate_api_key, hash_api_key, require_api_key
from tripwire.api.ratelimit import CRUD_LIMIT, limiter
from tripwire.types.models import (
    Endpoint,
    EndpointMode,
    EndpointPolicies,
    RegisterEndpointRequest,
)
from tripwire.webhook.provider import WebhookProvider

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/endpoints", tags=["endpoints"], dependencies=[Depends(require_api_key)])


class EndpointListResponse(BaseModel):
    data: list[Endpoint]
    count: int


class RotateKeyResponse(BaseModel):
    endpoint_id: str
    api_key: str  # New plaintext key (shown once)
    rotated_at: str


class UpdateEndpointRequest(BaseModel):
    url: str | None = None
    mode: EndpointMode | None = None
    chains: list[int] | None = None
    policies: EndpointPolicies | None = None
    active: bool | None = None


@router.post("", response_model=Endpoint, status_code=201)
@limiter.limit(CRUD_LIMIT)
async def register_endpoint(request: Request, body: RegisterEndpointRequest, sb=Depends(get_supabase)):
    """Register a new webhook endpoint.

    Also creates a corresponding Convoy application and endpoint so that
    webhook delivery is ready as soon as the endpoint is registered.
    """
    now = datetime.now(timezone.utc).isoformat()
    endpoint_id = nanoid(size=21)

    # Generate API key — store hash, return plaintext once
    api_key = generate_api_key()
    api_key_hash_value = hash_api_key(api_key)

    row = {
        "id": endpoint_id,
        "url": body.url,
        "mode": body.mode.value,
        "chains": body.chains,
        "recipient": body.recipient,
        "policies": (body.policies or EndpointPolicies()).model_dump(),
        "api_key_hash": api_key_hash_value,
        "active": True,
        "created_at": now,
        "updated_at": now,
    }

    result = sb.table("endpoints").insert(row).execute()
    endpoint = Endpoint(**result.data[0])
    # Attach plaintext key — only shown on creation
    endpoint.api_key = api_key

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
            # Persist the provider IDs and webhook secret back to the endpoint row
            sb.table("endpoints").update({
                "convoy_project_id": convoy_project_id,
                "convoy_endpoint_id": convoy_endpoint_id,
                "webhook_secret": webhook_secret,
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

    return endpoint


@router.post("/{endpoint_id}/rotate-key", response_model=RotateKeyResponse)
@limiter.limit(CRUD_LIMIT)
async def rotate_key(request: Request, endpoint_id: str, sb=Depends(get_supabase)):
    """Rotate the API key for an endpoint.

    Generates a new API key and moves the current hash to old_api_key_hash
    so both keys work during the grace period (default 24h).
    Returns the new plaintext key (shown once).
    """
    # Verify endpoint exists and is active
    existing = sb.table("endpoints").select("id, api_key_hash").eq("id", endpoint_id).eq("active", True).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    old_hash = existing.data[0]["api_key_hash"]
    now = datetime.now(timezone.utc)

    # Generate new key
    new_key = generate_api_key()
    new_hash = hash_api_key(new_key)

    sb.table("endpoints").update({
        "api_key_hash": new_hash,
        "old_api_key_hash": old_hash,
        "key_rotated_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }).eq("id", endpoint_id).execute()

    logger.info("api_key_rotated", endpoint_id=endpoint_id)

    return RotateKeyResponse(
        endpoint_id=endpoint_id,
        api_key=new_key,
        rotated_at=now.isoformat(),
    )


@router.get("", response_model=EndpointListResponse)
@limiter.limit(CRUD_LIMIT)
async def list_endpoints(request: Request, sb=Depends(get_supabase)):
    """List all registered endpoints."""
    result = sb.table("endpoints").select("*").eq("active", True).execute()
    return EndpointListResponse(data=result.data, count=len(result.data))


@router.get("/{endpoint_id}", response_model=Endpoint)
@limiter.limit(CRUD_LIMIT)
async def get_endpoint(request: Request, endpoint_id: str, sb=Depends(get_supabase)):
    """Get endpoint details."""
    result = sb.table("endpoints").select("*").eq("id", endpoint_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return Endpoint(**result.data[0])


@router.patch("/{endpoint_id}", response_model=Endpoint)
@limiter.limit(CRUD_LIMIT)
async def update_endpoint(request: Request, endpoint_id: str, body: UpdateEndpointRequest, sb=Depends(get_supabase)):
    """Update an endpoint."""

    # Verify exists
    existing = sb.table("endpoints").select("id").eq("id", endpoint_id).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Serialize enum and nested model
    if "mode" in updates:
        updates["mode"] = updates["mode"].value
    if "policies" in updates:
        updates["policies"] = updates["policies"].model_dump()

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()

    result = sb.table("endpoints").update(updates).eq("id", endpoint_id).execute()
    return Endpoint(**result.data[0])


@router.delete("/{endpoint_id}", status_code=204)
@limiter.limit(CRUD_LIMIT)
async def deactivate_endpoint(request: Request, endpoint_id: str, sb=Depends(get_supabase)):
    """Deactivate (soft-delete) an endpoint."""

    existing = sb.table("endpoints").select("id").eq("id", endpoint_id).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    sb.table("endpoints").update({
        "active": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", endpoint_id).execute()
