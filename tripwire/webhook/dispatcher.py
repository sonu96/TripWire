"""Webhook dispatch orchestrator for TripWire."""

from __future__ import annotations

import time
import uuid

import structlog

from tripwire.types.models import (
    AgentIdentity,
    ERC3009Transfer,
    Endpoint,
    EndpointMode,
    FinalityData,
    FinalityStatus,
    Subscription,
    TransferData,
    WebhookEventType,
    WebhookPayload,
)
from tripwire.webhook.svix_client import send_webhook

logger = structlog.get_logger(__name__)


def build_transfer_data(transfer: ERC3009Transfer) -> TransferData:
    """Extract TransferData from a raw ERC-3009 transfer event."""
    return TransferData(
        chain_id=transfer.chain_id,
        tx_hash=transfer.tx_hash,
        block_number=transfer.block_number,
        from_address=transfer.from_address,
        to_address=transfer.to_address,
        amount=transfer.value,
        nonce=transfer.nonce,
        token=transfer.token,
    )


def _build_finality_data(finality: FinalityStatus | None) -> FinalityData | None:
    """Build FinalityData from a FinalityStatus, if available."""
    if finality is None:
        return None
    return FinalityData(
        confirmations=finality.confirmations,
        required_confirmations=finality.required_confirmations,
        is_finalized=finality.is_finalized,
    )


def _build_payload(
    transfer: ERC3009Transfer,
    event_type: WebhookEventType,
    mode: EndpointMode,
    finality: FinalityStatus | None = None,
    identity: AgentIdentity | None = None,
) -> WebhookPayload:
    """Build a WebhookPayload from transfer, finality, and identity data."""
    transfer_data = build_transfer_data(transfer)
    finality_data = _build_finality_data(finality)

    data: dict = {"transfer": transfer_data.model_dump()}
    if finality_data is not None:
        data["finality"] = finality_data.model_dump()
    if identity is not None:
        data["identity"] = identity.model_dump()

    return WebhookPayload(
        id=str(uuid.uuid4()),
        type=event_type,
        mode=mode,
        timestamp=int(time.time()),
        data=data,
    )


def match_endpoints(
    transfer: ERC3009Transfer,
    endpoints: list[Endpoint],
) -> list[Endpoint]:
    """Match endpoints by recipient address and chain.

    An endpoint matches if:
    - Its recipient matches the transfer's to_address (case-insensitive)
    - The transfer's chain_id is in the endpoint's chains list
    - The endpoint is active
    """
    matched: list[Endpoint] = []
    for ep in endpoints:
        if not ep.active:
            continue
        if ep.recipient.lower() != transfer.to_address.lower():
            continue
        if transfer.chain_id.value not in ep.chains:
            continue
        matched.append(ep)
    return matched


def match_subscriptions(
    transfer: ERC3009Transfer,
    identity: AgentIdentity | None,
    subscriptions: list[Subscription],
) -> list[Subscription]:
    """Match Notify-mode subscriptions against a transfer using subscription filters.

    A subscription matches if all specified filters pass:
    - chains: transfer chain_id is in the list
    - senders: transfer from_address is in the list (case-insensitive)
    - recipients: transfer to_address is in the list (case-insensitive)
    - min_amount: transfer value >= min_amount
    - agent_class: identity agent_class matches (if identity provided)
    """
    matched: list[Subscription] = []
    for sub in subscriptions:
        if not sub.active:
            continue
        f = sub.filters

        if f.chains and transfer.chain_id.value not in f.chains:
            continue
        if f.senders and transfer.from_address.lower() not in [
            s.lower() for s in f.senders
        ]:
            continue
        if f.recipients and transfer.to_address.lower() not in [
            r.lower() for r in f.recipients
        ]:
            continue
        if f.min_amount and int(transfer.value) < int(f.min_amount):
            continue
        if f.agent_class:
            if identity is None or identity.agent_class != f.agent_class:
                continue

        matched.append(sub)
    return matched


async def dispatch_event(
    transfer: ERC3009Transfer,
    matched_endpoints: list[Endpoint],
    event_type: WebhookEventType = WebhookEventType.PAYMENT_CONFIRMED,
    finality: FinalityStatus | None = None,
    identity: AgentIdentity | None = None,
) -> list[str]:
    """Build a WebhookPayload and send via Svix for each matched endpoint.

    Returns a list of Svix message IDs for successful deliveries.
    """
    message_ids: list[str] = []

    for endpoint in matched_endpoints:
        payload = _build_payload(
            transfer=transfer,
            event_type=event_type,
            mode=endpoint.mode,
            finality=finality,
            identity=identity,
        )

        try:
            msg_id = await send_webhook(
                app_id=endpoint.id,
                event_type=event_type.value,
                payload=payload.model_dump(),
            )
            message_ids.append(msg_id)
            logger.info(
                "webhook_dispatched",
                endpoint_id=endpoint.id,
                tx_hash=transfer.tx_hash,
                event_type=event_type.value,
                message_id=msg_id,
            )
        except Exception:
            logger.exception(
                "webhook_dispatch_failed",
                endpoint_id=endpoint.id,
                tx_hash=transfer.tx_hash,
            )

    return message_ids
