"""Shared Pydantic models for TripWire."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Chain Types ─────────────────────────────────────────────────

class ChainId(int, Enum):
    ETHEREUM = 1
    BASE = 8453
    ARBITRUM = 42161


CHAIN_NAMES: dict[ChainId, str] = {
    ChainId.ETHEREUM: "ethereum",
    ChainId.BASE: "base",
    ChainId.ARBITRUM: "arbitrum",
}

FINALITY_DEPTHS: dict[ChainId, int] = {
    ChainId.ETHEREUM: 12,
    ChainId.BASE: 3,
    ChainId.ARBITRUM: 1,
}

USDC_CONTRACTS: dict[ChainId, str] = {
    ChainId.BASE: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    ChainId.ETHEREUM: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    ChainId.ARBITRUM: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
}


# ── ERC-3009 Transfer ──────────────────────────────────────────

class ERC3009Transfer(BaseModel):
    chain_id: ChainId
    tx_hash: str
    block_number: int
    block_hash: str
    log_index: int
    from_address: str
    to_address: str
    value: str  # string to preserve USDC 6-decimal precision
    authorizer: str  # AuthorizationUsed: address that signed the authorization
    valid_after: int
    valid_before: int
    nonce: str  # AuthorizationUsed: bytes32 hex nonce
    token: str  # USDC contract address
    timestamp: int


# ── Finality ───────────────────────────────────────────────────

class FinalityStatus(BaseModel):
    tx_hash: str
    chain_id: ChainId
    block_number: int
    confirmations: int
    required_confirmations: int
    is_finalized: bool
    finalized_at: int | None = None


# ── ERC-8004 Agent Identity ───────────────────────────────────

class AgentIdentity(BaseModel):
    address: str
    agent_class: str
    deployer: str
    capabilities: list[str]
    reputation_score: float = Field(ge=0, le=100)
    registered_at: int
    metadata: dict[str, Any] = {}


# ── Endpoint Registration ─────────────────────────────────────

class EndpointMode(str, Enum):
    NOTIFY = "notify"
    EXECUTE = "execute"


class EndpointPolicies(BaseModel):
    min_amount: str | None = None
    max_amount: str | None = None
    allowed_senders: list[str] | None = None
    blocked_senders: list[str] | None = None
    required_agent_class: str | None = None
    min_reputation_score: float | None = Field(None, ge=0, le=100)
    finality_depth: int = Field(default=3, ge=1, le=64)


class RegisterEndpointRequest(BaseModel):
    url: str
    mode: EndpointMode
    chains: list[int] = Field(min_length=1)
    recipient: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")
    policies: EndpointPolicies | None = None


class Endpoint(BaseModel):
    id: str
    url: str
    mode: EndpointMode
    chains: list[int]
    recipient: str
    policies: EndpointPolicies
    active: bool = True
    api_key: str | None = None  # Only populated on creation/rotation response
    svix_app_id: str | None = None
    svix_endpoint_id: str | None = None
    key_rotated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


# ── Webhook Payload ────────────────────────────────────────────

class WebhookEventType(str, Enum):
    PAYMENT_CONFIRMED = "payment.confirmed"
    PAYMENT_PENDING = "payment.pending"
    PAYMENT_FAILED = "payment.failed"
    PAYMENT_REORGED = "payment.reorged"


class TransferData(BaseModel):
    chain_id: ChainId
    tx_hash: str
    block_number: int
    from_address: str
    to_address: str
    amount: str
    nonce: str
    token: str


class FinalityData(BaseModel):
    confirmations: int
    required_confirmations: int
    is_finalized: bool


class WebhookData(BaseModel):
    transfer: TransferData
    finality: FinalityData | None = None
    identity: AgentIdentity | None = None


class WebhookPayload(BaseModel):
    id: str
    type: WebhookEventType
    mode: EndpointMode
    timestamp: int
    data: WebhookData


# ── Subscription (Notify Mode) ────────────────────────────────

class SubscriptionFilter(BaseModel):
    chains: list[int] | None = None
    senders: list[str] | None = None
    recipients: list[str] | None = None
    min_amount: str | None = None
    agent_class: str | None = None


class CreateSubscriptionRequest(BaseModel):
    filters: SubscriptionFilter


class Subscription(BaseModel):
    id: str
    endpoint_id: str
    filters: SubscriptionFilter
    active: bool = True
    created_at: datetime
