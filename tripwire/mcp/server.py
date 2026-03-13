"""MCP JSON-RPC server mounted as a FastAPI sub-application.

Implements the Model Context Protocol over HTTP:
- POST /  (mounted at /mcp/) -- JSON-RPC endpoint handling:
    - initialize
    - tools/list
    - tools/call

Authentication: Bearer token containing the agent's Ethereum address (MVP).
Real SIWE verification comes later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tripwire.db.client import get_supabase_client
from tripwire.db.repositories.endpoints import EndpointRepository
from tripwire.db.repositories.events import EventRepository
from tripwire.db.repositories.triggers import (
    TriggerRepository,
    TriggerTemplateRepository,
)
from tripwire.identity.resolver import IdentityResolver
from tripwire.observability.audit import AuditLogger, fire_and_forget

from tripwire.mcp import tools as tool_handlers

logger = structlog.get_logger(__name__)

_ETH_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

# MCP protocol version
MCP_PROTOCOL_VERSION = "2024-11-05"

# ── Tool definition registry ────────────────────────────────


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Coroutine[Any, Any, dict]]
    min_reputation: float = 0.0


TOOLS: dict[str, ToolDef] = {}


def _register(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    handler: Callable[..., Coroutine[Any, Any, dict]],
    min_reputation: float = 0.0,
) -> None:
    TOOLS[name] = ToolDef(
        name=name,
        description=description,
        input_schema=input_schema,
        handler=handler,
        min_reputation=min_reputation,
    )


# ── Register all 8 tools ────────────────────────────────────

_register(
    name="register_middleware",
    description=(
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
    min_reputation=0.0,
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
    min_reputation=0.0,
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
    min_reputation=0.0,
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
    min_reputation=0.0,
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
    min_reputation=0.0,
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
    min_reputation=0.0,
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
    min_reputation=0.0,
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
    min_reputation=0.0,
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


# ── Auth extraction ──────────────────────────────────────────


def _extract_agent_address(request: Request) -> str | None:
    """Extract agent address from Authorization: Bearer <address> header.

    MVP auth -- real SIWE verification comes later.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:].strip()
    if _ETH_ADDRESS_RE.match(token):
        return token.lower()
    return None


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

        # ── Handle initialize ────────────────────────────────
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

        # ── Handle tools/list ────────────────────────────────
        if method == "tools/list":
            tool_list = []
            for tool_def in TOOLS.values():
                tool_list.append({
                    "name": tool_def.name,
                    "description": tool_def.description,
                    "inputSchema": tool_def.input_schema,
                })
            return JSONResponse(
                content=_jsonrpc_success(req_id, {"tools": tool_list}),
                status_code=200,
            )

        # ── Handle tools/call ────────────────────────────────
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

            # Auth required for all tool calls
            agent_address = _extract_agent_address(request)
            if not agent_address:
                return JSONResponse(
                    content=_jsonrpc_error(
                        req_id,
                        _AUTH_REQUIRED,
                        "Authorization required: Bearer <ethereum_address>",
                    ),
                    status_code=200,
                )

            # Reputation gating via ERC-8004 identity
            if tool_def.min_reputation > 0:
                identity_resolver: IdentityResolver = (
                    request.app.state.identity_resolver
                )
                identity = await identity_resolver.resolve(
                    agent_address, chain_id=8453
                )
                reputation = (
                    identity.reputation_score if identity else 0.0
                )
                if reputation < tool_def.min_reputation:
                    logger.warning(
                        "mcp_reputation_gate_blocked",
                        agent=agent_address,
                        tool=tool_name,
                        reputation=reputation,
                        required=tool_def.min_reputation,
                    )
                    return JSONResponse(
                        content=_jsonrpc_error(
                            req_id,
                            _REPUTATION_TOO_LOW,
                            f"Reputation too low: {reputation:.1f} < {tool_def.min_reputation:.1f}",
                            data={
                                "reputation": reputation,
                                "required": tool_def.min_reputation,
                            },
                        ),
                        status_code=200,
                    )

            # Build repos dict from parent app state
            parent_app = request.app.state.parent_app
            supabase = parent_app.state.supabase

            repos = {
                "supabase": supabase,
                "endpoint_repo": EndpointRepository(supabase),
                "trigger_repo": TriggerRepository(supabase),
                "template_repo": TriggerTemplateRepository(supabase),
                "event_repo": EventRepository(supabase),
            }

            # Execute the tool handler
            try:
                result = await tool_def.handler(tool_args, agent_address, repos)
            except Exception as exc:
                logger.exception(
                    "mcp_tool_call_failed",
                    tool=tool_name,
                    agent=agent_address,
                    error=str(exc),
                )
                return JSONResponse(
                    content=_jsonrpc_error(
                        req_id,
                        _INTERNAL_ERROR,
                        f"Tool execution failed: {str(exc)}",
                    ),
                    status_code=200,
                )

            # Audit log every tool call
            audit_logger: AuditLogger = parent_app.state.audit_logger
            fire_and_forget(audit_logger.log(
                action=f"mcp.tools.{tool_name}",
                actor=agent_address,
                resource_type="mcp_tool",
                resource_id=tool_name,
                details={
                    "arguments": tool_args,
                    "success": "error" not in result,
                },
                ip_address=(
                    request.client.host if request.client else None
                ),
            ))

            logger.info(
                "mcp_tool_call",
                tool=tool_name,
                agent=agent_address,
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

        # ── Unknown method ───────────────────────────────────
        return JSONResponse(
            content=_jsonrpc_error(
                req_id, _METHOD_NOT_FOUND, f"Method not found: {method}"
            ),
            status_code=200,
        )

    return mcp_app


def _serialize_result(result: dict) -> str:
    """Serialize a tool result dict to a JSON string for the MCP content response."""
    import json

    return json.dumps(result, default=str)
