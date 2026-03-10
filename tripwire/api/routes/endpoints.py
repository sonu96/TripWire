"""Endpoint registration routes."""

from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from nanoid import generate as nanoid
from pydantic import BaseModel

from tripwire.api import get_supabase
from tripwire.api.auth import generate_api_key, hash_api_key, require_api_key
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


class UpdateEndpointRequest(BaseModel):
    url: str | None = None
    mode: EndpointMode | None = None
    chains: list[int] | None = None
    policies: EndpointPolicies | None = None
    active: bool | None = None


@router.post("", response_model=Endpoint, status_code=201)
async def register_endpoint(body: RegisterEndpointRequest, request: Request, sb=Depends(get_supabase)):
    """Register a new webhook endpoint.

    Also creates a corresponding Svix application and endpoint so that
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
            svix_app_id = await provider.create_app(
                developer_id=endpoint_id,
                name=f"tripwire-{endpoint_id}",
            )
            svix_endpoint_id = await provider.create_endpoint(
                app_id=svix_app_id,
                url=body.url,
                description=f"TripWire endpoint for {body.recipient}",
            )
            # Persist the provider IDs back to the endpoint row
            sb.table("endpoints").update({
                "svix_app_id": svix_app_id,
                "svix_endpoint_id": svix_endpoint_id,
            }).eq("id", endpoint_id).execute()
            endpoint.svix_app_id = svix_app_id
            endpoint.svix_endpoint_id = svix_endpoint_id
            logger.info(
                "webhook_provider_wired",
                endpoint_id=endpoint_id,
                svix_app_id=svix_app_id,
                svix_endpoint_id=svix_endpoint_id,
            )
        except Exception:
            logger.exception("webhook_provider_setup_failed", endpoint_id=endpoint_id)

    return endpoint


@router.get("", response_model=EndpointListResponse)
async def list_endpoints(sb=Depends(get_supabase)):
    """List all registered endpoints."""
    result = sb.table("endpoints").select("*").eq("active", True).execute()
    return EndpointListResponse(data=result.data, count=len(result.data))


@router.get("/{endpoint_id}", response_model=Endpoint)
async def get_endpoint(endpoint_id: str, sb=Depends(get_supabase)):
    """Get endpoint details."""
    result = sb.table("endpoints").select("*").eq("id", endpoint_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return Endpoint(**result.data[0])


@router.patch("/{endpoint_id}", response_model=Endpoint)
async def update_endpoint(endpoint_id: str, body: UpdateEndpointRequest, sb=Depends(get_supabase)):
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
async def deactivate_endpoint(endpoint_id: str, sb=Depends(get_supabase)):
    """Deactivate (soft-delete) an endpoint."""

    existing = sb.table("endpoints").select("id").eq("id", endpoint_id).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    sb.table("endpoints").update({
        "active": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", endpoint_id).execute()
