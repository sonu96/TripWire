"""MCP JSON-RPC server with 3-tier authentication.

Authentication tiers:
- PUBLIC: No auth needed (initialize, tools/list)
- SIWX: Wallet signature via SIWE (free tools)
- X402: Per-call payment via x402 protocol (paid tools)
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from tripwire.db.client import get_supabase_client
from tripwire.db.repositories.endpoints import EndpointRepository
from tripwire.db.repositories.events import EventRepository
from tripwire.db.repositories.triggers import (
    TriggerRepository,
    TriggerTemplateRepository,
)
from tripwire.observability.audit import AuditLogger, fire_and_forget

from tripwire.config.settings import settings
from tripwire.mcp.types import AuthTier, MCPAuthContext, ToolDef
from tripwire.mcp.auth import (
    build_auth_context,
    TripWirePaymentHooks,
    x402_tool_executor,
    _verify_session,
    _price_to_smallest_units,
)
from tripwire.mcp import tools as tool_handlers

logger = structlog.get_logger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"

# ── Tool definition registry ────────────────────────────────

TOOLS: dict[str, ToolDef] = {}


def _register(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    handler,
    auth_tier: AuthTier = AuthTier.SIWX,
    price: str | None = None,
    min_reputation: float = 0.0,
    product: str = "both",
) -> None:
    TOOLS[name] = ToolDef(
        name=name,
        description=description,
        input_schema=input_schema,
        handler=handler,
        auth_tier=auth_tier,
        price=price,
        min_reputation=min_reputation,
        product=product,
    )


# ── Register all 12 tools ───────────────────────────────────

_register(
    name="register_endpoint",
    handler=tool_handlers.register_endpoint,
    description=(
        "Create a webhook endpoint. Returns endpoint_id and webhook_secret. "
        "Use create_trigger or activate_template separately to add event triggers."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Your webhook/callback URL",
            },
            "mode": {
                "type": "string",
                "enum": ["notify", "execute"],
                "description": "Delivery mode: notify (Supabase Realtime) or execute (webhook POST)",
                "default": "execute",
            },
            "chains": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Chain IDs to monitor (default: [8453] for Base)",
                "default": [8453],
            },
            "recipient": {
                "type": "string",
                "description": "Recipient address to watch (defaults to your agent address)",
            },
            "policies": {
                "type": "object",
                "description": "Endpoint policies (min_amount, max_amount, allowed_senders, etc.)",
            },
        },
        "required": ["url"],
    },
    auth_tier=AuthTier.X402,
    price="$0.003",
    min_reputation=10.0,
    product="both",
)

_register(
    name="register_middleware",
    description=(
        "(Deprecated -- use register_endpoint instead) "
        "Register TripWire as middleware for your API. Creates an endpoint "
        "and triggers from template slugs or custom definitions."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Your webhook/callback URL",
            },
            "mode": {
                "type": "string",
                "enum": ["notify", "execute"],
                "description": "Delivery mode: notify (Supabase Realtime) or execute (webhook POST)",
                "default": "execute",
            },
            "chains": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Chain IDs to monitor (default: [8453] for Base)",
                "default": [8453],
            },
            "recipient": {
                "type": "string",
                "description": "Recipient address to watch (defaults to your agent address)",
            },
            "policies": {
                "type": "object",
                "description": "Endpoint policies (min_amount, max_amount, allowed_senders, etc.)",
            },
            "template_slugs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Template slugs to instantiate as triggers",
            },
            "custom_triggers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "event_signature": {"type": "string"},
                        "name": {"type": "string"},
                        "abi": {"type": "array"},
                        "contract_address": {"type": "string"},
                        "chain_ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "filter_rules": {"type": "array"},
                        "webhook_event_type": {"type": "string"},
                    },
                    "required": ["event_signature"],
                },
                "description": "Custom trigger definitions",
            },
        },
        "required": ["url"],
    },
    handler=tool_handlers.register_middleware,
    auth_tier=AuthTier.X402,
    price="$0.003",
    min_reputation=10.0,
    product="keeper",
)

_register(
    name="create_trigger",
    description="Create a custom trigger for an existing endpoint.",
    input_schema={
        "type": "object",
        "properties": {
            "endpoint_id": {"type": "string", "description": "Target endpoint ID"},
            "event_signature": {
                "type": "string",
                "description": "Solidity event signature (e.g. Transfer(address,address,uint256))",
            },
            "name": {"type": "string", "description": "Human-readable trigger name"},
            "abi": {"type": "array", "description": "ABI fragment for the event"},
            "contract_address": {
                "type": "string",
                "description": "Contract to watch (null for any)",
            },
            "chain_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Chain IDs to monitor",
            },
            "filter_rules": {
                "type": "array",
                "description": "Filter predicates on decoded event fields",
            },
            "webhook_event_type": {"type": "string"},
            "reputation_threshold": {"type": "number"},
        },
        "required": ["endpoint_id", "event_signature"],
    },
    handler=tool_handlers.create_trigger,
    auth_tier=AuthTier.X402,
    price="$0.003",
    min_reputation=10.0,
    product="pulse",
)

_register(
    name="list_triggers",
    description="List your active triggers.",
    input_schema={
        "type": "object",
        "properties": {
            "active_only": {
                "type": "boolean",
                "description": "Only return active triggers",
                "default": True,
            },
        },
    },
    handler=tool_handlers.list_triggers,
    auth_tier=AuthTier.SIWX,
    product="pulse",
)

_register(
    name="delete_trigger",
    description="Deactivate a trigger (soft delete).",
    input_schema={
        "type": "object",
        "properties": {
            "trigger_id": {"type": "string", "description": "Trigger ID to deactivate"},
        },
        "required": ["trigger_id"],
    },
    handler=tool_handlers.delete_trigger,
    auth_tier=AuthTier.SIWX,
    product="pulse",
)

_register(
    name="list_templates",
    description="Browse available trigger templates from the Bazaar.",
    input_schema={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Filter by category (e.g. 'defi', 'payments', 'nft')",
            },
        },
    },
    handler=tool_handlers.list_templates,
    auth_tier=AuthTier.SIWX,
    product="pulse",
)

_register(
    name="activate_template",
    description="Instantiate a Bazaar template with custom params for an endpoint.",
    input_schema={
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Template slug"},
            "endpoint_id": {"type": "string", "description": "Target endpoint ID"},
            "params": {
                "type": "object",
                "description": "Custom parameters (chain_ids, contract_address, filter_rules)",
            },
        },
        "required": ["slug", "endpoint_id"],
    },
    handler=tool_handlers.activate_template,
    auth_tier=AuthTier.X402,
    price="$0.001",
    min_reputation=10.0,
    product="pulse",
)

_register(
    name="get_trigger_status",
    description="Check trigger health and event count.",
    input_schema={
        "type": "object",
        "properties": {
            "trigger_id": {"type": "string", "description": "Trigger ID to check"},
        },
        "required": ["trigger_id"],
    },
    handler=tool_handlers.get_trigger_status,
    auth_tier=AuthTier.SIWX,
    product="pulse",
)

_register(
    name="search_events",
    description="Query recent events for your triggers and endpoints.",
    input_schema={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max results (1-100, default 50)",
                "default": 50,
            },
            "status": {
                "type": "string",
                "description": "Filter by event status (e.g. 'confirmed', 'pending')",
            },
            "chain_id": {
                "type": "integer",
                "description": "Filter by chain ID",
            },
        },
    },
    handler=tool_handlers.search_events,
    auth_tier=AuthTier.SIWX,
)

_register(
    name="fetch_abi",
    handler=tool_handlers.fetch_abi,
    description=(
        "Fetch the ABI for any smart contract and list its events. "
        "Use this to discover what events a contract emits before creating a trigger."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "contract_address": {
                "type": "string",
                "description": "Contract address (0x...)",
            },
            "chain": {
                "type": "string",
                "enum": ["base", "ethereum", "arbitrum"],
                "description": "Chain name",
            },
        },
        "required": ["contract_address", "chain"],
    },
    auth_tier=AuthTier.SIWX,
    product="pulse",
)

_register(
    name="list_pools",
    handler=tool_handlers.list_pools,
    description=(
        "List popular pools for a DeFi protocol. Returns pool addresses and available events. "
        "Currently supports: aerodrome, aave-v3, uniswap-v3."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "protocol": {
                "type": "string",
                "description": "Protocol name (aerodrome, aave-v3, uniswap-v3)",
            },
            "chain": {
                "type": "string",
                "enum": ["base", "ethereum", "arbitrum"],
                "default": "base",
                "description": "Chain name",
            },
            "limit": {
                "type": "integer",
                "default": 10,
                "description": "Max pools to return",
            },
        },
        "required": ["protocol"],
    },
    auth_tier=AuthTier.SIWX,
    product="pulse",
)

_register(
    name="validate_trigger",
    handler=tool_handlers.validate_trigger,
    description=(
        "Validate a trigger configuration before creating it. "
        "Checks event signature format, contract address, chain support, and filter rules."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "event_signature": {
                "type": "string",
                "description": "Solidity event signature, e.g. Swap(address,address,int256,int256,uint160,uint128,int24)",
            },
            "contract_address": {
                "type": "string",
                "description": "Contract address (0x...)",
            },
            "chain_id": {
                "type": "integer",
                "description": "Chain ID (8453=Base, 1=Ethereum, 42161=Arbitrum)",
            },
            "filter_rules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "op": {"type": "string"},
                        "value": {"type": "string"},
                    },
                },
                "description": "Optional filter rules to validate",
            },
            "event_abi": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string"},
                        "indexed": {"type": "boolean"},
                    },
                },
                "description": "Optional event ABI inputs (from fetch_abi) for field name validation",
            },
        },
        "required": ["event_signature", "contract_address", "chain_id"],
    },
    auth_tier=AuthTier.SIWX,
    product="pulse",
)


# ── JSON-RPC helpers ─────────────────────────────────────────


def _jsonrpc_success(id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _jsonrpc_error(id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id, "error": err}


# Standard JSON-RPC error codes
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

# Application error codes
_AUTH_REQUIRED = -32000
_REPUTATION_TOO_LOW = -32001
_PAYMENT_REQUIRED = -32002
_RATE_LIMITED = -32003


# ── MCP FastAPI sub-app ──────────────────────────────────────


def create_mcp_app() -> FastAPI:
    """Build the MCP sub-application."""

    mcp_app = FastAPI(
        title="TripWire MCP",
        description="Model Context Protocol server for AI agent integration",
        docs_url=None,
        redoc_url=None,
    )

    @mcp_app.post("/")
    async def mcp_endpoint(request: Request):
        """MCP JSON-RPC endpoint."""

        # Parse JSON body
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                content=_jsonrpc_error(None, _PARSE_ERROR, "Parse error"),
                status_code=200,
            )

        jsonrpc = body.get("jsonrpc")
        method = body.get("method")
        params = body.get("params", {})
        req_id = body.get("id")

        if jsonrpc != "2.0" or not method:
            return JSONResponse(
                content=_jsonrpc_error(req_id, _INVALID_REQUEST, "Invalid Request"),
                status_code=200,
            )

        # ── Handle initialize (PUBLIC) ──────────────────────
        if method == "initialize":
            return JSONResponse(
                content=_jsonrpc_success(req_id, {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": "tripwire-mcp",
                        "version": "1.0.0",
                    },
                }),
                status_code=200,
            )

        # ── Handle tools/list (PUBLIC) ──────────────────────
        if method == "tools/list":
            # Filter tools by active product mode.
            # td.product is a string that matches ProductMode enum values
            # ("pulse", "keeper", "both"). Since ProductMode(str, Enum)
            # compares equal to its string value, this works with both.
            visible_tools = {
                name: td for name, td in TOOLS.items()
                if td.product == "both"
                or (td.product == "pulse" and settings.is_pulse)
                or (td.product == "keeper" and settings.is_keeper)
            }
            tool_list = []
            for tool_def in visible_tools.values():
                entry = {
                    "name": tool_def.name,
                    "description": tool_def.description,
                    "inputSchema": tool_def.input_schema,
                }
                # Include pricing metadata so clients know which tools cost money
                if tool_def.auth_tier == AuthTier.X402 and tool_def.price:
                    entry["x-tripwire-price"] = tool_def.price
                    entry["x-tripwire-network"] = tool_def.network
                # Include product tag so clients know which product the tool belongs to
                entry["x-tripwire-product"] = tool_def.product
                tool_list.append(entry)
            return JSONResponse(
                content=_jsonrpc_success(req_id, {"tools": tool_list}),
                status_code=200,
            )

        # ── Handle tools/call ───────────────────────────────
        if method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})

            if not tool_name or tool_name not in TOOLS:
                return JSONResponse(
                    content=_jsonrpc_error(
                        req_id,
                        _METHOD_NOT_FOUND,
                        f"Unknown tool: {tool_name}",
                    ),
                    status_code=200,
                )

            tool_def = TOOLS[tool_name]
            parent_app = request.app.state.parent_app
            identity_resolver = parent_app.state.identity_resolver
            audit_logger: AuditLogger = parent_app.state.audit_logger

            # Build repos dict from parent app state
            supabase = parent_app.state.supabase
            repos = {
                "supabase": supabase,
                "endpoint_repo": EndpointRepository(supabase),
                "trigger_repo": TriggerRepository(supabase),
                "template_repo": TriggerTemplateRepository(supabase),
                "event_repo": EventRepository(supabase),
                "webhook_provider": parent_app.state.webhook_provider,
            }

            # ── SESSION path: pre-funded session budget ──────────
            session_header = request.headers.get("X-TripWire-Session")
            if session_header and settings.session_enabled:
                session_manager = getattr(parent_app.state, "session_manager", None)
                if session_manager is not None:
                    return await _handle_session_tool_call(
                        request=request,
                        req_id=req_id,
                        tool_def=tool_def,
                        tool_name=tool_name,
                        tool_args=tool_args,
                        identity_resolver=identity_resolver,
                        audit_logger=audit_logger,
                        session_manager=session_manager,
                        repos=repos,
                    )

            # ── X402 path: delegate to payment hooks + executor ──
            if tool_def.auth_tier == AuthTier.X402:
                return await _handle_x402_tool_call(
                    request=request,
                    req_id=req_id,
                    tool_def=tool_def,
                    tool_args=tool_args,
                    identity_resolver=identity_resolver,
                    audit_logger=audit_logger,
                    repos=repos,
                )

            # ── SIWX / PUBLIC path: existing logic ───────────────
            return await _handle_siwx_public_tool_call(
                request=request,
                req_id=req_id,
                tool_def=tool_def,
                tool_name=tool_name,
                tool_args=tool_args,
                identity_resolver=identity_resolver,
                audit_logger=audit_logger,
                repos=repos,
            )

        # ── Unknown method ──────────────────────────────────
        return JSONResponse(
            content=_jsonrpc_error(
                req_id, _METHOD_NOT_FOUND, f"Method not found: {method}"
            ),
            status_code=200,
        )

    return mcp_app


# ── X402 tool call handler (hooks pattern) ─────────────────────


async def _handle_x402_tool_call(
    request: Request,
    req_id: Any,
    tool_def: ToolDef,
    tool_args: dict,
    identity_resolver: Any,
    audit_logger: AuditLogger,
    repos: dict,
) -> JSONResponse:
    """Handle an X402 tool call using TripWirePaymentHooks + x402_tool_executor."""
    hooks = TripWirePaymentHooks(
        tool_def=tool_def,
        identity_resolver=identity_resolver,
        audit_logger=audit_logger,
    )

    try:
        outcome = await x402_tool_executor(
            request=request,
            tool_def=tool_def,
            tool_handler=tool_def.handler,
            tool_args=tool_args,
            hooks=hooks,
            repos=repos,
        )
    except HTTPException as exc:
        # Map HTTP status codes to JSON-RPC error codes
        if exc.status_code == 402:
            code = _PAYMENT_REQUIRED
        elif exc.status_code == 403:
            code = _REPUTATION_TOO_LOW
        elif exc.status_code == 429:
            code = _RATE_LIMITED
        elif exc.status_code == 401:
            code = _AUTH_REQUIRED
        else:
            code = _AUTH_REQUIRED
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)

        # Include reputation data for reputation gate errors
        data = None
        if exc.status_code == 403 and "Reputation too low" in detail:
            # Parse reputation values from the detail message
            data = {"detail": detail}

        return JSONResponse(
            content=_jsonrpc_error(req_id, code, detail, data=data),
            status_code=200,
        )
    except Exception as exc:
        logger.exception(
            "mcp_x402_unexpected_error",
            tool=tool_def.name,
            error=str(exc),
        )
        return JSONResponse(
            content=_jsonrpc_error(
                req_id,
                _INTERNAL_ERROR,
                "Payment service unavailable",
            ),
            status_code=200,
        )

    # x402_tool_executor returns {"result": ...} or {"error": ..., "code": ...}
    if "error" in outcome:
        error_msg = outcome["error"]
        error_code = outcome.get("code", "INTERNAL_ERROR")

        # Map known error codes to JSON-RPC error codes
        if error_code == "SETTLEMENT_FAILED":
            jsonrpc_code = _PAYMENT_REQUIRED
        else:
            jsonrpc_code = _INTERNAL_ERROR

        return JSONResponse(
            content=_jsonrpc_error(req_id, jsonrpc_code, error_msg),
            status_code=200,
        )

    # Success — tool result is in outcome["result"]
    tool_result = outcome["result"]
    is_error = "error" in tool_result
    return JSONResponse(
        content=_jsonrpc_success(req_id, {
            "content": [
                {
                    "type": "text",
                    "text": _serialize_result(tool_result),
                }
            ],
            "isError": is_error,
        }),
        status_code=200,
    )


# ── SESSION tool call handler ───────────────────────────────────


async def _handle_session_tool_call(
    request: Request,
    req_id: Any,
    tool_def: ToolDef,
    tool_name: str,
    tool_args: dict,
    identity_resolver: Any,
    audit_logger: AuditLogger,
    session_manager: Any,
    repos: dict,
) -> JSONResponse:
    """Handle a tool call authenticated via a pre-funded session.

    Flow:
    1. _verify_session() — validates session, checks budget, atomically decrements
    2. Reputation gating (refund budget on gate failure)
    3. Rate limiting
    4. Execute tool handler
    5. Audit log with session context
    6. Return result with session metadata
    """
    # 1. Verify session + decrement budget
    try:
        ctx: MCPAuthContext = await _verify_session(request, tool_def, session_manager)
    except HTTPException as exc:
        if exc.status_code == 402:
            code = _PAYMENT_REQUIRED
        elif exc.status_code == 403:
            code = _REPUTATION_TOO_LOW
        else:
            code = _AUTH_REQUIRED
        return JSONResponse(
            content=_jsonrpc_error(
                req_id,
                code,
                exc.detail if isinstance(exc.detail, str) else str(exc.detail),
            ),
            status_code=200,
        )
    except Exception as exc:
        logger.exception(
            "mcp_session_auth_error",
            tool=tool_name,
            error=str(exc),
        )
        return JSONResponse(
            content=_jsonrpc_error(
                req_id, _INTERNAL_ERROR, "Session validation failed"
            ),
            status_code=200,
        )

    # 2. Reputation gating (refund on failure)
    if tool_def.min_reputation > 0:
        if ctx.reputation_score < tool_def.min_reputation:
            # Refund the budget since we already decremented
            cost = _price_to_smallest_units(tool_def.price)
            if cost > 0:
                try:
                    await session_manager.refund(ctx.session_id, cost)
                except Exception:
                    logger.warning(
                        "session_refund_failed_on_reputation_gate",
                        session_id=ctx.session_id,
                        cost=cost,
                    )

            logger.warning(
                "mcp_reputation_gate_blocked",
                agent=ctx.agent_address,
                tool=tool_name,
                reputation=ctx.reputation_score,
                required=tool_def.min_reputation,
            )
            return JSONResponse(
                content=_jsonrpc_error(
                    req_id,
                    _REPUTATION_TOO_LOW,
                    f"Reputation too low: {ctx.reputation_score:.1f} < {tool_def.min_reputation:.1f}",
                    data={
                        "reputation": ctx.reputation_score,
                        "required": tool_def.min_reputation,
                    },
                ),
                status_code=200,
            )

    # 3. Per-address rate limiting
    if ctx.agent_address:
        try:
            from tripwire.api.redis import get_redis
            r = get_redis()
            rate_key = f"mcp:rate:{ctx.agent_address}"
            current = await r.incr(rate_key)
            if current == 1:
                await r.expire(rate_key, 60)
            if current > 60:
                logger.warning(
                    "mcp_rate_limited",
                    agent=ctx.agent_address,
                    tool=tool_name,
                    count=current,
                )
                # Refund budget on rate limit
                cost = _price_to_smallest_units(tool_def.price)
                if cost > 0:
                    try:
                        await session_manager.refund(ctx.session_id, cost)
                    except Exception:
                        pass
                return JSONResponse(
                    content=_jsonrpc_error(
                        req_id,
                        _RATE_LIMITED,
                        "Rate limit exceeded: max 60 tool calls per minute",
                    ),
                    status_code=200,
                )
        except Exception:
            logger.warning("mcp_rate_limit_redis_unavailable")

    # 4. Execute tool handler
    _t0 = time.perf_counter()
    try:
        result = await tool_def.handler(tool_args, ctx, repos)
    except Exception as exc:
        logger.exception(
            "mcp_tool_call_failed",
            tool=tool_name,
            agent=ctx.agent_address,
            auth_tier=ctx.auth_tier.value,
            session_id=ctx.session_id,
            error=str(exc),
        )
        return JSONResponse(
            content=_jsonrpc_error(
                req_id, _INTERNAL_ERROR, "Tool execution failed"
            ),
            status_code=200,
        )

    # 5. Audit log with session context
    _latency_ms = int((time.perf_counter() - _t0) * 1000)
    fire_and_forget(audit_logger.log(
        action=f"mcp.tools.{tool_name}",
        actor=ctx.agent_address or "anonymous",
        resource_type="mcp_tool",
        resource_id=tool_name,
        details={
            "arguments": tool_args,
            "auth_tier": ctx.auth_tier.value,
            "payment_verified": ctx.payment_verified,
            "session_id": ctx.session_id,
            "budget_remaining": ctx.budget_remaining,
            "success": "error" not in result,
            "execution_latency_ms": _latency_ms,
        },
        ip_address=(
            request.client.host if request.client else None
        ),
    ))

    logger.info(
        "mcp_tool_call",
        tool=tool_name,
        agent=ctx.agent_address,
        auth_tier=ctx.auth_tier.value,
        session_id=ctx.session_id,
        budget_remaining=ctx.budget_remaining,
        success="error" not in result,
    )

    # 6. Return result with session metadata
    is_error = "error" in result
    response_result = {
        "content": [
            {
                "type": "text",
                "text": _serialize_result(result),
            }
        ],
        "isError": is_error,
        "x-tripwire-session": {
            "session_id": ctx.session_id,
            "budget_remaining": ctx.budget_remaining,
        },
    }
    return JSONResponse(
        content=_jsonrpc_success(req_id, response_result),
        status_code=200,
    )


# ── SIWX / PUBLIC tool call handler (unchanged logic) ──────────


async def _handle_siwx_public_tool_call(
    request: Request,
    req_id: Any,
    tool_def: ToolDef,
    tool_name: str,
    tool_args: dict,
    identity_resolver: Any,
    audit_logger: AuditLogger,
    repos: dict,
) -> JSONResponse:
    """Handle a SIWX or PUBLIC tool call — preserves existing logic exactly."""
    # ── Authenticate via build_auth_context ────────────────
    try:
        ctx: MCPAuthContext = await build_auth_context(
            request, tool_def, identity_resolver
        )
    except HTTPException as exc:
        # Map HTTP status codes to JSON-RPC error codes
        if exc.status_code == 402:
            code = _PAYMENT_REQUIRED
        elif exc.status_code == 403:
            code = _REPUTATION_TOO_LOW
        else:
            code = _AUTH_REQUIRED
        return JSONResponse(
            content=_jsonrpc_error(
                req_id,
                code,
                exc.detail if isinstance(exc.detail, str) else str(exc.detail),
            ),
            status_code=200,
        )
    except Exception as exc:
        # Redis down, network errors, etc. — return JSON-RPC error, not HTTP 500
        logger.exception(
            "mcp_auth_unexpected_error",
            tool=tool_name,
            error=str(exc),
        )
        return JSONResponse(
            content=_jsonrpc_error(
                req_id,
                _INTERNAL_ERROR,
                "Authentication service unavailable",
            ),
            status_code=200,
        )

    # ── Require agent_address for non-PUBLIC tools ──
    if tool_def.auth_tier != AuthTier.PUBLIC and not ctx.agent_address:
        return JSONResponse(
            content=_jsonrpc_error(
                req_id,
                _AUTH_REQUIRED,
                "Could not determine agent address from authentication",
            ),
            status_code=200,
        )

    # ── Reputation gating ───────────────────────────
    if tool_def.min_reputation > 0:
        if ctx.reputation_score < tool_def.min_reputation:
            logger.warning(
                "mcp_reputation_gate_blocked",
                agent=ctx.agent_address,
                tool=tool_name,
                reputation=ctx.reputation_score,
                required=tool_def.min_reputation,
            )
            return JSONResponse(
                content=_jsonrpc_error(
                    req_id,
                    _REPUTATION_TOO_LOW,
                    f"Reputation too low: {ctx.reputation_score:.1f} < {tool_def.min_reputation:.1f}",
                    data={
                        "reputation": ctx.reputation_score,
                        "required": tool_def.min_reputation,
                    },
                ),
                status_code=200,
            )

    # ── Per-address rate limiting ────────────────────
    if ctx.agent_address:
        try:
            from tripwire.api.redis import get_redis
            r = get_redis()
            rate_key = f"mcp:rate:{ctx.agent_address}"
            current = await r.incr(rate_key)
            if current == 1:
                await r.expire(rate_key, 60)  # 60-second window
            if current > 60:  # 60 calls/minute per address
                logger.warning(
                    "mcp_rate_limited",
                    agent=ctx.agent_address,
                    tool=tool_name,
                    count=current,
                )
                return JSONResponse(
                    content=_jsonrpc_error(
                        req_id,
                        _RATE_LIMITED,
                        "Rate limit exceeded: max 60 tool calls per minute",
                    ),
                    status_code=200,
                )
        except Exception:
            # Redis down — fail open for rate limiting (auth already passed)
            logger.warning("mcp_rate_limit_redis_unavailable")

    # ── Execute the tool handler ────────────────────
    _t0 = time.perf_counter()
    try:
        result = await tool_def.handler(tool_args, ctx, repos)
    except Exception as exc:
        logger.exception(
            "mcp_tool_call_failed",
            tool=tool_name,
            agent=ctx.agent_address,
            auth_tier=ctx.auth_tier.value,
            error=str(exc),
        )
        return JSONResponse(
            content=_jsonrpc_error(
                req_id,
                _INTERNAL_ERROR,
                "Tool execution failed",
            ),
            status_code=200,
        )

    # ── Audit log ───────────────────────────────────
    _latency_ms = int((time.perf_counter() - _t0) * 1000)
    fire_and_forget(audit_logger.log(
        action=f"mcp.tools.{tool_name}",
        actor=ctx.agent_address or "anonymous",
        resource_type="mcp_tool",
        resource_id=tool_name,
        details={
            "arguments": tool_args,
            "auth_tier": ctx.auth_tier.value,
            "payment_verified": ctx.payment_verified,
            "success": "error" not in result,
            "execution_latency_ms": _latency_ms,
        },
        ip_address=(
            request.client.host if request.client else None
        ),
    ))

    logger.info(
        "mcp_tool_call",
        tool=tool_name,
        agent=ctx.agent_address,
        auth_tier=ctx.auth_tier.value,
        success="error" not in result,
    )

    # MCP tools/call returns content array
    is_error = "error" in result
    return JSONResponse(
        content=_jsonrpc_success(req_id, {
            "content": [
                {
                    "type": "text",
                    "text": _serialize_result(result),
                }
            ],
            "isError": is_error,
        }),
        status_code=200,
    )


def _serialize_result(result: dict) -> str:
    """Serialize a tool result dict to a JSON string for the MCP content response."""
    return json.dumps(result, default=str)
