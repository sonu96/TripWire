"""Shared Pydantic models for TripWire."""

from datetime import datetime
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── Reusable Types ─────────────────────────────────────────────

EthAddress = Annotated[str, Field(pattern=r"^0x[a-fA-F0-9]{40}$")]


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
    model_config = ConfigDict(str_strip_whitespace=True)

    chain_id: ChainId
    tx_hash: str
    block_number: int
    block_hash: str
    log_index: int
    from_address: EthAddress
    to_address: EthAddress
    value: str  # string to preserve USDC 6-decimal precision
    authorizer: EthAddress  # AuthorizationUsed: address that signed the authorization
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
    model_config = ConfigDict(str_strip_whitespace=True)

    address: EthAddress
    agent_class: str
    deployer: EthAddress
    capabilities: list[str]
    reputation_score: float = Field(ge=0, le=100)
    registered_at: int
    metadata: dict[str, Any] = Field(default_factory=dict)


# ── Endpoint Registration ─────────────────────────────────────

class EndpointMode(str, Enum):
    NOTIFY = "notify"
    EXECUTE = "execute"


class EndpointPolicies(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    min_amount: str | None = None
    max_amount: str | None = None
    allowed_senders: list[EthAddress] | None = None
    blocked_senders: list[EthAddress] | None = None
    required_agent_class: str | None = None
    min_reputation_score: float | None = Field(None, ge=0, le=100)
    finality_depth: int = Field(default=3, ge=1, le=64)


class RegisterEndpointRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    url: str
    mode: EndpointMode
    chains: list[int] = Field(min_length=1)
    recipient: EthAddress
    owner_address: EthAddress
    policies: EndpointPolicies | None = None

    @field_validator("url")
    @classmethod
    def url_must_be_safe(cls, v: str) -> str:
        from tripwire.api.validation import validate_endpoint_url

        return validate_endpoint_url(v)


class Endpoint(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: str
    url: str
    mode: EndpointMode
    chains: list[int]
    recipient: EthAddress
    owner_address: EthAddress
    registration_tx_hash: str | None = None
    registration_chain_id: int | None = None
    policies: EndpointPolicies
    active: bool = True
    convoy_project_id: str | None = None
    convoy_endpoint_id: str | None = None
    webhook_secret: str | None = None  # Per-endpoint HMAC signing secret
    created_at: datetime
    updated_at: datetime


# ── Webhook Payload ────────────────────────────────────────────

class WebhookEventType(str, Enum):
    PAYMENT_CONFIRMED = "payment.confirmed"
    PAYMENT_PENDING = "payment.pending"
    PAYMENT_PRE_CONFIRMED = "payment.pre_confirmed"
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


def build_finality_data(finality: "FinalityStatus | None") -> "FinalityData | None":
    """Build FinalityData from a FinalityStatus, if available."""
    if finality is None:
        return None
    return FinalityData(
        confirmations=finality.confirmations,
        required_confirmations=finality.required_confirmations,
        is_finalized=finality.is_finalized,
    )


class WebhookData(BaseModel):
    transfer: TransferData
    finality: FinalityData | None = None
    identity: AgentIdentity | None = None


class WebhookPayload(BaseModel):
    id: str
    idempotency_key: str
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
