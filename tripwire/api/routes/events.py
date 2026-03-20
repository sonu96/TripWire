"""Event history routes."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from tripwire.api import get_supabase
from tripwire.api.auth import require_wallet_auth, WalletAuthContext
from tripwire.api.ratelimit import CRUD_LIMIT, limiter
from tripwire.types.models import WebhookEventType, execution_state_from_status

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["events"])


# ── Helper: verify endpoint ownership ────────────────────────


def _verify_endpoint_ownership(endpoint_row: dict, wallet_address: str) -> None:
    """Raise 403 if the endpoint does not belong to the authenticated wallet."""
    if endpoint_row.get("owner_address", "").lower() != wallet_address.lower():
        raise HTTPException(status_code=403, detail="Not authorized to access this endpoint")


class EventResponse(BaseModel):
    id: str
    endpoint_id: str | None = None
    type: WebhookEventType
    data: dict
    created_at: str
    execution_state: str | None = None
    safe_to_execute: bool | None = None
    trust_source: str | None = None


class EventListResponse(BaseModel):
    data: list[EventResponse]
    cursor: str | None = None
    has_more: bool = False


def _enrich_event(row: dict) -> dict:
    """Add execution_state, safe_to_execute, trust_source to an event row."""
    state, safe, source = execution_state_from_status(row.get("status", "pending"))
    row["execution_state"] = state.value
    row["safe_to_execute"] = safe
    row["trust_source"] = source.value
    return row


def _get_wallet_endpoint_ids(sb, wallet_address: str) -> list[str]:
    """Return all endpoint IDs owned by the given wallet address."""
    result = (
        sb.table("endpoints")
        .select("id")
        .eq("owner_address", wallet_address)
        .execute()
    )
    return [row["id"] for row in result.data]


@router.get("/events", response_model=EventListResponse)
@limiter.limit(CRUD_LIMIT)
async def list_events(
    request: Request,
    cursor: str | None = Query(None, description="Cursor for pagination (event id)"),
    limit: int = Query(50, ge=1, le=200),
    event_type: WebhookEventType | None = Query(None),
    chain_id: int | None = Query(None),
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """List events with cursor pagination and optional filters.

    Only returns events belonging to the authenticated wallet's endpoints.
    """

    # Get all endpoint IDs owned by this wallet
    endpoint_ids = _get_wallet_endpoint_ids(sb, wallet_auth.wallet_address)
    if not endpoint_ids:
        return EventListResponse(data=[], cursor=None, has_more=False)

    # Query via event_endpoints join table (supports multi-endpoint dispatch)
    linked = (
        sb.table("event_endpoints")
        .select("event_id")
        .in_("endpoint_id", endpoint_ids)
        .execute()
    )
    event_ids = list({r["event_id"] for r in (linked.data or [])})
    if not event_ids:
        return EventListResponse(data=[], cursor=None, has_more=False)

    query = sb.table("events").select("*").in_("id", event_ids).order("created_at", desc=True).limit(limit + 1)

    if cursor:
        # Fetch the cursor event's created_at for keyset pagination
        cursor_row = sb.table("events").select("created_at").eq("id", cursor).execute()
        if cursor_row.data:
            query = query.lt("created_at", cursor_row.data[0]["created_at"])

    if event_type:
        query = query.eq("type", event_type.value)

    if chain_id:
        query = query.eq("chain_id", chain_id)

    result = query.execute()
    rows = result.data

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    next_cursor = rows[-1]["id"] if has_more and rows else None
    return EventListResponse(
        data=[_enrich_event(r) for r in rows],
        cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/events/{event_id}", response_model=EventResponse)
@limiter.limit(CRUD_LIMIT)
async def get_event(
    request: Request,
    event_id: str,
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """Get event details (must belong to an endpoint owned by the authenticated wallet)."""
    result = sb.table("events").select("*").eq("id", event_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Event not found")

    event = result.data[0]

    # Verify ownership through event_endpoints join table
    linked = (
        sb.table("event_endpoints")
        .select("endpoint_id")
        .eq("event_id", event_id)
        .execute()
    )
    linked_endpoint_ids = [r["endpoint_id"] for r in (linked.data or [])]

    # Fall back to legacy endpoint_id column if no join table entries
    if not linked_endpoint_ids and event.get("endpoint_id"):
        linked_endpoint_ids = [event["endpoint_id"]]

    if not linked_endpoint_ids:
        raise HTTPException(status_code=403, detail="Not authorized to access this event")

    # Check if any linked endpoint belongs to the authenticated wallet
    wallet_endpoints = _get_wallet_endpoint_ids(sb, wallet_auth.wallet_address)
    if not set(linked_endpoint_ids) & set(wallet_endpoints):
        raise HTTPException(status_code=403, detail="Not authorized to access this event")

    return EventResponse(**_enrich_event(event))


@router.get("/endpoints/{endpoint_id}/events", response_model=EventListResponse)
@limiter.limit(CRUD_LIMIT)
async def list_endpoint_events(
    request: Request,
    endpoint_id: str,
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
    sb=Depends(get_supabase),
):
    """List events for a specific endpoint (must belong to authenticated wallet)."""

    # Verify endpoint exists and ownership
    ep = sb.table("endpoints").select("*").eq("id", endpoint_id).execute()
    if not ep.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    _verify_endpoint_ownership(ep.data[0], wallet_auth.wallet_address)

    # Query via event_endpoints join table (supports multi-endpoint dispatch)
    linked = (
        sb.table("event_endpoints")
        .select("event_id")
        .eq("endpoint_id", endpoint_id)
        .execute()
    )
    event_ids = [r["event_id"] for r in (linked.data or [])]
    if not event_ids:
        return EventListResponse(data=[], cursor=None, has_more=False)

    query = (
        sb.table("events")
        .select("*")
        .in_("id", event_ids)
        .order("created_at", desc=True)
        .limit(limit + 1)
    )

    if cursor:
        cursor_row = sb.table("events").select("created_at").eq("id", cursor).execute()
        if cursor_row.data:
            query = query.lt("created_at", cursor_row.data[0]["created_at"])

    result = query.execute()
    rows = result.data

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    next_cursor = rows[-1]["id"] if has_more and rows else None
    return EventListResponse(
        data=[_enrich_event(r) for r in rows],
        cursor=next_cursor,
        has_more=has_more,
    )
