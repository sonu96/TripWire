"""Payload builder helpers for TripWire webhook delivery.

Provides two builder functions:

- ``build_generic_payload`` — for Pulse (non-payment) events
- ``build_payment_payload`` — for Keeper (ERC-3009 payment) events

Both return a plain ``dict`` that is JSON-serialisable and conforms to
the ``WebhookPayload`` schema.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from tripwire.types.models import (
    AgentIdentity,
    ERC3009Transfer,
    EndpointMode,
    ExecutionBlock,
    ExecutionState,
    FinalityData,
    FinalityStatus,
    TransferData,
    TrustSource,
    WebhookData,
    WebhookEventType,
    WebhookPayload,
    build_finality_data,
    derive_execution_metadata,
)
from tripwire.webhook.dispatcher import (
    build_transfer_data,
    generate_generic_idempotency_key,
    generate_idempotency_key,
)


def build_generic_payload(
    event: dict[str, Any],
    event_type: str,
    endpoint_mode: str,
    execution_block: dict[str, Any] | None = None,
    identity_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a WebhookPayload dict for any event type (Pulse).

    Returns a plain dict (not a Pydantic model) ready for JSON serialisation
    and delivery via the webhook provider.

    Parameters
    ----------
    event:
        OnchainEvent-shaped dict. Should contain at least ``"event_id"``
        (or ``"id"``).
    event_type:
        Dot-delimited type string, e.g. ``"uniswap.swap"`` or
        ``"trigger.matched"``.
    endpoint_mode:
        ``"execute"`` or ``"notify"``.
    execution_block:
        Optional pre-built execution metadata dict. If ``None``, defaults
        to confirmed/not-safe/onchain.
    identity_data:
        Optional AgentIdentity-shaped dict for enrichment.

    Returns
    -------
    dict
        A serialised ``WebhookPayload``-compatible dict.
    """
    event_id = event.get("event_id") or event.get("id") or str(uuid.uuid4())
    endpoint_id = event.get("endpoint_id", "")
    trigger_id = event.get("trigger_id")

    # Idempotency
    idempotency_key = generate_generic_idempotency_key(event_id, endpoint_id)

    # Execution block
    if execution_block is not None:
        exec_data = execution_block
    else:
        exec_data = {
            "state": ExecutionState.CONFIRMED.value,
            "safe_to_execute": False,
            "trust_source": TrustSource.ONCHAIN.value,
            "finality": None,
        }

    # Identity
    identity_dict: dict[str, Any] | None = None
    if identity_data is not None:
        try:
            identity_dict = AgentIdentity(**identity_data).model_dump()
        except Exception:
            # Pass through raw dict if it doesn't conform to AgentIdentity
            identity_dict = identity_data

    # Resolve mode
    try:
        mode = EndpointMode(endpoint_mode)
    except ValueError:
        mode = EndpointMode.EXECUTE

    payload: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "idempotency_key": idempotency_key,
        "type": event_type,
        "mode": mode.value,
        "timestamp": int(time.time()),
        "version": "v1",
        "execution": exec_data,
        "data": {
            "event": event,
            "identity": identity_dict,
        },
    }

    if trigger_id is not None:
        payload["trigger_id"] = trigger_id

    return payload


def build_payment_payload(
    transfer: ERC3009Transfer,
    event_type: WebhookEventType,
    mode: EndpointMode,
    endpoint_id: str,
    finality: FinalityStatus | None = None,
    identity: AgentIdentity | None = None,
    required_depth: int | None = None,
) -> dict[str, Any]:
    """Build a WebhookPayload dict for payment events (Keeper).

    Delegates to the existing ``_build_payload`` logic in ``dispatcher.py``
    and returns a plain dict for JSON serialisation. This preserves exact
    backward compatibility with the current Keeper webhook format.

    Parameters
    ----------
    transfer:
        The decoded ERC-3009 transfer event.
    event_type:
        One of the ``payment.*`` WebhookEventType values.
    mode:
        Endpoint delivery mode (execute or notify).
    endpoint_id:
        The target endpoint's ID.
    finality:
        Optional FinalityStatus from the finality checker.
    identity:
        Optional resolved AgentIdentity.
    required_depth:
        Optional finality depth override from endpoint policies.

    Returns
    -------
    dict
        A serialised ``WebhookPayload``-compatible dict.
    """
    idempotency_key = generate_idempotency_key(
        chain_id=transfer.chain_id.value,
        tx_hash=transfer.tx_hash,
        log_index=transfer.log_index,
        endpoint_id=endpoint_id,
        event_type=event_type.value,
    )
    finality_data = build_finality_data(finality, required_depth=required_depth)
    execution = derive_execution_metadata(event_type, finality_data)

    transfer_data = build_transfer_data(transfer)

    payload = WebhookPayload(
        id=str(uuid.uuid4()),
        idempotency_key=idempotency_key,
        type=event_type,
        mode=mode,
        timestamp=int(time.time()),
        version="v1",
        execution=execution,
        data=WebhookData(
            transfer=transfer_data,
            finality=finality_data,
            identity=identity,
        ),
    )

    return payload.model_dump()
