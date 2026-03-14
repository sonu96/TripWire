"""x402 service manifest for Bazaar discovery."""

from fastapi import APIRouter

from tripwire.config.settings import settings
from tripwire.mcp.types import AuthTier

router = APIRouter()


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
    }
