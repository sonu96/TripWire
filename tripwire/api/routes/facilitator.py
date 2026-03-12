"""x402 facilitator hook endpoint for pre-settlement payment detection.

Receives structured ERC-3009 authorization data from the x402 facilitator
BEFORE the transaction is submitted onchain.  The facilitator has already
verified the ERC-3009 signature — TripWire skips decode and finality
and runs only: nonce dedup, identity resolution, policy evaluation,
and dispatch (~100ms fast path).
"""

from __future__ import annotations

import hmac
import time
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from tripwire.api.ratelimit import INGEST_LIMIT, limiter
from tripwire.config.settings import settings
from tripwire.types.models import ChainId, ERC3009Transfer, USDC_CONTRACTS

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingestion"])


# ── Known USDC addresses (lowercased for comparison) ──────────

_KNOWN_USDC: set[str] = {addr.lower() for addr in USDC_CONTRACTS.values()}

_SUPPORTED_CHAINS: set[int] = {c.value for c in ChainId}


# ── Auth dependency ──────────────────────────────────────────


async def _verify_facilitator_request(request: Request) -> None:
    """Verify that the request originates from the x402 facilitator.

    Uses a separate ``facilitator_webhook_secret`` so that Goldsky and
    facilitator credentials can be rotated independently.
    """
    secret = settings.facilitator_webhook_secret
    if not secret:
        if settings.app_env != "development":
            raise HTTPException(
                status_code=500,
                detail="FACILITATOR_WEBHOOK_SECRET must be set in production",
            )
        logger.debug("facilitator_auth_skipped", reason="no secret configured")
        return

    auth_header = request.headers.get("Authorization", "")
    expected = f"Bearer {secret}"

    if not hmac.compare_digest(auth_header, expected):
        logger.warning(
            "facilitator_auth_failed",
            remote=request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=401, detail="Invalid or missing Authorization")

    logger.debug("facilitator_auth_success")


# ── Request / response models ─────────────────────────────────


class FacilitatorPayload(BaseModel):
    """Structured ERC-3009 authorization data sent by the facilitator."""

    from_address: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")
    to_address: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")
    amount: str  # string to preserve USDC 6-decimal precision
    nonce: str  # bytes32 hex nonce
    chain_id: int
    token: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")  # USDC contract address
    valid_after: int
    valid_before: int
    signature_verified: bool

    @field_validator("token")
    @classmethod
    def token_must_be_known_usdc(cls, v: str) -> str:
        if v.lower() not in _KNOWN_USDC:
            raise ValueError(f"Unknown USDC token address: {v}")
        return v

    @field_validator("chain_id")
    @classmethod
    def chain_must_be_supported(cls, v: int) -> int:
        if v not in _SUPPORTED_CHAINS:
            raise ValueError(f"Unsupported chain_id: {v}")
        return v


class FacilitatorResponse(BaseModel):
    status: str
    event_id: str | None = None
    tx_hash: str | None = None


# ── Route ────────────────────────────────────────────────────


@router.post(
    "/facilitator",
    response_model=FacilitatorResponse,
    dependencies=[Depends(_verify_facilitator_request)],
)
@limiter.limit(INGEST_LIMIT)
async def ingest_facilitator(
    request: Request,
    body: FacilitatorPayload,
):
    """Receive a pre-settlement ERC-3009 authorization from the x402 facilitator.

    The facilitator has verified the ERC-3009 signature and is about to submit
    the transaction.  There is no tx_hash or block_number yet — this endpoint
    provides the ~100ms fast path for payment detection.
    """
    t_start = time.perf_counter()

    # ── Validate signature_verified flag ──────────────────────
    if not body.signature_verified:
        logger.warning(
            "facilitator_signature_not_verified",
            from_address=body.from_address,
            to_address=body.to_address,
        )
        raise HTTPException(
            status_code=422,
            detail="signature_verified must be true",
        )

    # ── Build a synthetic ERC3009Transfer (no onchain fields) ─
    # Use a deterministic pseudo-tx-hash so the event can be
    # correlated later when the real tx lands onchain.
    pseudo_tx_hash = f"0x{'0' * 24}{uuid.uuid4().hex[:40]}"

    transfer = ERC3009Transfer(
        chain_id=ChainId(body.chain_id),
        tx_hash=pseudo_tx_hash,
        block_number=0,  # not yet onchain
        block_hash="0x" + "0" * 64,  # placeholder
        log_index=0,
        from_address=body.from_address,
        to_address=body.to_address,
        value=body.amount,
        authorizer=body.from_address,  # ERC-3009: authorizer == sender
        valid_after=body.valid_after,
        valid_before=body.valid_before,
        nonce=body.nonce,
        token=body.token,
        timestamp=int(time.time()),
    )

    # ── Run through processor (skips decode + finality) ───────
    processor = request.app.state.processor
    result = await processor.process_pre_confirmed_event(transfer)

    elapsed_ms = round((time.perf_counter() - t_start) * 1000, 2)
    logger.info(
        "facilitator_ingest_complete",
        status=result.get("status"),
        event_id=result.get("event_id"),
        elapsed_ms=elapsed_ms,
    )

    return FacilitatorResponse(
        status=result.get("status", "error"),
        event_id=result.get("event_id"),
        tx_hash=result.get("tx_hash", pseudo_tx_hash),
    )
