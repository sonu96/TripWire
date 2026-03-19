"""Shared Pydantic models for TripWire."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from tripwire.ingestion.decoders.protocol import DecodedEvent


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


class ProductMode(str, Enum):
    """Product mode — Pulse (generic triggers), Keeper (payment monitoring), or both."""
    PULSE = "pulse"
    KEEPER = "keeper"
    BOTH = "both"


class EndpointPolicies(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    min_amount: str | None = None
    max_amount: str | None = None
    allowed_senders: list[EthAddress] | None = None
    blocked_senders: list[EthAddress] | None = None
    required_agent_class: str | None = None
    min_reputation_score: float | None = Field(None, ge=0, le=100)
    finality_depth: int | None = Field(default=None, ge=1, le=64)


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

class ExecutionState(str, Enum):
    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"
    FINALIZED = "finalized"
    REORGED = "reorged"


class TrustSource(str, Enum):
    FACILITATOR = "facilitator"
    ONCHAIN = "onchain"


class WebhookEventType(str, Enum):
    # Keeper (payment) event types
    PAYMENT_CONFIRMED = "payment.confirmed"
    PAYMENT_PENDING = "payment.pending"
    PAYMENT_PRE_CONFIRMED = "payment.pre_confirmed"
    PAYMENT_FAILED = "payment.failed"
    PAYMENT_REORGED = "payment.reorged"
    PAYMENT_FINALIZED = "payment.finalized"

    # Pulse (generic trigger) event types
    TRIGGER_MATCHED = "trigger.matched"
    TRIGGER_CONFIRMED = "trigger.confirmed"
    TRIGGER_FINALIZED = "trigger.finalized"


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


class ExecutionBlock(BaseModel):
    """Nested execution metadata per TWSS-1 spec (Section 7.2)."""
    state: ExecutionState
    safe_to_execute: bool = False
    trust_source: TrustSource = TrustSource.ONCHAIN
    finality: FinalityData | None = None


def build_finality_data(
    finality: "FinalityStatus | None",
    required_depth: int | None = None,
) -> "FinalityData | None":
    """Build FinalityData from a FinalityStatus, if available.

    If *required_depth* is provided it overrides the chain-default
    ``required_confirmations`` stored on the FinalityStatus.  The
    ``is_finalized`` flag is recomputed against the override so that
    the webhook payload accurately reflects the endpoint's configured
    finality threshold.
    """
    if finality is None:
        return None
    req = required_depth if required_depth is not None else finality.required_confirmations
    return FinalityData(
        confirmations=finality.confirmations,
        required_confirmations=req,
        is_finalized=finality.confirmations >= req,
    )


def derive_execution_metadata(
    event_type: WebhookEventType,
    finality: FinalityData | None,
) -> ExecutionBlock:
    """Derive an ExecutionBlock from event type and finality data.

    Returns a nested ExecutionBlock per TWSS-1 Section 7.2.

    Mapping:
    - PRE_CONFIRMED -> provisional, false, facilitator
    - REORGED/FAILED -> reorged, false, onchain
    - PAYMENT_FINALIZED / TRIGGER_FINALIZED or finality.is_finalized -> finalized, true, onchain
    - TRIGGER_MATCHED -> confirmed, false, onchain  (initial match, not yet final)
    - TRIGGER_CONFIRMED -> confirmed, false, onchain
    - else -> confirmed, false, onchain
    """
    if event_type == WebhookEventType.PAYMENT_PRE_CONFIRMED:
        state, safe, trust = ExecutionState.PROVISIONAL, False, TrustSource.FACILITATOR
    elif event_type in (WebhookEventType.PAYMENT_REORGED, WebhookEventType.PAYMENT_FAILED):
        state, safe, trust = ExecutionState.REORGED, False, TrustSource.ONCHAIN
    elif event_type in (WebhookEventType.PAYMENT_FINALIZED, WebhookEventType.TRIGGER_FINALIZED):
        state, safe, trust = ExecutionState.FINALIZED, True, TrustSource.ONCHAIN
    elif finality is not None and finality.is_finalized:
        state, safe, trust = ExecutionState.FINALIZED, True, TrustSource.ONCHAIN
    elif event_type == WebhookEventType.TRIGGER_MATCHED:
        state, safe, trust = ExecutionState.CONFIRMED, False, TrustSource.ONCHAIN
    elif event_type == WebhookEventType.TRIGGER_CONFIRMED:
        state, safe, trust = ExecutionState.CONFIRMED, False, TrustSource.ONCHAIN
    else:
        state, safe, trust = ExecutionState.CONFIRMED, False, TrustSource.ONCHAIN

    return ExecutionBlock(
        state=state,
        safe_to_execute=safe,
        trust_source=trust,
        finality=finality,
    )


def execution_state_from_status(
    status: str,
) -> tuple[ExecutionState, bool, TrustSource]:
    """Derive execution metadata from the DB ``events.status`` column.

    Maps the 5 lifecycle status values to (execution_state, safe_to_execute,
    trust_source).  Used at the API/MCP response layer so we don't need extra
    DB columns.
    """
    _STATUS_MAP: dict[str, tuple[ExecutionState, bool, TrustSource]] = {
        "pre_confirmed": (ExecutionState.PROVISIONAL, False, TrustSource.FACILITATOR),
        "pending": (ExecutionState.CONFIRMED, False, TrustSource.ONCHAIN),
        "confirmed": (ExecutionState.CONFIRMED, False, TrustSource.ONCHAIN),
        "finalized": (ExecutionState.FINALIZED, True, TrustSource.ONCHAIN),
        "reorged": (ExecutionState.REORGED, False, TrustSource.ONCHAIN),
    }
    return _STATUS_MAP.get(
        status,
        (ExecutionState.CONFIRMED, False, TrustSource.ONCHAIN),
    )


# ── Event-Neutral Data Model (v2) ─────────────────────────────
#
# OnchainEvent is the product-neutral base for any onchain event.
# PaymentEvent extends it for Keeper (x402 / ERC-3009 payments).
# TriggerEvent extends it for Pulse (generic trigger-matched events).


class OnchainEvent(BaseModel):
    """Base event model — product-neutral. Any onchain event."""
    event_id: str
    event_type: str                    # e.g. "erc3009.transfer", "uniswap.swap", "aave.liquidation"
    chain_id: int
    tx_hash: str
    block_number: int
    block_hash: str = ""
    log_index: int = 0
    contract_address: str
    topic0: str
    decoded_fields: dict[str, Any]     # Generic decoded event data
    timestamp: int
    execution: ExecutionBlock          # Universal — provisional/confirmed/finalized
    identity: AgentIdentity | None = None
    source: str = "onchain"            # "onchain", "facilitator", "manual"


class PaymentData(BaseModel):
    """Payment-specific fields for x402/ERC-3009 events."""
    amount: str                        # Smallest unit (USDC = 6 decimals)
    token: str                         # Token contract address
    from_address: str
    to_address: str
    nonce: str = ""                    # ERC-3009 nonce
    authorizer: str = ""               # ERC-3009 authorizer


class PaymentEvent(OnchainEvent):
    """Payment event — extends OnchainEvent with payment-specific data."""
    event_type: str = "erc3009.transfer"
    payment: PaymentData


class TriggerEvent(OnchainEvent):
    """Trigger-matched event — extends OnchainEvent with trigger context."""
    trigger_id: str
    trigger_name: str = ""
    filter_matched: bool = True


# ── Webhook Payload (v1 + v2) ─────────────────────────────────
#
# WebhookData supports both v1 (transfer-centric) and v2 (event-neutral)
# formats.  The `transfer` field is preserved for Keeper backward
# compatibility; the `event` field carries the new OnchainEvent hierarchy.


class WebhookData(BaseModel):
    """Webhook data — supports both v1 (transfer) and v2 (event) formats.

    v1 payloads: ``transfer`` is set, ``event`` is None.
    v2 payloads: ``event`` is always set; ``transfer`` may also be set
    for Keeper backward compat.

    ``event`` accepts ``OnchainEvent.model_dump()`` output or raw event
    field dicts for Pulse (non-payment) events.
    """
    event: dict[str, Any] | None = None  # v2: event-neutral payload (model_dump()'d)
    transfer: TransferData | None = None  # v1/backward compat for Keeper
    finality: FinalityData | None = None
    identity: AgentIdentity | dict | None = None


class WebhookPayload(BaseModel):
    id: str
    idempotency_key: str
    type: WebhookEventType
    mode: EndpointMode
    timestamp: int
    version: str = "v1"
    execution: ExecutionBlock
    data: WebhookData
    # Optional trigger metadata for Pulse events
    trigger_id: str | None = None


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


# ── Trigger Registry ─────────────────────────────────────────


class TriggerFilter(BaseModel):
    """Filter predicate applied to decoded event fields."""
    field: str
    op: str = "eq"
    value: Any = None


class Trigger(BaseModel):
    """Dynamic trigger definition from the trigger registry."""
    model_config = ConfigDict(str_strip_whitespace=True)
    id: str
    owner_address: str
    endpoint_id: str
    name: str | None = None
    event_signature: str
    topic0: str | None = None  # precomputed keccak256 hash of event_signature
    abi: list[dict[str, Any]]
    contract_address: str | None = None
    chain_ids: list[int] = Field(default_factory=list)
    filter_rules: list[TriggerFilter] = Field(default_factory=list)
    webhook_event_type: str
    reputation_threshold: float = 0.0
    required_agent_class: str | None = None
    version: str = "1.0.0"
    batch_id: str | None = None
    # C3: Payment gating — require decoded event to contain payment meeting threshold
    require_payment: bool = False
    payment_token: str | None = None  # Required token contract (None = any token)
    min_payment_amount: str | None = None  # Minimum amount in smallest unit
    active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TriggerTemplate(BaseModel):
    """Pre-built trigger template for the Bazaar."""
    model_config = ConfigDict(str_strip_whitespace=True)
    id: str
    name: str
    slug: str
    version: str = "1.0.0"
    description: str | None = None
    category: str = "general"
    event_signature: str
    topic0: str | None = None  # precomputed keccak256 hash of event_signature
    abi: list[dict[str, Any]]
    default_chains: list[int] = Field(default_factory=list)
    default_filters: list[TriggerFilter] = Field(default_factory=list)
    parameter_schema: list[dict[str, Any]] = Field(default_factory=list)
    webhook_event_type: str
    reputation_threshold: float = 0.0
    author_address: str | None = None
    is_public: bool = True
    install_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── Conversion Helpers ────────────────────────────────────────
#
# Bridge legacy types (ERC3009Transfer, DecodedEvent) to the new
# event-neutral OnchainEvent / PaymentEvent hierarchy.


# ERC-3009 events emit a standard ERC-20 Transfer event onchain.
# topic0 = keccak256("Transfer(address,address,uint256)")
# This is the same constant as TRANSFER_TOPIC in tripwire/ingestion/decoder.py.
_ERC3009_TOPIC0 = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f1"
    "63c4a11628f55a4df523b3ef"
)


def erc3009_to_onchain_event(
    transfer: ERC3009Transfer,
    execution: ExecutionBlock,
    identity: AgentIdentity | None = None,
    event_id: str | None = None,
) -> PaymentEvent:
    """Convert a legacy ERC3009Transfer to the new PaymentEvent model.

    This bridges the Keeper-era ERC3009Transfer into the event-neutral
    PaymentEvent so that downstream code can work with a single type.
    """
    return PaymentEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type="erc3009.transfer",
        chain_id=int(transfer.chain_id),
        tx_hash=transfer.tx_hash,
        block_number=transfer.block_number,
        block_hash=transfer.block_hash,
        log_index=transfer.log_index,
        contract_address=transfer.token,
        topic0=_ERC3009_TOPIC0,
        decoded_fields={
            "from": transfer.from_address,
            "to": transfer.to_address,
            "value": transfer.value,
            "validAfter": transfer.valid_after,
            "validBefore": transfer.valid_before,
            "nonce": transfer.nonce,
            "authorizer": transfer.authorizer,
        },
        timestamp=transfer.timestamp,
        execution=execution,
        identity=identity,
        source="onchain",
        payment=PaymentData(
            amount=transfer.value,
            token=transfer.token,
            from_address=transfer.from_address,
            to_address=transfer.to_address,
            nonce=transfer.nonce,
            authorizer=transfer.authorizer,
        ),
    )


def decoded_event_to_onchain_event(
    decoded: DecodedEvent,
    execution: ExecutionBlock,
    timestamp: int = 0,
    identity: AgentIdentity | None = None,
    event_id: str | None = None,
    source: str = "onchain",
) -> OnchainEvent:
    """Convert a DecodedEvent to the new OnchainEvent model.

    If the decoded event carries payment metadata (payment_amount is set),
    a PaymentEvent is returned instead of a plain OnchainEvent.
    """
    common = dict(
        event_id=event_id or str(uuid.uuid4()),
        event_type=f"decoded.{decoded.decoder_name}",
        chain_id=decoded.chain_id or 0,
        tx_hash=decoded.tx_hash,
        block_number=decoded.block_number,
        block_hash=decoded.block_hash,
        log_index=decoded.log_index,
        contract_address=decoded.contract_address,
        topic0=decoded.topic0,
        decoded_fields=decoded.fields,
        timestamp=timestamp,
        execution=execution,
        identity=identity,
        source=source,
    )

    # If the decoded event contains payment metadata, produce a PaymentEvent.
    if decoded.payment_amount is not None:
        common["event_type"] = f"payment.{decoded.decoder_name}"
        return PaymentEvent(
            **common,
            payment=PaymentData(
                amount=decoded.payment_amount,
                token=decoded.payment_token or "",
                from_address=decoded.payment_from or "",
                to_address=decoded.payment_to or "",
            ),
        )

    return OnchainEvent(**common)
