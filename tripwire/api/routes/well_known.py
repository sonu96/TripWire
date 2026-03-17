"""x402 service manifest and TWSS-1 skill spec for Bazaar discovery."""

from fastapi import APIRouter

from tripwire.config.settings import settings
from tripwire.mcp.types import AuthTier

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
                                    "required": ["confirmations", "required", "is_finalized"],
                                    "properties": {
                                        "confirmations": {"type": "integer", "minimum": 0},
                                        "required": {"type": "integer", "minimum": 1, "maximum": 64},
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


@router.get("/.well-known/x402-manifest.json")
async def x402_manifest():
    # Import here to avoid circular imports (server.py registers tools at module level)
    from tripwire.mcp.server import TOOLS

    # Build services list from x402-gated tools
    services = []
    for tool_def in TOOLS.values():
        if tool_def.auth_tier == AuthTier.X402 and tool_def.price:
            services.append({
                "name": tool_def.name,
                "description": tool_def.description,
                "endpoint": "/mcp",
                "method": "POST",
                "scheme": "exact",
                "price": tool_def.price,
                "network": tool_def.network,
                "pay_to": settings.tripwire_treasury_address,
            })

    # Build full tool list with auth tiers
    mcp_tools = []
    for tool_def in TOOLS.values():
        tool_info = {
            "name": tool_def.name,
            "auth_tier": tool_def.auth_tier.value,
        }
        if tool_def.price:
            tool_info["price"] = tool_def.price
        mcp_tools.append(tool_info)

    return {
        "@context": "https://x402.org/context",
        "name": "TripWire",
        "description": "Programmable onchain event triggers for AI agents — middleware + event trigger platform",
        "version": "1.0.0",
        "identity": {
            "protocol": "ERC-8004",
            "registry": settings.erc8004_identity_registry,
        },
        "auth": {
            "siwe": {
                "nonce_endpoint": "/auth/nonce",
                "domain": settings.siwe_domain,
            },
            "x402": {
                "facilitator": settings.x402_facilitator_url,
                "network": settings.x402_network,
                "pay_to": settings.tripwire_treasury_address,
            },
        },
        "mcp": {
            "endpoint": "/mcp",
            "transport": "json-rpc",
            "tools": mcp_tools,
        },
        "services": services,
        "supported_chains": [
            {"chain_id": 8453, "name": "Base"},
            {"chain_id": 1, "name": "Ethereum"},
            {"chain_id": 42161, "name": "Arbitrum"},
        ],
        "skill_spec": {
            "version": TWSS_VERSION,
            "url": f"{settings.app_base_url}/.well-known/tripwire-skill-spec.json",
        },
    }
