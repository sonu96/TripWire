"""MCP authentication types for TripWire."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Coroutine


class AuthTier(str, Enum):
    """Authentication tier for MCP tools."""
    PUBLIC = "public"   # No auth needed (health, discovery)
    SIWX = "siwx"       # Wallet signature (free, identity-gated)
    X402 = "x402"       # Payment required per-call


@dataclass(frozen=True)
class MCPAuthContext:
    """Authentication context passed to every MCP tool handler."""
    auth_tier: AuthTier
    agent_address: str | None = None
    identity: Any | None = None  # AgentIdentity from tripwire.types.models
    reputation_score: float = 0.0
    payment_verified: bool = False
    payer_address: str | None = None


@dataclass
class ToolDef:
    """MCP tool definition with auth tier and pricing."""
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Coroutine[Any, Any, dict]]
    auth_tier: AuthTier = AuthTier.SIWX
    price: str | None = None        # e.g. "$0.003" for x402 tools
    network: str = "eip155:8453"    # CAIP-2 chain for x402 payment
    min_reputation: float = 0.0
