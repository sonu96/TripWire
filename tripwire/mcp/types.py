"""MCP authentication types for TripWire."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine


class AuthTier(str, Enum):
    """Authentication tier for MCP tools."""
    PUBLIC = "public"   # No auth needed (health, discovery)
    SIWX = "siwx"       # Wallet signature (free, identity-gated)
    SESSION = "session"  # Pre-funded session with budget
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
    session_id: str | None = None
    budget_remaining: int | None = None


@dataclass
class ToolDef:
    """MCP tool definition with auth tier and pricing."""
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Coroutine[Any, Any, dict]]
    auth_tier: AuthTier = AuthTier.SIWX
    price: str | None = None        # e.g. "$0.003" for x402 tools
    networks: list[str] = field(default_factory=lambda: ["eip155:8453"])
    min_reputation: float = 0.0
    product: str = "both"           # ProductMode value: "pulse", "keeper", or "both"

    @property
    def network(self) -> str:
        """Primary network (backward compat)."""
        return self.networks[0] if self.networks else "eip155:8453"
