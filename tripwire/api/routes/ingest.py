"""Goldsky ingestion webhook endpoint.

Receives decoded ERC-3009 AuthorizationUsed events from Goldsky
and processes them through the full TripWire pipeline.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingestion"])


class IngestResponse(BaseModel):
    processed: int
    results: list[dict[str, Any]]


class IngestSingleResponse(BaseModel):
    status: str
    tx_hash: str | None = None
    event_id: str | None = None


@router.post("/goldsky", response_model=IngestResponse)
async def ingest_goldsky_batch(request: Request):
    """Receive a batch of Goldsky-decoded events.

    Goldsky webhook sink sends an array of decoded log rows.
    Each row has been decoded by _gs_log_decode() and contains:
      - transaction_hash, block_number, block_hash, log_index
      - block_timestamp, address, chain_id
      - decoded: {authorizer, nonce}
    """
    processor = request.app.state.processor
    body = await request.json()

    # Goldsky sends either a single object or an array
    if isinstance(body, dict):
        raw_logs = [body]
    elif isinstance(body, list):
        raw_logs = body
    else:
        return IngestResponse(processed=0, results=[])

    logger.info("goldsky_ingest_received", count=len(raw_logs))
    results = await processor.process_batch(raw_logs)

    return IngestResponse(processed=len(results), results=results)


@router.post("/event", response_model=IngestSingleResponse)
async def ingest_single_event(request: Request):
    """Process a single raw event (for testing or manual submission)."""
    processor = request.app.state.processor
    body = await request.json()

    result = await processor.process_event(body)
    return IngestSingleResponse(**result)
