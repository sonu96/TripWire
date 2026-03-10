"""Re-exported Pydantic models for SDK consumers."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


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
    PAYMENT_FAILED = "payment.failed"
    PAYMENT_REORGED = "payment.reorged"


# ── Policies ──────────────────────────────────────────────────

class EndpointPolicies(BaseModel):
    min_amount: str | None = None
    max_amount: str | None = None
    allowed_senders: list[str] | None = None
    blocked_senders: list[str] | None = None
    required_agent_class: str | None = None
    min_reputation_score: float | None = Field(None, ge=0, le=100)
    finality_depth: int = Field(default=3, ge=1, le=64)


# ── Endpoint ──────────────────────────────────────────────────

class Endpoint(BaseModel):
    id: str
    url: str
    mode: EndpointMode
    chains: list[int]
    recipient: str
    policies: EndpointPolicies
    active: bool = True
    created_at: datetime
    updated_at: datetime


# ── Subscription ──────────────────────────────────────────────

class SubscriptionFilter(BaseModel):
    chains: list[int] | None = None
    senders: list[str] | None = None
    recipients: list[str] | None = None
    min_amount: str | None = None
    agent_class: str | None = None


class Subscription(BaseModel):
    id: str
    endpoint_id: str
    filters: SubscriptionFilter
    active: bool = True
    created_at: datetime


# ── Event ─────────────────────────────────────────────────────

class Event(BaseModel):
    id: str
    endpoint_id: str
    type: WebhookEventType
    data: dict[str, Any]
    created_at: str


# ── Paginated Response ────────────────────────────────────────

class PaginatedResponse(BaseModel):
    data: list[Event]
    cursor: str | None = None
    has_more: bool = False
