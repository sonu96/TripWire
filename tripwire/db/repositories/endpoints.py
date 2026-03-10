"""Endpoint CRUD repository."""

from datetime import datetime, timezone

import structlog
from nanoid import generate as nanoid
from supabase import Client

from tripwire.types.models import (
    Endpoint,
    EndpointPolicies,
    RegisterEndpointRequest,
)

logger = structlog.get_logger()


class EndpointRepository:
    """CRUD operations for the endpoints table."""

    def __init__(self, client: Client) -> None:
        self._sb = client

    def create(self, req: RegisterEndpointRequest, api_key_hash: str | None = None) -> Endpoint:
        """Insert a new endpoint and return it."""
        now = datetime.now(timezone.utc).isoformat()
        endpoint_id = nanoid(size=21)

        row = {
            "id": endpoint_id,
            "url": req.url,
            "mode": req.mode.value,
            "chains": req.chains,
            "recipient": req.recipient,
            "policies": (req.policies or EndpointPolicies()).model_dump(),
            "api_key_hash": api_key_hash,
            "active": True,
            "created_at": now,
            "updated_at": now,
        }

        result = self._sb.table("endpoints").insert(row).execute()
        logger.info("endpoint_created", endpoint_id=endpoint_id)
        return Endpoint(**result.data[0])

    def get_by_id(self, endpoint_id: str) -> Endpoint | None:
        """Fetch a single endpoint by ID, or None if not found."""
        result = self._sb.table("endpoints").select("*").eq("id", endpoint_id).execute()
        if not result.data:
            return None
        return Endpoint(**result.data[0])

    def list_active(self) -> list[Endpoint]:
        """Return all active endpoints."""
        result = (
            self._sb.table("endpoints")
            .select("*")
            .eq("active", True)
            .order("created_at", desc=True)
            .execute()
        )
        return [Endpoint(**row) for row in result.data]

    def list_by_recipient(self, recipient: str) -> list[Endpoint]:
        """Return active endpoints for a given recipient address."""
        result = (
            self._sb.table("endpoints")
            .select("*")
            .eq("recipient", recipient.lower())
            .eq("active", True)
            .execute()
        )
        return [Endpoint(**row) for row in result.data]

    def update(self, endpoint_id: str, updates: dict) -> Endpoint | None:
        """Partially update an endpoint. Returns updated endpoint or None."""
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        result = (
            self._sb.table("endpoints")
            .update(updates)
            .eq("id", endpoint_id)
            .execute()
        )
        if not result.data:
            return None
        logger.info("endpoint_updated", endpoint_id=endpoint_id)
        return Endpoint(**result.data[0])

    def deactivate(self, endpoint_id: str) -> bool:
        """Soft-delete an endpoint. Returns True if the row was found."""
        result = (
            self._sb.table("endpoints")
            .update({
                "active": False,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", endpoint_id)
            .execute()
        )
        if result.data:
            logger.info("endpoint_deactivated", endpoint_id=endpoint_id)
            return True
        return False
