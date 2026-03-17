"""Webhook dispatch orchestrator for TripWire."""

from __future__ import annotations

import hashlib
import time
import uuid

import structlog

from tripwire.types.models import (
    AgentIdentity,
    ERC3009Transfer,
    Endpoint,
    EndpointMode,
    FinalityStatus,
    Subscription,
    TransferData,
    WebhookData,
    WebhookEventType,
    WebhookPayload,
    build_finality_data,
    derive_execution_metadata,
)
from tripwire.webhook.convoy_client import ConvoyCircuitOpenError
from tripwire.webhook.provider import WebhookProvider

logger = structlog.get_logger(__name__)


def generate_idempotency_key(
    chain_id: int, tx_hash: str, log_index: int, endpoint_id: str, event_type: str
) -> str:
    """Generate a deterministic idempotency key for a webhook delivery.

    The same (chain_id, tx_hash, log_index, endpoint_id, event_type) tuple
    always produces the same key, ensuring duplicate deliveries can be detected.
    """
    raw = f"{chain_id}:{tx_hash.lower()}:{log_index}:{endpoint_id}:{event_type}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:32]
    return f"idem_{digest}"


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


def _build_payload(
    transfer: ERC3009Transfer,
    event_type: WebhookEventType,
    mode: EndpointMode,
    endpoint_id: str,
    finality: FinalityStatus | None = None,
    identity: AgentIdentity | None = None,
    required_depth: int | None = None,
) -> WebhookPayload:
    """Build a WebhookPayload from transfer, finality, and identity data.

    If *required_depth* is provided it overrides the chain-default
    ``required_confirmations`` in the finality payload so that webhook
    consumers see the threshold that was actually applied for this endpoint.
    """
    idempotency_key = generate_idempotency_key(
        chain_id=transfer.chain_id.value,
        tx_hash=transfer.tx_hash,
        log_index=transfer.log_index,
        endpoint_id=endpoint_id,
        event_type=event_type.value,
    )
    finality_data = build_finality_data(finality, required_depth=required_depth)
    execution_state, safe_to_execute, trust_source = derive_execution_metadata(
        event_type, finality_data
    )
    return WebhookPayload(
        id=str(uuid.uuid4()),
        idempotency_key=idempotency_key,
        type=event_type,
        mode=mode,
        timestamp=int(time.time()),
        version="v1",
        execution_state=execution_state,
        safe_to_execute=safe_to_execute,
        trust_source=trust_source,
        data=WebhookData(
            transfer=build_transfer_data(transfer),
            finality=finality_data,
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


def resolve_endpoint_depth(
    endpoint: Endpoint,
    chain_id: "ChainId",
) -> int | None:
    """Return the effective finality depth for an endpoint.

    Returns ``None`` (meaning "use chain default") when the endpoint has no
    explicit ``finality_depth`` override.
    """
    from tripwire.types.models import FINALITY_DEPTHS, EndpointPolicies

    policies = endpoint.policies or EndpointPolicies()
    if policies.finality_depth is not None:
        return policies.finality_depth
    return None


async def dispatch_event(
    transfer: ERC3009Transfer,
    matched_endpoints: list[Endpoint],
    provider: WebhookProvider,
    event_type: WebhookEventType = WebhookEventType.PAYMENT_CONFIRMED,
    finality: FinalityStatus | None = None,
    identity: AgentIdentity | None = None,
) -> list[str]:
    """Build a WebhookPayload and send via Convoy for each matched endpoint.

    All deliveries are routed through Convoy which handles retries, DLQ, and
    delivery logging. Returns a list of Convoy message IDs for successful sends.
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

        ep_depth = resolve_endpoint_depth(endpoint, transfer.chain_id)
        payload = _build_payload(
            transfer=transfer,
            event_type=event_type,
            mode=endpoint.mode,
            endpoint_id=endpoint.id,
            finality=finality,
            identity=identity,
            required_depth=ep_depth,
        )

        # Embed the Convoy endpoint ID so that ConvoyProvider.send() can forward it
        # to send_event() without changing the WebhookProvider.send() signature.
        payload_dict = payload.model_dump()
        if endpoint.convoy_endpoint_id:
            payload_dict["__convoy_endpoint_id__"] = endpoint.convoy_endpoint_id

        try:
            message_id = await provider.send(
                app_id=project_id,
                event_type=event_type.value,
                payload=payload_dict,
            )
            message_ids.append(message_id)
            logger.info(
                "webhook_convoy_send_ok",
                endpoint_id=endpoint.id,
                convoy_project_id=project_id,
                convoy_endpoint_id=endpoint.convoy_endpoint_id,
                tx_hash=transfer.tx_hash,
                event_type=event_type.value,
                message_id=message_id,
            )
        except ConvoyCircuitOpenError:
            logger.warning(
                "convoy_circuit_open",
                endpoint_id=endpoint.id,
                convoy_project_id=project_id,
                tx_hash=transfer.tx_hash,
                event_type=event_type.value,
            )
        except Exception:
            logger.exception(
                "webhook_convoy_send_failed",
                endpoint_id=endpoint.id,
                convoy_project_id=project_id,
                convoy_endpoint_id=endpoint.convoy_endpoint_id,
                tx_hash=transfer.tx_hash,
                event_type=event_type.value,
            )

    return message_ids
