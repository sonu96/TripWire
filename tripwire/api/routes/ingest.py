"""Goldsky ingestion webhook endpoint.

Receives decoded ERC-3009 AuthorizationUsed events from Goldsky
and processes them through the full TripWire pipeline.
"""

from __future__ import annotations

import hmac
from typing import Any

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel

from tripwire.api.ratelimit import INGEST_LIMIT, limiter
from tripwire.config.settings import settings

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingestion"])


# ── Goldsky webhook auth dependency ──────────────────────────


async def _verify_goldsky_request(request: Request) -> None:
    """Verify that the request originates from Goldsky.

    If ``settings.goldsky_webhook_secret`` is empty the check is
    skipped (convenient during local development).
    """
    secret = settings.goldsky_webhook_secret.get_secret_value()
    if not secret:
        if settings.app_env != "development":
            raise HTTPException(
                status_code=500,
                detail="GOLDSKY_WEBHOOK_SECRET must be set in production",
            )
        logger.debug("goldsky_auth_skipped", reason="no secret configured")
        return

    auth_header = request.headers.get("Authorization", "")
    expected = f"Bearer {secret}"

    if not hmac.compare_digest(auth_header, expected):
        logger.warning(
            "goldsky_auth_failed",
            remote=request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=401, detail="Invalid or missing Authorization")

    logger.debug("goldsky_auth_success")


# ── Response models ──────────────────────────────────────────


class IngestResponse(BaseModel):
    processed: int
    results: list[dict[str, Any]]


class IngestSingleResponse(BaseModel):
    status: str
    tx_hash: str | None = None
    event_id: str | None = None


# ── Routes ───────────────────────────────────────────────────


@router.post(
    "/goldsky",
    response_model=IngestResponse,
    dependencies=[Depends(_verify_goldsky_request)],
)
@limiter.limit(INGEST_LIMIT)
async def ingest_goldsky_batch(
    request: Request,
    body: list[dict[str, Any]] | dict[str, Any] = Body(),
):
    """Receive a batch of Goldsky-decoded events.

    Goldsky webhook sink sends an array of decoded log rows.
    Each row has been decoded by _gs_log_decode() and contains:
      - transaction_hash, block_number, block_hash, log_index
      - block_timestamp, address, chain_id
      - decoded: {authorizer, nonce}
    """
    processor = request.app.state.processor

    # Goldsky sends either a single object or an array
    raw_logs = [body] if isinstance(body, dict) else body

    logger.info("goldsky_ingest_received", count=len(raw_logs))

    if len(raw_logs) > 1000:
        raise HTTPException(status_code=400, detail="Batch too large (max 1000)")

    if settings.event_bus_enabled and hasattr(request.app.state, "worker_pool"):
        from tripwire.ingestion.event_bus import publish_batch

        try:
            message_ids = await publish_batch(raw_logs)
        except Exception as exc:
            # Fallback to synchronous processing when Redis/event bus is down
            logger.warning(
                "event_bus_publish_failed_falling_back",
                error=type(exc).__name__,
                count=len(raw_logs),
            )
            results = await processor.process_batch(raw_logs)
            return IngestResponse(processed=len(results), results=results)
        return IngestResponse(
            processed=len(message_ids),
            results=[{"status": "queued", "stream_id": mid} for mid in message_ids],
        )

    results = await processor.process_batch(raw_logs)

    return IngestResponse(processed=len(results), results=results)


@router.post(
    "/event",
    response_model=IngestSingleResponse,
    dependencies=[Depends(_verify_goldsky_request)],
)
@limiter.limit(INGEST_LIMIT)
async def ingest_single_event(
    request: Request,
    body: dict[str, Any] = Body(),
):
    """Process a single raw event (for testing or manual submission)."""
    processor = request.app.state.processor

    if settings.event_bus_enabled and hasattr(request.app.state, "worker_pool"):
        from tripwire.ingestion.event_bus import publish_event

        try:
            message_id = await publish_event(body)
        except Exception as exc:
            # Fallback to synchronous processing when Redis/event bus is down
            logger.warning("event_bus_publish_failed_falling_back", error=type(exc).__name__)
            result = await processor.process_event(body)
            return IngestSingleResponse(**result)
        return IngestSingleResponse(
            status="queued",
            tx_hash=body.get("transaction_hash"),
            event_id=message_id,
        )

    result = await processor.process_event(body)
    return IngestSingleResponse(**result)
