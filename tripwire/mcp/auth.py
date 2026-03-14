"""MCP authentication: SIWE wallet auth and x402 payment verification.

Builds an MCPAuthContext for each tool invocation based on the tool's AuthTier.
For x402 tools, payment is verified but NOT settled until the caller explicitly
calls settle_payment() after successful tool execution.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import HTTPException, Request

from tripwire.api.auth import _build_siwe_message
from tripwire.api.redis import get_redis
from tripwire.config.settings import settings
from tripwire.identity.resolver import IdentityResolver
from tripwire.mcp.types import AuthTier, MCPAuthContext, ToolDef

logger = structlog.get_logger(__name__)

# Optional x402 imports -- gracefully degrade if not installed.
try:
    from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
    from x402.mechanisms.evm.exact import ExactEvmServerScheme
    from x402.server import x402ResourceServer

    _X402_AVAILABLE = True
except ImportError:  # pragma: no cover
    _X402_AVAILABLE = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_x402_instance: "x402ResourceServer | None" = None


def _x402_server() -> "x402ResourceServer":
    """Return a cached x402ResourceServer singleton.

    Must be a singleton so that verify() and settle() share state.
    """
    global _x402_instance
    if not _X402_AVAILABLE:
        raise HTTPException(
            status_code=501,
            detail="x402 payment support is not installed",
        )
    if _x402_instance is None:
        _x402_instance = x402ResourceServer(
            HTTPFacilitatorClient(
                FacilitatorConfig(url=settings.x402_facilitator_url)
            )
        )
        _x402_instance.register(settings.x402_network, ExactEvmServerScheme())
    return _x402_instance


async def _verify_siwe(request: Request) -> str:
    """Verify SIWE headers and return the recovered wallet address.

    Mirrors the logic in ``tripwire.api.auth.require_wallet_auth`` but is
    decoupled from the FastAPI dependency-injection system so it can be
    called programmatically from the MCP auth layer.
    """
    address = request.headers.get("X-TripWire-Address")
    signature = request.headers.get("X-TripWire-Signature")
    nonce = request.headers.get("X-TripWire-Nonce")
    issued_at = request.headers.get("X-TripWire-Issued-At")
    expiration_time = request.headers.get("X-TripWire-Expiration")

    if not all([address, signature, nonce, issued_at, expiration_time]):
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing SIWE authentication headers; "
                "X-TripWire-Address, X-TripWire-Signature, X-TripWire-Nonce, "
                "X-TripWire-Issued-At, and X-TripWire-Expiration are all required"
            ),
        )

    # Expiration validation
    try:
        exp_dt = datetime.fromisoformat(expiration_time)
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid expiration time format")

    if datetime.now(timezone.utc) > exp_dt:
        raise HTTPException(status_code=401, detail="Signature has expired")

    # Issued-at validation (reject messages signed too far in the past or future)
    try:
        iat_dt = datetime.fromisoformat(issued_at)
        if iat_dt.tzinfo is None:
            iat_dt = iat_dt.replace(tzinfo=timezone.utc)
        tolerance = settings.auth_timestamp_tolerance_seconds
        now = datetime.now(timezone.utc)
        if abs((now - iat_dt).total_seconds()) > tolerance:
            raise HTTPException(status_code=401, detail="Issued-at timestamp out of tolerance")
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid issued-at time format")

    # Body hash
    body_bytes = await request.body()
    body_hash = hashlib.sha256(body_bytes).hexdigest()

    # Reconstruct SIWE message
    method = request.method
    path = request.url.path
    statement = f"{method} {path} {body_hash}"

    message_text = _build_siwe_message(
        domain=settings.siwe_domain,
        address=address,
        statement=statement,
        nonce=nonce,
        issued_at=issued_at,
        expiration_time=expiration_time,
    )
    signable = encode_defunct(text=message_text)

    # Signature recovery
    try:
        recovered = Account.recover_message(signable, signature=signature)
    except Exception as exc:
        logger.warning("mcp_siwe_recovery_failed", error=str(exc))
        raise HTTPException(status_code=401, detail="Invalid signature")

    if recovered.lower() != address.lower():
        logger.warning(
            "mcp_siwe_address_mismatch",
            claimed=address,
            recovered=recovered,
        )
        raise HTTPException(
            status_code=401,
            detail="Signature does not match claimed address",
        )

    # Atomic nonce consumption
    r = get_redis()
    consumed = await r.delete(f"siwe:nonce:{nonce}")
    if consumed == 0:
        logger.warning("mcp_siwe_nonce_invalid", nonce=nonce)
        raise HTTPException(status_code=401, detail="Invalid or already-used nonce")

    logger.debug("mcp_siwe_auth_ok", wallet_address=recovered)
    return recovered


async def _verify_x402_payment(
    request: Request,
    tool_def: ToolDef,
) -> str:
    """Verify the X-PAYMENT header for an x402-gated tool.

    Returns the payer address extracted from the payment proof.
    The payment is verified but NOT settled -- the caller must invoke
    ``settle_payment`` after successful tool execution.
    """
    payment_header = request.headers.get("X-PAYMENT")
    if not payment_header:
        raise HTTPException(
            status_code=402,
            detail="Payment required. Include an X-PAYMENT header.",
        )

    # --- Replay protection: atomic claim of payment proof --------------------
    payment_hash = hashlib.sha256(payment_header.encode()).hexdigest()
    dedup_key = f"x402:payment:{payment_hash}:{tool_def.name}"
    r = get_redis()

    # Atomic SET NX — only one concurrent request can claim this payment
    was_set = await r.set(dedup_key, "1", ex=86400, nx=True)  # 24h TTL
    if not was_set:
        logger.warning(
            "mcp_x402_payment_replay",
            tool=tool_def.name,
            payment_hash=payment_hash,
        )
        raise HTTPException(status_code=402, detail="Payment already used")

    server = _x402_server()

    payment_option = PaymentOption(
        scheme="exact",
        price=tool_def.price or "$0.00",
        network=tool_def.network,
        pay_to=settings.tripwire_treasury_address,
    )

    try:
        result = await server.verify(payment_header, payment_option)
    except Exception as exc:
        # Verification failed — release the dedup key so the payment can be retried
        await r.delete(dedup_key)
        logger.warning("mcp_x402_verify_failed", error=str(exc), tool=tool_def.name)
        raise HTTPException(status_code=402, detail="Payment verification failed")

    if not result.valid:
        await r.delete(dedup_key)
        raise HTTPException(status_code=402, detail="Payment verification failed")

    payer = getattr(result, "payer", None) or getattr(result, "from_address", None)
    payer_address = str(payer) if payer else None

    logger.info(
        "mcp_x402_payment_verified",
        tool=tool_def.name,
        payer=payer_address,
        price=tool_def.price,
        payment_hash=payment_hash,
    )
    return payer_address or ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def build_auth_context(
    request: Request,
    tool_def: ToolDef,
    identity_resolver: Any,
) -> MCPAuthContext:
    """Build an MCPAuthContext for *tool_def* based on its AuthTier.

    Parameters
    ----------
    request:
        The incoming FastAPI ``Request`` (carries headers).
    tool_def:
        The MCP tool being invoked (carries auth tier and pricing info).
    identity_resolver:
        An ``IdentityResolver`` instance for ERC-8004 identity lookups.

    Returns
    -------
    MCPAuthContext
        Populated auth context ready to be forwarded to the tool handler.
    """

    # -- PUBLIC tier: no auth required -----------------------------------------
    if tool_def.auth_tier == AuthTier.PUBLIC:
        return MCPAuthContext(auth_tier=AuthTier.PUBLIC)

    # -- SIWX tier: wallet signature required ----------------------------------
    if tool_def.auth_tier == AuthTier.SIWX:
        wallet_address = await _verify_siwe(request)

        identity = None
        reputation_score = 0.0
        if identity_resolver is not None:
            # Default to Base chain for identity resolution
            identity = await identity_resolver.resolve(wallet_address, 8453)
            if identity is not None:
                reputation_score = identity.reputation_score

        # Reputation gating is handled by the server after context is built
        return MCPAuthContext(
            auth_tier=AuthTier.SIWX,
            agent_address=wallet_address,
            identity=identity,
            reputation_score=reputation_score,
        )

    # -- X402 tier: payment required -------------------------------------------
    if tool_def.auth_tier == AuthTier.X402:
        payer_address = await _verify_x402_payment(request, tool_def)

        identity = None
        reputation_score = 0.0
        if identity_resolver is not None and payer_address:
            identity = await identity_resolver.resolve(payer_address, 8453)
            if identity is not None:
                reputation_score = identity.reputation_score

        # Reputation gating is handled by the server after context is built
        return MCPAuthContext(
            auth_tier=AuthTier.X402,
            agent_address=payer_address or None,
            identity=identity,
            reputation_score=reputation_score,
            payment_verified=True,
            payer_address=payer_address or None,
        )

    raise HTTPException(status_code=500, detail=f"Unknown auth tier: {tool_def.auth_tier}")


async def settle_payment(request: Request) -> None:
    """Settle an x402 payment after successful tool execution.

    Should only be called when the tool handler has completed successfully.
    If the x402 package is not installed or no payment header is present,
    this is a no-op.
    """
    if not _X402_AVAILABLE:
        return

    payment_header = request.headers.get("X-PAYMENT")
    if not payment_header:
        return

    server = _x402_server()

    try:
        await server.settle(payment_header)
        logger.info("mcp_x402_payment_settled")
    except Exception as exc:
        logger.error("mcp_x402_settle_failed", error=str(exc))
        raise
