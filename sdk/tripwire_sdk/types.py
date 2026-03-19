"""Re-exported Pydantic models for SDK consumers."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ── Base Model ───────────────────────────────────────────────

class TripWireBaseModel(BaseModel):
    """Shared base for every SDK model.

    - ``extra="ignore"``  — silently drop unknown fields from the server.
    - ``frozen=True``      — instances are immutable (hashable by default).
    - ``str_strip_whitespace=True`` — auto-strip leading/trailing whitespace.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, str_strip_whitespace=True)


# ── Enums ─────────────────────────────────────────────────────

class ChainId(int, Enum):
    ETHEREUM = 1
    BASE = 8453
    ARBITRUM = 42161


class EndpointMode(str, Enum):
    NOTIFY = "notify"
    EXECUTE = "execute"


class WebhookEventType(str, Enum):
    PAYMENT_CONFIRMED = "payment.confirmed"
    PAYMENT_PENDING = "payment.pending"
    PAYMENT_PRE_CONFIRMED = "payment.pre_confirmed"
    PAYMENT_FAILED = "payment.failed"
    PAYMENT_REORGED = "payment.reorged"


# ── Policies ──────────────────────────────────────────────────

class EndpointPolicies(TripWireBaseModel):
    min_amount: str | None = None
    max_amount: str | None = None
    allowed_senders: list[str] | None = None
    blocked_senders: list[str] | None = None
    required_agent_class: str | None = None
    min_reputation_score: float | None = Field(None, ge=0, le=100)
    finality_depth: int = Field(default=3, ge=1, le=64)


# ── Endpoint ──────────────────────────────────────────────────

class Endpoint(TripWireBaseModel):
    id: str
    url: str
    mode: EndpointMode
    chains: list[int]
    recipient: str
    owner_address: str
    policies: EndpointPolicies
    active: bool = True
    created_at: datetime
    updated_at: datetime


# ── Subscription ──────────────────────────────────────────────

class SubscriptionFilter(TripWireBaseModel):
    chains: list[int] | None = None
    senders: list[str] | None = None
    recipients: list[str] | None = None
    min_amount: str | None = None
    agent_class: str | None = None


class Subscription(TripWireBaseModel):
    id: str
    endpoint_id: str
    filters: SubscriptionFilter
    active: bool = True
    created_at: datetime


# ── Transfer Data ────────────────────────────────────────────

class TransferData(TripWireBaseModel):
    chain_id: int
    tx_hash: str
    block_number: int
    from_address: str
    to_address: str
    amount: str
    nonce: str
    token: str


# ── Finality Data ────────────────────────────────────────────

class FinalityData(TripWireBaseModel):
    confirmations: int
    required_confirmations: int
    is_finalized: bool


# ── Webhook Data ─────────────────────────────────────────────

class WebhookData(TripWireBaseModel):
    transfer: TransferData
    finality: FinalityData
    identity: dict[str, Any] | None = None


# ── Event ─────────────────────────────────────────────────────

class Event(TripWireBaseModel):
    id: str
    type: WebhookEventType
    data: dict[str, Any]
    created_at: str


# ── Webhook Payload ───────────────────────────────────────────

class WebhookPayload(TripWireBaseModel):
    id: str
    idempotency_key: str
    type: WebhookEventType
    mode: EndpointMode
    timestamp: int
    data: WebhookData


# ── Paginated Response ────────────────────────────────────────

class PaginatedResponse(TripWireBaseModel):
    data: list[Event]
    cursor: str | None = None
    has_more: bool = False


# ── Session ──────────────────────────────────────────────────

class Session(TripWireBaseModel):
    """Keeper session — pre-funded budget for multiple tool calls."""

    session_id: str
    wallet_address: str
    budget_total: int
    budget_remaining: int
    budget_currency: str = "USDC"
    expires_at: str
    ttl_seconds: int
    chain_id: int = 8453
    status: str = "active"
    created_at: str = ""
    reputation_score: float = 0.0
    agent_class: str = "unknown"
