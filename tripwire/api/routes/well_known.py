"""x402 service manifest for Bazaar discovery."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/.well-known/x402-manifest.json")
async def x402_manifest():
    return {
        "@context": "https://x402.org/context",
        "name": "TripWire",
        "description": "Programmable onchain event triggers for AI agents — middleware + event trigger platform",
        "version": "1.0.0",
        "identity": {
            "protocol": "ERC-8004",
            "registry": "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
        },
        "mcp": {
            "endpoint": "/mcp",
            "transport": "streamable-http",
            "tools": [
                "register_middleware",
                "create_trigger",
                "list_triggers",
                "delete_trigger",
                "list_templates",
                "activate_template",
                "get_trigger_status",
                "search_events",
            ],
        },
        "services": [
            {
                "name": "register_middleware",
                "description": "Register TripWire as onchain event middleware for your API",
                "endpoint": "/mcp",
                "method": "POST",
                "price": "$0.003",
                "network": "eip155:8453",
            },
            {
                "name": "create_trigger",
                "description": "Create a custom onchain event trigger",
                "endpoint": "/mcp",
                "method": "POST",
                "price": "$0.003",
                "network": "eip155:8453",
            },
            {
                "name": "activate_template",
                "description": "Activate a pre-built trigger template from the Bazaar",
                "endpoint": "/mcp",
                "method": "POST",
                "price": "$0.001",
                "network": "eip155:8453",
            },
        ],
        "supported_chains": [
            {"chain_id": 8453, "name": "Base"},
            {"chain_id": 1, "name": "Ethereum"},
            {"chain_id": 42161, "name": "Arbitrum"},
        ],
        "trigger_templates": "/mcp (use list_templates tool)",
    }
