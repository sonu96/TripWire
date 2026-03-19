"""x402 service discovery and TWSS-1 skill spec for Bazaar discovery."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from tripwire.config.settings import settings
from tripwire.mcp.types import AuthTier, ToolDef

router = APIRouter()


TWSS_VERSION = "1.0.0-draft"


@router.get("/.well-known/tripwire-skill-spec.json")
async def skill_spec():
    """TWSS-1: TripWire Skill Spec — execution-aware skill schema."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://tripwire.dev/schemas/twss-1/v1",
        "title": "TWSS-1 Skill Output",
        "version": TWSS_VERSION,
        "spec_url": "https://tripwire.dev/docs/SKILL-SPEC",

        "execution_states": ["provisional", "confirmed", "finalized", "reorged"],
        "trust_sources": ["facilitator", "onchain"],

        "three_layer_gating": {
            "layer_1_payment": {
                "description": "Can pay? — x402 payment or event payment metadata",
                "fields": ["require_payment", "payment_token", "min_payment_amount"],
            },
            "layer_2_identity": {
                "description": "Can trust? — ERC-8004 reputation + agent class",
                "fields": ["min_reputation", "required_agent_class"],
            },
            "layer_3_execution": {
                "description": "Is safe? — Execution state + finality depth",
                "fields": ["execution.state", "execution.safe_to_execute", "execution.finality"],
            },
        },

        "two_phase_model": {
            "prepare": {
                "trigger": "provisional",
                "safe_to_execute": False,
                "agent_action": "Optimistic UI, hold resources, do NOT commit",
            },
            "commit": {
                "trigger": "finalized",
                "safe_to_execute": True,
                "agent_action": "Execute irreversible business logic",
            },
        },

        "output_schema": {
            "type": "object",
            "required": ["id", "idempotency_key", "type", "version", "timestamp", "execution", "data"],
            "properties": {
                "id": {"type": "string"},
                "idempotency_key": {"type": "string"},
                "type": {"type": "string"},
                "version": {"type": "string", "const": "v1"},
                "timestamp": {"type": "integer"},
                "execution": {
                    "type": "object",
                    "required": ["state", "safe_to_execute", "trust_source"],
                    "properties": {
                        "state": {"enum": ["provisional", "confirmed", "finalized", "reorged"]},
                        "safe_to_execute": {"type": "boolean"},
                        "trust_source": {"enum": ["facilitator", "onchain"]},
                        "finality": {
                            "oneOf": [
                                {"type": "null"},
                                {
                                    "type": "object",
                                    "required": ["confirmations", "required_confirmations", "is_finalized"],
                                    "properties": {
                                        "confirmations": {"type": "integer", "minimum": 0},
                                        "required_confirmations": {"type": "integer", "minimum": 1, "maximum": 64},
                                        "is_finalized": {"type": "boolean"},
                                    },
                                },
                            ],
                        },
                    },
                },
                "data": {"type": "object"},
                "identity": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string"},
                        "agent_class": {"type": "string"},
                        "reputation_score": {"type": "number", "minimum": 0, "maximum": 100},
                        "capabilities": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },

        "chain_finality": {
            "eip155:1": {"name": "Ethereum", "default_depth": 12, "block_time_ms": 12000},
            "eip155:8453": {"name": "Base", "default_depth": 3, "block_time_ms": 2000},
            "eip155:42161": {"name": "Arbitrum", "default_depth": 1, "block_time_ms": 250},
        },

        "determinism_guarantees": [
            "Idempotency: same event + same endpoint + same type = same idempotency_key",
            "Ordering: provisional -> confirmed -> finalized (reorged may arrive anytime after provisional)",
            "At-least-once delivery via Convoy (10 retries, exponential backoff, DLQ)",
            "Finality monotonicity: confirmations only increase (except on reorg)",
            "Nonce uniqueness: (chain_id, nonce, authorizer) is globally unique",
        ],
    }


@router.get("/.well-known/x402-manifest.json", status_code=410)
async def x402_manifest():
    """x402 v1 manifest — deprecated. Use GET /discovery/resources."""
    return JSONResponse(
        status_code=410,
        content={
            "error": "Gone",
            "message": "x402 v1 manifest is deprecated. Use GET /discovery/resources for x402 V2 Bazaar format.",
            "redirect": "/discovery/resources",
        },
    )


def _tool_to_resource(tool_def: ToolDef, now: str, networks: list[str]) -> dict:
    """Convert a ToolDef into a Bazaar discovery resource dict.

    For X402 tools, emits one PaymentOption per network in the tool's
    ``networks`` list (multi-chain support).
    """
    accepts: list[dict] = []
    if tool_def.auth_tier == AuthTier.X402 and tool_def.price:
        # Emit one accept entry per network the tool supports
        for net in tool_def.networks:
            if net in networks:
                accepts.append({
                    "scheme": "exact",
                    "price": tool_def.price,
                    "network": net,
                    "payTo": settings.tripwire_treasury_address,
                    "asset": "USDC",
                })
        # Fallback: if none of the tool's networks match, use primary
        if not accepts:
            accepts.append({
                "scheme": "exact",
                "price": tool_def.price,
                "network": networks[0],
                "payTo": settings.tripwire_treasury_address,
                "asset": "USDC",
            })

    return {
        "resource": f"{settings.app_base_url}/mcp#{tool_def.name}",
        "type": "mcp",
        "x402Version": 2,
        "accepts": accepts,
        "extensions": ["sign-in-with-x"],
        "metadata": {
            "tool": tool_def.name,
            "description": tool_def.description,
            "inputSchema": tool_def.input_schema,
            "transport": "json-rpc",
            "minReputation": tool_def.min_reputation,
        },
        "lastUpdated": now,
    }


@router.get("/discovery/resources")
async def discovery_resources():
    """x402 V2 Bazaar discovery — returns all MCP tools as resources."""
    from tripwire.mcp.server import TOOLS

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    # Multi-chain: use all configured networks
    networks: list[str] = settings.x402_networks

    return [_tool_to_resource(td, now, networks) for td in TOOLS.values()]
