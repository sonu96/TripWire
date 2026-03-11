"""Webhook dispatch orchestrator for TripWire."""

from __future__ import annotations

import asyncio
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
    WebhookData,
    WebhookEventType,
    WebhookPayload,
)
from tripwire.webhook.provider import WebhookProvider

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
    return WebhookPayload(
        id=str(uuid.uuid4()),
        type=event_type,
        mode=mode,
        timestamp=int(time.time()),
        data=WebhookData(
            transfer=build_transfer_data(transfer),
            finality=_build_finality_data(finality),
            identity=identity,
        ),
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
    provider: WebhookProvider,
    event_type: WebhookEventType = WebhookEventType.PAYMENT_CONFIRMED,
    finality: FinalityStatus | None = None,
    identity: AgentIdentity | None = None,
) -> list[str]:
    """Build a WebhookPayload and send via the webhook provider for each matched endpoint.

    For each endpoint two delivery paths are fired in parallel:
    1. Convoy managed queue — via provider.send() using convoy_project_id / convoy_endpoint_id.
    2. Direct httpx fast path — via provider.direct_deliver() posting straight to endpoint.url.

    Returns a list of Convoy message IDs for successful managed-queue deliveries.
    """
    message_ids: list[str] = []

    for endpoint in matched_endpoints:
        # Use stored convoy_project_id — skip dispatch if Convoy project setup failed.
        project_id = endpoint.convoy_project_id
        if not project_id:
            logger.error(
                "webhook_dispatch_skipped_no_convoy_project",
                endpoint_id=endpoint.id,
                tx_hash=transfer.tx_hash,
                reason="convoy_project_id not set — webhook provider setup may have failed",
            )
            continue

        payload = _build_payload(
            transfer=transfer,
            event_type=event_type,
            mode=endpoint.mode,
            finality=finality,
            identity=identity,
        )

        # Embed the Convoy endpoint ID so that ConvoyProvider.send() can forward it
        # to send_event() without changing the WebhookProvider.send() signature.
        payload_dict = payload.model_dump()
        if endpoint.convoy_endpoint_id:
            payload_dict["__convoy_endpoint_id__"] = endpoint.convoy_endpoint_id

        # --- Parallel delivery: managed queue + direct fast path ---
        convoy_coro = provider.send(
            app_id=project_id,
            event_type=event_type.value,
            payload=payload_dict,
        )
        direct_coro = provider.direct_deliver(
            url=endpoint.url,
            payload=payload.model_dump(),
            secret=endpoint.webhook_secret or "",
        )

        try:
            convoy_result, direct_result = await asyncio.gather(
                convoy_coro,
                direct_coro,
                return_exceptions=True,
            )
        except Exception:
            logger.exception(
                "webhook_dispatch_gather_failed",
                endpoint_id=endpoint.id,
                tx_hash=transfer.tx_hash,
            )
            continue

        # Log Convoy managed-queue result
        if isinstance(convoy_result, BaseException):
            logger.error(
                "webhook_convoy_send_failed",
                endpoint_id=endpoint.id,
                convoy_project_id=project_id,
                convoy_endpoint_id=endpoint.convoy_endpoint_id,
                tx_hash=transfer.tx_hash,
                event_type=event_type.value,
                error=str(convoy_result),
            )
        else:
            message_ids.append(convoy_result)
            logger.info(
                "webhook_convoy_send_ok",
                endpoint_id=endpoint.id,
                convoy_project_id=project_id,
                convoy_endpoint_id=endpoint.convoy_endpoint_id,
                tx_hash=transfer.tx_hash,
                event_type=event_type.value,
                message_id=convoy_result,
            )

        # Log direct fast-path result
        if isinstance(direct_result, BaseException):
            logger.error(
                "webhook_direct_deliver_failed",
                endpoint_id=endpoint.id,
                url=endpoint.url,
                tx_hash=transfer.tx_hash,
                event_type=event_type.value,
                error=str(direct_result),
            )
        else:
            logger.info(
                "webhook_direct_deliver_ok",
                endpoint_id=endpoint.id,
                url=endpoint.url,
                tx_hash=transfer.tx_hash,
                event_type=event_type.value,
                success=direct_result,
            )

    return message_ids
