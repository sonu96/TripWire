"""Event history routes."""

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from tripwire.types.models import WebhookEventType

router = APIRouter(tags=["events"])


class EventResponse(BaseModel):
    id: str
    endpoint_id: str
    type: WebhookEventType
    data: dict
    created_at: str


class EventListResponse(BaseModel):
    data: list[EventResponse]
    cursor: str | None = None
    has_more: bool = False


def _supabase(request: Request):
    return request.app.state.supabase


@router.get("/events", response_model=EventListResponse)
async def list_events(
    request: Request,
    cursor: str | None = Query(None, description="Cursor for pagination (event id)"),
    limit: int = Query(50, ge=1, le=200),
    event_type: WebhookEventType | None = Query(None),
    chain_id: int | None = Query(None),
):
    """List events with cursor pagination and optional filters."""
    sb = _supabase(request)

    query = sb.table("events").select("*").order("created_at", desc=True).limit(limit + 1)

    if cursor:
        # Fetch the cursor event's created_at for keyset pagination
        cursor_row = sb.table("events").select("created_at").eq("id", cursor).execute()
        if cursor_row.data:
            query = query.lt("created_at", cursor_row.data[0]["created_at"])

    if event_type:
        query = query.eq("type", event_type.value)

    if chain_id:
        query = query.eq("data->>chain_id", str(chain_id))

    result = query.execute()
    rows = result.data

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    next_cursor = rows[-1]["id"] if has_more and rows else None
    return EventListResponse(data=rows, cursor=next_cursor, has_more=has_more)


@router.get("/events/{event_id}", response_model=EventResponse)
async def get_event(event_id: str, request: Request):
    """Get event details."""
    sb = _supabase(request)
    result = sb.table("events").select("*").eq("id", event_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Event not found")
    return EventResponse(**result.data[0])


@router.get("/endpoints/{endpoint_id}/events", response_model=EventListResponse)
async def list_endpoint_events(
    endpoint_id: str,
    request: Request,
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """List events for a specific endpoint."""
    sb = _supabase(request)

    # Verify endpoint exists
    ep = sb.table("endpoints").select("id").eq("id", endpoint_id).execute()
    if not ep.data:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    query = (
        sb.table("events")
        .select("*")
        .eq("endpoint_id", endpoint_id)
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
    return EventListResponse(data=rows, cursor=next_cursor, has_more=has_more)
