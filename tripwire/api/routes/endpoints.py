"""Endpoint registration routes."""

from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, HTTPException, Request
from nanoid import generate as nanoid
from pydantic import BaseModel

from tripwire.types.models import (
    Endpoint,
    EndpointMode,
    EndpointPolicies,
    RegisterEndpointRequest,
)
from tripwire.webhook.svix_client import create_application, create_endpoint as create_svix_endpoint

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


def _supabase(request: Request):
    return request.app.state.supabase


@router.post("", response_model=Endpoint, status_code=201)
async def register_endpoint(body: RegisterEndpointRequest, request: Request):
    """Register a new webhook endpoint.

    Also creates a corresponding Svix application and endpoint so that
    webhook delivery is ready as soon as the endpoint is registered.
    """
    sb = _supabase(request)
    now = datetime.now(timezone.utc).isoformat()
    endpoint_id = nanoid(size=21)

    row = {
        "id": endpoint_id,
        "url": body.url,
        "mode": body.mode.value,
        "chains": body.chains,
        "recipient": body.recipient,
        "policies": (body.policies or EndpointPolicies()).model_dump(),
        "active": True,
        "created_at": now,
        "updated_at": now,
    }

    result = sb.table("endpoints").insert(row).execute()
    endpoint = Endpoint(**result.data[0])

    # Create Svix application + endpoint for webhook delivery
    if body.mode == EndpointMode.EXECUTE:
        try:
            await create_application(
                developer_id=endpoint_id,
                name=f"tripwire-{endpoint_id}",
            )
            await create_svix_endpoint(
                app_id=endpoint_id,
                url=body.url,
                description=f"TripWire endpoint for {body.recipient}",
            )
            logger.info("svix_wired", endpoint_id=endpoint_id)
        except Exception:
            logger.exception("svix_setup_failed", endpoint_id=endpoint_id)

    return endpoint


@router.get("", response_model=EndpointListResponse)
async def list_endpoints(request: Request):
    """List all registered endpoints."""
    sb = _supabase(request)
    result = sb.table("endpoints").select("*").eq("active", True).execute()
    return EndpointListResponse(data=result.data, count=len(result.data))


@router.get("/{endpoint_id}", response_model=Endpoint)
async def get_endpoint(endpoint_id: str, request: Request):
    """Get endpoint details."""
    sb = _supabase(request)
    result = sb.table("endpoints").select("*").eq("id", endpoint_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return Endpoint(**result.data[0])


@router.patch("/{endpoint_id}", response_model=Endpoint)
async def update_endpoint(endpoint_id: str, body: UpdateEndpointRequest, request: Request):
    """Update an endpoint."""
    sb = _supabase(request)

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
async def deactivate_endpoint(endpoint_id: str, request: Request):
    """Deactivate (soft-delete) an endpoint."""
    sb = _supabase(request)

    existing = sb.table("endpoints").select("id").eq("id", endpoint_id).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    sb.table("endpoints").update({
        "active": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", endpoint_id).execute()
