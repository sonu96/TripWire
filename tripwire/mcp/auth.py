"""MCP authentication: SIWE wallet auth and x402 payment verification.

Builds an MCPAuthContext for each tool invocation based on the tool's AuthTier.

For x402 tools the ``TripWirePaymentHooks`` class encapsulates TripWire's
value-add (replay protection, identity resolution, reputation gating, rate
limiting, audit logging) as lifecycle hooks around the x402 SDK's
verify/settle flow.  The ``x402_tool_executor()`` orchestrator drives the
full lifecycle: verify -> before_execution -> tool handler -> after_execution
-> settle -> on_settlement_success / on_settlement_failure.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import structlog
from fastapi import HTTPException, Request

from tripwire.api.redis import get_redis
from tripwire.auth.siwe import build_siwe_message, build_request_statement, verify_siwe_signature, validate_timestamps
from tripwire.config.settings import settings
from tripwire.identity.resolver import IdentityResolver
from tripwire.mcp.types import AuthTier, MCPAuthContext, ToolDef
from tripwire.observability.audit import AuditLogger, fire_and_forget
from tripwire.utils.caip import caip2_to_chain_id

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
# x402ResourceServer singleton (shared by verify and settle)
# ---------------------------------------------------------------------------

_x402_instance: "x402ResourceServer | None" = None


def _get_x402_server() -> "x402ResourceServer":
    """Return a cached x402ResourceServer singleton."""
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
        for network in settings.x402_networks:
            _x402_instance.register(network, ExactEvmServerScheme())
    return _x402_instance


# ---------------------------------------------------------------------------
# SIWE verification (SIWX tier — uses shared tripwire.auth.siwe module)
# ---------------------------------------------------------------------------


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

    # Expiration + issued-at validation (MCP uses issued-at tolerance)
    try:
        validate_timestamps(
            issued_at,
            expiration_time,
            check_issued_at_tolerance=True,
            tolerance_seconds=settings.auth_timestamp_tolerance_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    # Reconstruct SIWE message
    body_bytes = await request.body()
    method = request.method
    path = request.url.path
    statement = build_request_statement(method, path, body_bytes)

    message_text = build_siwe_message(
        domain=settings.siwe_domain,
        address=address,
        statement=statement,
        nonce=nonce,
        issued_at=issued_at,
        expiration_time=expiration_time,
        chain_id=settings.siwe_chain_id,
    )

    # Signature recovery + address comparison
    try:
        recovered = verify_siwe_signature(message_text, signature, address)
    except ValueError as exc:
        logger.warning("mcp_siwe_recovery_failed", error=str(exc))
        raise HTTPException(status_code=401, detail="Invalid signature or address mismatch")
    except Exception as exc:
        logger.warning("mcp_siwe_recovery_failed", error=str(exc))
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Atomic nonce consumption
    r = get_redis()
    consumed = await r.delete(f"siwe:nonce:{nonce}")
    if consumed == 0:
        logger.warning("mcp_siwe_nonce_invalid", nonce=nonce)
        raise HTTPException(status_code=401, detail="Invalid or already-used nonce")

    logger.debug("mcp_siwe_auth_ok", wallet_address=recovered)
    return recovered


# ---------------------------------------------------------------------------
# Session verification (SESSION tier)
# ---------------------------------------------------------------------------


def _price_to_smallest_units(price: str | None) -> int:
    """Convert a price string like ``'$0.003'`` to smallest USDC units (6 decimals).

    Examples
    --------
    >>> _price_to_smallest_units("$0.003")
    3000
    >>> _price_to_smallest_units(None)
    0
    >>> _price_to_smallest_units("$1.00")
    1000000
    """
    if not price:
        return 0
    return int(float(price.lstrip("$")) * 1_000_000)


async def _verify_session(
    request: Request,
    tool_def: ToolDef,
    session_manager,
) -> MCPAuthContext:
    """Validate session token, check budget, atomically decrement.

    Parameters
    ----------
    request:
        The incoming FastAPI ``Request`` (carries ``X-TripWire-Session`` header).
    tool_def:
        The MCP tool being invoked (carries pricing info).
    session_manager:
        A ``SessionManager`` instance for session lifecycle operations.

    Returns
    -------
    MCPAuthContext
        Populated auth context for the SESSION tier.

    Raises
    ------
    HTTPException
        On missing header, session not found, expired, or insufficient budget.
    """
    from tripwire.session.manager import (
        InsufficientBudget,
        SessionExpired,
        SessionNotFound,
    )

    session_id = request.headers.get("X-TripWire-Session")
    if not session_id:
        raise HTTPException(
            status_code=401,
            detail="Missing X-TripWire-Session header",
        )

    cost = _price_to_smallest_units(tool_def.price)

    try:
        session_data = await session_manager.validate_and_decrement(session_id, cost)
    except SessionNotFound:
        raise HTTPException(status_code=401, detail="Session not found")
    except SessionExpired:
        raise HTTPException(status_code=401, detail="Session expired")
    except InsufficientBudget:
        raise HTTPException(
            status_code=402,
            detail="Insufficient session budget",
        )

    return MCPAuthContext(
        auth_tier=AuthTier.SESSION,
        agent_address=session_data.wallet_address,
        reputation_score=session_data.reputation_score,
        payment_verified=True,
        payer_address=session_data.wallet_address,
        session_id=session_id,
        budget_remaining=session_data.budget_remaining,
    )


# ---------------------------------------------------------------------------
# Public API — build_auth_context (PUBLIC + SIWX tiers only)
# ---------------------------------------------------------------------------


async def build_auth_context(
    request: Request,
    tool_def: ToolDef,
    identity_resolver: Any,
) -> MCPAuthContext:
    """Build an MCPAuthContext for *tool_def* based on its AuthTier.

    Handles PUBLIC and SIWX tiers.  X402 tools are handled by
    ``TripWirePaymentHooks`` + ``x402_tool_executor()`` instead.

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
            chain_id = caip2_to_chain_id(tool_def.network)
            identity = await identity_resolver.resolve(wallet_address, chain_id)
            if identity is not None:
                reputation_score = identity.reputation_score

        # Reputation gating is handled by the server after context is built
        return MCPAuthContext(
            auth_tier=AuthTier.SIWX,
            agent_address=wallet_address,
            identity=identity,
            reputation_score=reputation_score,
        )

    raise HTTPException(status_code=500, detail=f"Unknown auth tier: {tool_def.auth_tier}")


# ---------------------------------------------------------------------------
# TripWirePaymentHooks — x402 SDK payment wrapper hooks
# ---------------------------------------------------------------------------


@dataclass
class PaymentContext:
    """Mutable context threaded through the x402 payment lifecycle."""

    payment_header: str = ""
    payment_hash: str = ""
    dedup_key: str = ""
    payer_address: str = ""
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    auth_context: MCPAuthContext | None = None
    execution_start: float = 0.0
    result: dict | None = None
    has_error: bool = False


class TripWirePaymentHooks:
    """x402 SDK payment wrapper hooks — injects identity, reputation, rate limiting, audit."""

    def __init__(
        self,
        tool_def: ToolDef,
        identity_resolver: Any,
        audit_logger: AuditLogger,
        redis_getter: Callable = get_redis,
    ) -> None:
        self._tool_def = tool_def
        self._identity_resolver = identity_resolver
        self._audit_logger = audit_logger
        self._get_redis = redis_getter

    async def before_execution(self, payment_context: PaymentContext) -> MCPAuthContext:
        """After verify, before tool execution.

        1. Replay protection (Redis SET NX)
        2. Extract payer address (already set on payment_context by executor)
        3. ERC-8004 identity resolution
        4. Reputation gating
        5. Rate limiting

        Returns
        -------
        MCPAuthContext
            Fully populated auth context for the X402 tier.

        Raises
        ------
        HTTPException
            On replay, reputation gate, or rate limit violations.
        """
        r = self._get_redis()
        tool_def = self._tool_def

        # 1. Replay protection — atomic claim of payment proof
        payment_hash = hashlib.sha256(payment_context.payment_header.encode()).hexdigest()
        dedup_key = f"x402:payment:{payment_hash}:{tool_def.name}"
        payment_context.payment_hash = payment_hash
        payment_context.dedup_key = dedup_key

        was_set = await r.set(dedup_key, "1", ex=86400, nx=True)  # 24h TTL
        if not was_set:
            logger.warning(
                "mcp_x402_payment_replay",
                tool=tool_def.name,
                payment_hash=payment_hash,
            )
            raise HTTPException(status_code=402, detail="Payment already used")

        # 2. Payer address already extracted by the executor from verify result
        payer_address = payment_context.payer_address

        # 3. ERC-8004 identity resolution (multi-chain aware)
        identity = None
        reputation_score = 0.0
        if self._identity_resolver is not None and payer_address:
            chain_id = caip2_to_chain_id(tool_def.network)
            identity = await self._identity_resolver.resolve(payer_address, chain_id)
            if identity is not None:
                reputation_score = identity.reputation_score

        # Build auth context
        ctx = MCPAuthContext(
            auth_tier=AuthTier.X402,
            agent_address=payer_address or None,
            identity=identity,
            reputation_score=reputation_score,
            payment_verified=True,
            payer_address=payer_address or None,
        )
        payment_context.auth_context = ctx

        # 4. Reputation gating
        if tool_def.min_reputation > 0:
            if ctx.reputation_score < tool_def.min_reputation:
                # Release dedup key so they can retry after building reputation
                await r.delete(dedup_key)
                logger.warning(
                    "mcp_reputation_gate_blocked",
                    agent=ctx.agent_address,
                    tool=tool_def.name,
                    reputation=ctx.reputation_score,
                    required=tool_def.min_reputation,
                )
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Reputation too low: {ctx.reputation_score:.1f} "
                        f"< {tool_def.min_reputation:.1f}"
                    ),
                )

        # 5. Per-address rate limiting
        if ctx.agent_address:
            try:
                rate_key = f"mcp:rate:{ctx.agent_address}"
                current = await r.incr(rate_key)
                if current == 1:
                    await r.expire(rate_key, 60)  # 60-second window
                if current > 60:  # 60 calls/minute per address
                    logger.warning(
                        "mcp_rate_limited",
                        agent=ctx.agent_address,
                        tool=tool_def.name,
                        count=current,
                    )
                    raise HTTPException(
                        status_code=429,
                        detail="Rate limit exceeded: max 60 tool calls per minute",
                    )
            except HTTPException as exc:
                if exc.status_code == 429:
                    # Clean up dedup key — caller should be able to retry after rate limit window
                    try:
                        await r.delete(dedup_key)
                    except Exception:
                        pass
                raise
            except Exception:
                # Redis down — fail open for rate limiting (payment already verified)
                logger.warning("mcp_rate_limit_redis_unavailable")

        logger.info(
            "mcp_x402_payment_verified",
            tool=tool_def.name,
            payer=payer_address,
            price=tool_def.price,
            payment_hash=payment_hash,
        )

        return ctx

    async def after_execution(
        self,
        payment_context: PaymentContext,
        result: dict,
        request: Request,
    ) -> None:
        """After tool execution, before settlement.

        1. Audit logging
        2. Check if result has error -> signal to skip settlement
        """
        payment_context.result = result
        payment_context.has_error = "error" in result

        ctx = payment_context.auth_context
        latency_ms = int((time.perf_counter() - payment_context.execution_start) * 1000)

        fire_and_forget(self._audit_logger.log(
            action=f"mcp.tools.{payment_context.tool_name}",
            actor=ctx.agent_address or "anonymous",
            resource_type="mcp_tool",
            resource_id=payment_context.tool_name,
            details={
                "arguments": payment_context.tool_args,
                "auth_tier": ctx.auth_tier.value,
                "payment_verified": ctx.payment_verified,
                "success": not payment_context.has_error,
                "execution_latency_ms": latency_ms,
            },
            ip_address=(
                request.client.host if request.client else None
            ),
        ))

        logger.info(
            "mcp_tool_call",
            tool=payment_context.tool_name,
            agent=ctx.agent_address,
            auth_tier=ctx.auth_tier.value,
            success=not payment_context.has_error,
        )

    async def on_settlement_success(self, payment_context: PaymentContext) -> None:
        """Log successful settlement."""
        logger.info(
            "mcp_x402_payment_settled",
            tool=payment_context.tool_name,
            payer=payment_context.payer_address,
        )

    async def on_settlement_failure(
        self,
        payment_context: PaymentContext,
        error: Exception,
    ) -> None:
        """Clean up dedup key, signal result withholding.

        When settlement fails we must not return the tool result (prevents
        free service via settlement manipulation).  The executor checks
        ``payment_context.has_error`` after this hook and returns an error
        response.
        """
        logger.error(
            "mcp_x402_settle_failed",
            tool=payment_context.tool_name,
            agent=payment_context.payer_address,
            error=str(error),
        )

        # Best-effort cleanup of dedup key so the payer can retry
        try:
            r = self._get_redis()
            await r.delete(payment_context.dedup_key)
        except Exception:
            pass  # Best-effort cleanup

        # Mark as error so the executor knows to withhold the result
        payment_context.has_error = True


# ---------------------------------------------------------------------------
# x402_tool_executor — orchestrates the full x402 payment lifecycle
# ---------------------------------------------------------------------------


async def x402_tool_executor(
    request: Request,
    tool_def: ToolDef,
    tool_handler: Callable[..., Coroutine[Any, Any, dict]],
    tool_args: dict,
    hooks: TripWirePaymentHooks,
    repos: dict,
) -> dict:
    """Orchestrate the x402 payment lifecycle for a single tool call.

    Flow:
    1. Extract PAYMENT-SIGNATURE header
    2. ``x402ResourceServer.verify()``
    3. ``hooks.before_execution()`` — replay protection, identity, reputation, rate limit
    4. Execute tool handler
    5. ``hooks.after_execution()`` — audit, error detection
    6. If no error: ``x402ResourceServer.settle()``
    7. ``hooks.on_settlement_success()`` or ``hooks.on_settlement_failure()``
    8. Return result dict (or raise)

    Returns
    -------
    dict
        A dict with either:
        - ``{"result": <tool_result>}`` on success
        - ``{"error": <message>, "code": <json_rpc_code>}`` on failure

    The caller (server.py) is responsible for wrapping this into
    the JSON-RPC envelope.
    """
    # 1. Extract PAYMENT-SIGNATURE header
    payment_header = request.headers.get("PAYMENT-SIGNATURE")
    if not payment_header:
        raise HTTPException(
            status_code=402,
            detail="Payment required. Include a PAYMENT-SIGNATURE header.",
        )

    # Build payment context
    pctx = PaymentContext(
        payment_header=payment_header,
        tool_name=tool_def.name,
        tool_args=tool_args,
    )

    # 2. x402ResourceServer.verify()
    server = _get_x402_server()

    payment_option = PaymentOption(
        scheme="exact",
        price=tool_def.price or "$0.00",
        network=tool_def.network,
        pay_to=settings.tripwire_treasury_address,
    )

    try:
        verify_result = await server.verify(payment_header, payment_option)
    except Exception as exc:
        logger.warning("mcp_x402_verify_failed", error=str(exc), tool=tool_def.name)
        raise HTTPException(status_code=402, detail="Payment verification failed")

    if not verify_result.valid:
        raise HTTPException(status_code=402, detail="Payment verification failed")

    # Extract payer address from verify result
    payer = getattr(verify_result, "payer", None) or getattr(verify_result, "from_address", None)
    pctx.payer_address = str(payer) if payer else ""

    # 3. hooks.before_execution() — replay protection, identity, reputation, rate limit
    #    May raise HTTPException on replay, reputation gate, or rate limit violations.
    #    On verify failure, the dedup key hasn't been set yet, so no cleanup needed.
    ctx = await hooks.before_execution(pctx)

    # Require agent_address for X402 tools
    if not ctx.agent_address:
        raise HTTPException(
            status_code=401,
            detail="Could not determine agent address from payment",
        )

    # 4. Execute tool handler
    pctx.execution_start = time.perf_counter()
    try:
        tool_result = await tool_handler(tool_args, ctx, repos)
    except Exception as exc:
        # Clean up dedup key so caller can retry with the same payment proof
        try:
            r = get_redis()
            if r and pctx.dedup_key:
                await r.delete(pctx.dedup_key)
        except Exception:
            pass  # best-effort cleanup
        logger.exception(
            "mcp_tool_call_failed",
            tool=tool_def.name,
            agent=ctx.agent_address,
            auth_tier=ctx.auth_tier.value,
            error=str(exc),
        )
        return {"error": "Tool execution failed", "code": "INTERNAL_ERROR"}

    # 5. hooks.after_execution() — audit, error detection
    await hooks.after_execution(pctx, tool_result, request)

    # 6. Settle if no error in tool result
    if pctx.has_error:
        # Tool returned a handled error (e.g. NOT_FOUND, validation error).
        # Don't settle, but DO clean up the dedup key so the caller can
        # retry with the same payment proof.
        try:
            r = get_redis()
            if r and pctx.dedup_key:
                await r.delete(pctx.dedup_key)
        except Exception:
            pass  # best-effort cleanup
        return {"result": tool_result}

    try:
        await server.settle(payment_header)
    except Exception as exc:
        # 7a. Settlement failure
        await hooks.on_settlement_failure(pctx, exc)
        # Return error — do NOT return tool result (prevents free service)
        return {
            "error": "Payment settlement failed -- tool result withheld",
            "code": "SETTLEMENT_FAILED",
        }
    # 7b. Settlement success
    await hooks.on_settlement_success(pctx)

    # 8. Return result
    return {"result": tool_result}
