"""Session management routes — open, query, and close Keeper sessions.

Sessions provide a pre-authorized spending limit for MCP tool calls, eliminating
per-call x402 payment negotiation.  Authenticated via SIWE (``require_wallet_auth``).

Feature-flagged: only available when ``session_enabled=True`` and Keeper mode is active.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from tripwire.api.auth import WalletAuthContext, require_wallet_auth
from tripwire.config.settings import settings
from tripwire.observability.audit import fire_and_forget
from tripwire.session.manager import SessionManager

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["session"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class OpenSessionRequest(BaseModel):
    """Request body for POST /auth/session."""

    budget: int | None = Field(
        default=None,
        description="Budget in smallest USDC units (6 decimals).  "
        "Clamped to server max.  Defaults to server default.",
    )
    ttl_seconds: int | None = Field(
        default=None,
        description="Session lifetime in seconds.  "
        "Clamped to server max.  Defaults to server default.",
    )
    chain_id: int | None = Field(
        default=None,
        description="Chain ID context for identity resolution.",
    )


class SessionResponse(BaseModel):
    """Shared response shape for session endpoints."""

    session_id: str
    wallet_address: str
    budget_total: int
    budget_remaining: int
    expires_at: str  # ISO-8601
    ttl_seconds: int
    chain_id: int
    status: str = "active"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_session_manager(request: Request) -> SessionManager:
    """Extract the SessionManager from app state, raising 501 if not available."""
    manager = getattr(request.app.state, "session_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=501,
            detail="Session system is not enabled",
        )
    return manager


def _get_audit_logger(request: Request):
    """Return the audit logger if available, else None."""
    return getattr(
        getattr(request, "app", None), "state", None
    ) and getattr(request.app.state, "audit_logger", None)


def _format_expires_at(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# POST /auth/session — Open session
# ---------------------------------------------------------------------------


@router.post("/session")
async def open_session(
    request: Request,
    body: OpenSessionRequest,
    wallet: WalletAuthContext = Depends(require_wallet_auth),
) -> SessionResponse:
    """Create a new Keeper session with a server-side spending limit.

    The session is free to create (gated only by SIWE auth).  The budget is a
    server-side spending limit that authorises subsequent MCP tool calls via the
    ``X-TripWire-Session`` header.
    """
    if not settings.session_enabled:
        raise HTTPException(status_code=501, detail="Session system is not enabled")

    manager = _get_session_manager(request)

    # Resolve identity for caching reputation_score + agent_class in the session
    reputation_score = 0.0
    agent_class = "unknown"
    identity_resolver = getattr(request.app.state, "identity_resolver", None)
    if identity_resolver is not None:
        chain_id = body.chain_id if body.chain_id is not None else settings.siwe_chain_id
        try:
            identity = await identity_resolver.resolve(wallet.wallet_address, chain_id)
            if identity is not None:
                reputation_score = identity.reputation_score
                agent_class = getattr(identity, "agent_class", "unknown") or "unknown"
        except Exception:
            logger.warning(
                "session_identity_resolution_failed",
                wallet=wallet.wallet_address,
            )

    session = await manager.create(
        wallet_address=wallet.wallet_address,
        budget=body.budget,
        ttl_seconds=body.ttl_seconds,
        chain_id=body.chain_id,
        reputation_score=reputation_score,
        agent_class=agent_class,
    )

    # Audit log
    _audit = _get_audit_logger(request)
    if _audit:
        fire_and_forget(_audit.log(
            action="session.opened",
            actor=wallet.wallet_address,
            resource_type="session",
            resource_id=session.session_id,
            details={
                "budget_total": session.budget_total,
                "ttl_seconds": session.ttl_seconds,
                "chain_id": session.chain_id,
            },
            ip_address=request.client.host if request.client else None,
        ))

    return SessionResponse(
        session_id=session.session_id,
        wallet_address=session.wallet_address,
        budget_total=session.budget_total,
        budget_remaining=session.budget_remaining,
        expires_at=_format_expires_at(session.expires_at),
        ttl_seconds=session.ttl_seconds,
        chain_id=session.chain_id,
        status="active",
    )


# ---------------------------------------------------------------------------
# GET /auth/session/{session_id} — Get status
# ---------------------------------------------------------------------------


@router.get("/session/{session_id}")
async def get_session(
    session_id: str,
    request: Request,
    wallet: WalletAuthContext = Depends(require_wallet_auth),
) -> SessionResponse:
    """Retrieve the current state of a session.

    Only the wallet that created the session can query it.
    """
    manager = _get_session_manager(request)
    session = await manager.get(session_id)

    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Ownership check
    if session.wallet_address.lower() != wallet.wallet_address.lower():
        raise HTTPException(status_code=403, detail="Not the session owner")

    expired = time.time() > session.expires_at
    status = "expired" if expired else "active"

    return SessionResponse(
        session_id=session.session_id,
        wallet_address=session.wallet_address,
        budget_total=session.budget_total,
        budget_remaining=session.budget_remaining,
        expires_at=_format_expires_at(session.expires_at),
        ttl_seconds=session.ttl_seconds,
        chain_id=session.chain_id,
        status=status,
    )


# ---------------------------------------------------------------------------
# DELETE /auth/session/{session_id} — Close session
# ---------------------------------------------------------------------------


@router.delete("/session/{session_id}")
async def close_session(
    session_id: str,
    request: Request,
    wallet: WalletAuthContext = Depends(require_wallet_auth),
) -> SessionResponse:
    """Close a session and return its final state.

    Only the wallet that created the session can close it.
    """
    manager = _get_session_manager(request)

    # Pre-check ownership before closing
    session = await manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.wallet_address.lower() != wallet.wallet_address.lower():
        raise HTTPException(status_code=403, detail="Not the session owner")

    final = await manager.close(session_id)
    if final is None:
        raise HTTPException(status_code=404, detail="Session already closed")

    # Audit log
    _audit = _get_audit_logger(request)
    if _audit:
        fire_and_forget(_audit.log(
            action="session.closed",
            actor=wallet.wallet_address,
            resource_type="session",
            resource_id=session_id,
            details={
                "budget_remaining": final.budget_remaining,
                "budget_total": final.budget_total,
            },
            ip_address=request.client.host if request.client else None,
        ))

    return SessionResponse(
        session_id=final.session_id,
        wallet_address=final.wallet_address,
        budget_total=final.budget_total,
        budget_remaining=final.budget_remaining,
        expires_at=_format_expires_at(final.expires_at),
        ttl_seconds=final.ttl_seconds,
        chain_id=final.chain_id,
        status="closed",
    )
