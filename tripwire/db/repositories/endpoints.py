"""Endpoint CRUD repository."""

import structlog
from supabase import Client

from tripwire.types.models import Endpoint

logger = structlog.get_logger(__name__)


class EndpointRepository:
    """CRUD operations for the endpoints table."""

    def __init__(self, client: Client) -> None:
        self._sb = client

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

