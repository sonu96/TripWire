"""Trigger (Pulse) event handler.

Extracts dynamic trigger processing from the monolithic processor into
a standalone handler.  Covers:
  - Dynamic trigger detection (tuple event types)
  - Decode via AbiGenericDecoder per trigger
  - Filter evaluation via evaluate_filters()
  - Deduplication (tx_hash:log_index:trigger_id)
  - Identity resolution
  - Reputation threshold gating
  - Endpoint fetch by trigger.endpoint_id
  - Webhook dispatch
  - Event recording

Also includes the unified processing path (``_process_unified``) and
the C3 payment gating check.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

import structlog

from tripwire.observability.tracing import tracer, StatusCode
from tripwire.api.policies.engine import evaluate_policy
from tripwire.ingestion.decoders import ERC3009Decoder, AbiGenericDecoder
from tripwire.ingestion.decoders.protocol import DecodedEvent
from tripwire.ingestion.filter_engine import evaluate_filters
from tripwire.ingestion.finality import check_finality_generic
from tripwire.observability.metrics import record_pipeline_timing
from tripwire.types.models import (
    FINALITY_DEPTHS,
    EndpointMode,
    EndpointPolicies,
    WebhookEventType,
)

if TYPE_CHECKING:
    from tripwire.ingestion.processor import EventProcessor

logger = structlog.get_logger(__name__)


class TriggerHandler:
    """Handles dynamic trigger events (Pulse product)."""

    async def can_handle(
        self,
        event_type: str | tuple[str, list],
        raw_log: dict[str, Any],
    ) -> bool:
        """Return True for dynamic trigger tuples ``("dynamic", triggers)``."""
        return isinstance(event_type, tuple)

    async def handle(
        self,
        raw_log: dict[str, Any],
        processor: EventProcessor,
        event_type: str | tuple[str, list],
    ) -> dict[str, Any]:
        """Process a raw log against one or more dynamic triggers."""
        _, triggers = event_type  # type: ignore[misc]
        return await _process_dynamic_event(raw_log, triggers, processor)


# ── Legacy dynamic trigger path ────────────────────────────────────


async def _process_dynamic_event(
    raw_log: dict[str, Any],
    triggers: list,
    processor: EventProcessor,
) -> dict[str, Any]:
    """Process a raw log against one or more dynamic triggers.

    For each matched trigger: decode with its ABI, apply filters,
    deduplicate, resolve identity, fetch endpoint, and dispatch.
    """
    from tripwire.types.models import Trigger  # noqa: F811

    pipeline_start = time.perf_counter()
    tx_hash = raw_log.get("transaction_hash", "")
    log_index = raw_log.get("log_index", 0)
    results: list[dict[str, Any]] = []

    for trigger in triggers:
        trigger: Trigger
        trigger_id = trigger.id

        # 1. Decode event using trigger's ABI
        try:
            decoded_event = AbiGenericDecoder(abi_fragment=trigger.abi).decode(raw_log)
            decoded = decoded_event.fields
        except Exception:
            logger.exception(
                "dynamic_decode_failed",
                trigger_id=trigger_id,
                tx_hash=tx_hash,
            )
            results.append({"trigger_id": trigger_id, "status": "error", "reason": "decode_failed"})
            continue

        # 2. Apply trigger-specific filters
        passed, reason = evaluate_filters(decoded, trigger.filter_rules)
        if not passed:
            logger.info(
                "dynamic_filter_rejected",
                trigger_id=trigger_id,
                tx_hash=tx_hash,
                reason=reason,
            )
            results.append({"trigger_id": trigger_id, "status": "filtered", "reason": reason})
            continue

        # 3. Deduplication: tx_hash:log_index:trigger_id
        dedup_nonce = f"{tx_hash}:{log_index}:{trigger_id}"
        try:
            is_new = await asyncio.to_thread(
                processor._nonce_repo.record_nonce,
                chain_id=decoded.get("_chain_id") or 0,
                nonce=dedup_nonce,
                authorizer="dynamic_trigger",
            )
        except Exception:
            logger.exception("dynamic_dedup_failed", trigger_id=trigger_id, tx_hash=tx_hash)
            results.append({"trigger_id": trigger_id, "status": "error", "reason": "dedup_failed"})
            continue

        if not is_new:
            logger.info("dynamic_duplicate", trigger_id=trigger_id, tx_hash=tx_hash)
            results.append({"trigger_id": trigger_id, "status": "duplicate"})
            continue

        # 4. Identity resolution -- use first address field found
        identity = None
        address_for_identity = None
        for key, val in decoded.items():
            if key.startswith("_"):
                continue
            if isinstance(val, str) and len(val) == 42 and val.startswith("0x"):
                address_for_identity = val
                break

        if address_for_identity:
            try:
                identity = await processor._resolver.resolve(
                    address_for_identity, decoded.get("_chain_id") or 0
                )
            except Exception:
                logger.warning(
                    "dynamic_identity_failed",
                    trigger_id=trigger_id,
                    address=address_for_identity,
                )

        # 4b. Reputation threshold gating
        if trigger.reputation_threshold > 0:
            rep_score = identity.reputation_score if identity else 0.0
            if rep_score < trigger.reputation_threshold:
                logger.info(
                    "dynamic_reputation_rejected",
                    trigger_id=trigger_id,
                    tx_hash=tx_hash,
                    reputation=rep_score,
                    threshold=trigger.reputation_threshold,
                )
                results.append({
                    "trigger_id": trigger_id,
                    "status": "filtered",
                    "reason": "reputation_below_threshold",
                })
                continue

        # 5. Fetch endpoint by trigger.endpoint_id
        try:
            endpoint = await asyncio.to_thread(
                processor._endpoint_repo.get_by_id, trigger.endpoint_id
            )
        except Exception:
            logger.exception(
                "dynamic_endpoint_fetch_failed",
                trigger_id=trigger_id,
                endpoint_id=trigger.endpoint_id,
            )
            results.append({"trigger_id": trigger_id, "status": "error", "reason": "endpoint_fetch_failed"})
            continue

        if not endpoint or not endpoint.active:
            logger.info(
                "dynamic_endpoint_inactive",
                trigger_id=trigger_id,
                endpoint_id=trigger.endpoint_id,
            )
            results.append({"trigger_id": trigger_id, "status": "skipped", "reason": "endpoint_inactive"})
            continue

        # 6. Dispatch via webhook provider
        event_type_str = WebhookEventType.TRIGGER_MATCHED.value
        event_id = str(uuid.uuid4())
        identity_dict = identity.model_dump() if identity else None
        payload = {
            "id": event_id,
            "idempotency_key": f"dyn_{tx_hash}_{log_index}_{trigger_id}",
            "type": event_type_str,
            "mode": endpoint.mode.value,
            "timestamp": int(time.time()),
            "version": "v1",
            "execution": {
                "state": "confirmed",
                "safe_to_execute": False,
                "trust_source": "onchain",
                "finality": None,
            },
            "trigger_id": trigger_id,
            "data": {
                "event": decoded,
                "identity": identity_dict,
            },
        }

        message_id = None
        if endpoint.convoy_project_id:
            try:
                message_id = await processor._webhook_provider.send(
                    app_id=endpoint.convoy_project_id,
                    event_type=event_type_str,
                    payload=payload,
                )
            except Exception:
                logger.exception(
                    "dynamic_dispatch_failed",
                    trigger_id=trigger_id,
                    tx_hash=tx_hash,
                )

        # 7. Record event and link endpoint via join table
        now = datetime.now(timezone.utc).isoformat()
        row: dict[str, Any] = {
            "id": event_id,
            "type": event_type_str,
            "data": decoded,
            "created_at": now,
            "chain_id": decoded.get("_chain_id"),
            "tx_hash": tx_hash,
            "block_number": decoded.get("_block_number", 0),
            "block_hash": decoded.get("_block_hash", ""),
            "log_index": decoded.get("_log_index", 0),
            "from_address": address_for_identity or "",
            "to_address": decoded.get("_address", ""),
            "status": "confirmed",
            "endpoint_id": trigger.endpoint_id,
            # New schema columns (migration 026)
            "event_type": trigger.webhook_event_type or event_type_str,
            "decoded_fields": decoded,
            "trigger_id": trigger_id,
            "product_source": "pulse",
            "source": "onchain",
        }
        if identity:
            row["identity_data"] = identity.model_dump()
        try:
            processor._event_repo.insert(row)
            # Link endpoint via join table (#7)
            processor._event_repo.link_endpoints(event_id, [trigger.endpoint_id])
        except Exception:
            logger.exception("dynamic_event_record_failed", event_id=event_id)

        # Record delivery
        if message_id:
            processor._record_delivery(
                endpoint_id=endpoint.id,
                event_id=event_id,
                provider_message_id=message_id,
            )

        results.append({
            "trigger_id": trigger_id,
            "status": "processed",
            "event_id": event_id,
            "message_id": message_id,
        })

    total_ms = round((time.perf_counter() - pipeline_start) * 1000, 2)
    processed_count = sum(1 for r in results if r.get("status") == "processed")

    logger.info(
        "dynamic_event_processed",
        tx_hash=tx_hash,
        triggers_evaluated=len(triggers),
        triggers_dispatched=processed_count,
        total_ms=total_ms,
    )

    return {
        "status": "processed" if processed_count > 0 else "filtered",
        "tx_hash": tx_hash,
        "triggers_evaluated": len(triggers),
        "triggers_dispatched": processed_count,
        "results": results,
    }


# ── C2: Unified processing loop ────────────────────────────────────


async def _process_unified(
    raw_log: dict[str, Any],
    triggers: list | None,
    processor: EventProcessor,
) -> dict[str, Any]:
    """Unified processing loop for all event types (Phase C2).

    Single code path for ERC-3009 and dynamic trigger events.
    Uses DecodedEvent as the uniform data structure throughout.

    Steps:
      1. Decode (ERC3009Decoder or AbiGenericDecoder per trigger)
      2. Filter (trigger filter_rules, if applicable)
      3. Payment gating (C3 -- check DecodedEvent payment metadata)
      4. Deduplication
      5. Finality + Identity (parallel)
      6. Endpoint resolution + policy evaluation
      7. Dispatch (webhook + notify)
      8. Event recording
    """
    from tripwire.types.models import Trigger, ERC3009Transfer  # noqa: F811

    pipeline_start = time.perf_counter()
    timings: dict[str, float] = {}

    is_erc3009 = triggers is None

    # -- 1. DECODE -------------------------------------------------------
    t0 = time.perf_counter()
    # Build list of (DecodedEvent, Trigger | None) pairs
    decoded_pairs: list[tuple[DecodedEvent, Any]] = []

    if is_erc3009:
        with tracer.start_as_current_span("decode") as decode_span:
            try:
                decoded = ERC3009Decoder().decode(raw_log)
                decoded_pairs.append((decoded, None))
                decode_span.set_attribute("decode.status", "ok")
            except Exception as exc:
                logger.exception("unified_decode_failed", raw_log=raw_log)
                decode_span.record_exception(exc)
                decode_span.set_status(StatusCode.ERROR, str(exc))
                decode_span.set_attribute("decode.status", "error")
                return {"status": "error", "reason": "decode_failed"}
    else:
        for trigger in triggers:
            trigger: Trigger
            try:
                decoded = AbiGenericDecoder(abi_fragment=trigger.abi).decode(raw_log)
            except Exception:
                logger.exception(
                    "unified_decode_failed",
                    trigger_id=trigger.id,
                    tx_hash=raw_log.get("transaction_hash", ""),
                )
                continue

            # -- 2. FILTER -----------------------------------------------
            passed, reason = evaluate_filters(decoded.fields, trigger.filter_rules)
            if not passed:
                logger.info(
                    "unified_filter_rejected",
                    trigger_id=trigger.id,
                    reason=reason,
                )
                continue

            # -- 3. PAYMENT GATING (C3) ----------------------------------
            if trigger.require_payment:
                gate_ok, gate_reason = _check_payment_gate(decoded, trigger)
                if not gate_ok:
                    logger.info(
                        "unified_payment_gate_rejected",
                        trigger_id=trigger.id,
                        tx_hash=decoded.tx_hash,
                        reason=gate_reason,
                    )
                    continue

            decoded_pairs.append((decoded, trigger))

    timings["decode_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    if not decoded_pairs:
        return {
            "status": "filtered",
            "reason": "no_events_after_decode",
            "tx_hash": raw_log.get("transaction_hash", ""),
        }

    # Use the first decoded event for shared context
    primary_decoded, _ = decoded_pairs[0]
    tx_hash = primary_decoded.tx_hash
    chain_id = primary_decoded.chain_id

    structlog.contextvars.bind_contextvars(tx_hash=tx_hash, chain_id=chain_id)

    # -- 4. DEDUPLICATION ------------------------------------------------
    t0 = time.perf_counter()
    active_pairs: list[tuple[DecodedEvent, Any]] = []

    for decoded, trigger in decoded_pairs:
        if trigger is not None:
            # Dynamic triggers: tx_hash:log_index:trigger_id
            dedup_key = f"{decoded.tx_hash}:{decoded.log_index}:{trigger.id}"
            dedup_authorizer = decoded.identity_address or "dynamic_trigger"
        elif is_erc3009 and decoded.typed_model is not None:
            # ERC-3009: match legacy dedup keys exactly
            transfer = decoded.typed_model
            is_plain_transfer = not transfer.authorizer
            dedup_key = (
                transfer.nonce if not is_plain_transfer
                else f"{transfer.tx_hash}:{transfer.log_index}"
            )
            dedup_authorizer = (
                transfer.authorizer if not is_plain_transfer
                else "transfer"
            )
        else:
            dedup_key = decoded.dedup_key or f"{decoded.tx_hash}:{decoded.log_index}"
            dedup_authorizer = decoded.identity_address or "transfer"

        if is_erc3009 and decoded.identity_address:
            # ERC-3009: use correlation-aware dedup
            try:
                is_new, existing_event_id, existing_source = await asyncio.to_thread(
                    processor._nonce_repo.record_nonce_or_correlate,
                    chain_id=chain_id or 0,
                    nonce=dedup_key,
                    authorizer=dedup_authorizer,
                    source="goldsky",
                )
            except Exception:
                logger.exception("unified_dedup_failed", tx_hash=tx_hash)
                return {"status": "error", "reason": "dedup_failed", "tx_hash": tx_hash}

            if not is_new:
                if existing_source == "facilitator" and existing_event_id:
                    logger.info(
                        "promoting_pre_confirmed_event",
                        tx_hash=tx_hash,
                        existing_event_id=existing_event_id,
                    )
                    transfer = decoded.typed_model
                    return await processor._promote_pre_confirmed_event(
                        existing_event_id=existing_event_id,
                        transfer=transfer,
                        timings=timings,
                        pipeline_start=pipeline_start,
                    )
                logger.info("unified_duplicate", tx_hash=tx_hash, dedup_key=dedup_key)
                continue
        else:
            # Dynamic triggers: simple dedup
            try:
                is_new = await asyncio.to_thread(
                    processor._nonce_repo.record_nonce,
                    chain_id=chain_id or 0,
                    nonce=dedup_key,
                    authorizer=dedup_authorizer,
                )
            except Exception:
                logger.exception(
                    "unified_dedup_failed",
                    trigger_id=trigger.id if trigger else None,
                    tx_hash=tx_hash,
                )
                continue

            if not is_new:
                logger.info("unified_duplicate", tx_hash=tx_hash, dedup_key=dedup_key)
                continue

        active_pairs.append((decoded, trigger))

    timings["dedup_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    if not active_pairs:
        return {"status": "duplicate", "tx_hash": tx_hash}

    # -- 5. FINALITY + IDENTITY (parallel) --------------------------------
    t0 = time.perf_counter()

    async def _do_finality():
        if not primary_decoded.block_number or not primary_decoded.chain_id:
            return None
        try:
            return await check_finality_generic(
                chain_id=primary_decoded.chain_id,
                block_number=primary_decoded.block_number,
                tx_hash=primary_decoded.tx_hash,
            )
        except Exception:
            logger.exception("unified_finality_failed", tx_hash=tx_hash)
            return None

    identity_address = primary_decoded.identity_address

    async def _do_identity():
        if not identity_address:
            return None
        with tracer.start_as_current_span("identity") as id_span:
            try:
                result = await processor._resolver.resolve(
                    identity_address, chain_id or 0
                )
                id_span.set_attribute("identity.status", "ok" if result else "empty")
                return result
            except Exception as exc:
                logger.warning("unified_identity_failed", address=identity_address)
                id_span.record_exception(exc)
                id_span.set_status(StatusCode.ERROR, str(exc))
                id_span.set_attribute("identity.status", "error")
                return None

    finality, identity = await asyncio.gather(_do_finality(), _do_identity())
    parallel_ms = round((time.perf_counter() - t0) * 1000, 2)
    timings["finality_ms"] = parallel_ms
    timings["identity_ms"] = parallel_ms

    # -- 6. REPUTATION GATING --------------------------------------------
    gated_pairs: list[tuple[DecodedEvent, Any]] = []
    for decoded, trigger in active_pairs:
        if trigger is not None and trigger.reputation_threshold > 0:
            rep = identity.reputation_score if identity else 0.0
            if rep < trigger.reputation_threshold:
                logger.info(
                    "unified_reputation_rejected",
                    trigger_id=trigger.id,
                    reputation=rep,
                    threshold=trigger.reputation_threshold,
                )
                continue
        gated_pairs.append((decoded, trigger))

    if not gated_pairs:
        return {"status": "filtered", "reason": "reputation_below_threshold", "tx_hash": tx_hash}

    # -- 7. ENDPOINT RESOLUTION + POLICY + DISPATCH ----------------------
    if is_erc3009:
        # ERC-3009: delegate to the existing _dispatch_for_transfer
        transfer = primary_decoded.typed_model
        return await processor._dispatch_for_transfer(
            transfer=transfer,
            finality=finality,
            identity=identity,
            timings=timings,
            pipeline_start=pipeline_start,
        )

    # Dynamic triggers: resolve per-trigger endpoints and dispatch
    results: list[dict[str, Any]] = []

    for decoded, trigger in gated_pairs:
        trigger_id = trigger.id

        # Fetch endpoint
        try:
            endpoint = await asyncio.to_thread(
                processor._endpoint_repo.get_by_id, trigger.endpoint_id
            )
        except Exception:
            logger.exception(
                "unified_endpoint_fetch_failed",
                trigger_id=trigger_id,
                endpoint_id=trigger.endpoint_id,
            )
            results.append({"trigger_id": trigger_id, "status": "error", "reason": "endpoint_fetch_failed"})
            continue

        if not endpoint or not endpoint.active:
            results.append({"trigger_id": trigger_id, "status": "skipped", "reason": "endpoint_inactive"})
            continue

        # Policy evaluation (full policy engine)
        t0 = time.perf_counter()
        with tracer.start_as_current_span("policy") as policy_span:
            policies = endpoint.policies or EndpointPolicies()
            from tripwire.types.models import TransferData, ChainId as _ChainId
            try:
                policy_chain = _ChainId(decoded.chain_id) if decoded.chain_id else _ChainId.BASE
            except ValueError:
                policy_chain = _ChainId.BASE
            transfer_data = TransferData(
                chain_id=policy_chain,
                tx_hash=decoded.tx_hash,
                block_number=decoded.block_number,
                from_address=decoded.payment_from or decoded.identity_address or "",
                to_address=decoded.payment_to or decoded.contract_address,
                amount=decoded.payment_amount or "0",
                nonce=decoded.dedup_key or "",
                token=decoded.payment_token or decoded.contract_address,
            )
            allowed, reason = evaluate_policy(transfer_data, identity, policies)
            policy_span.set_attribute("policy.allowed", allowed)

        if not allowed:
            logger.info(
                "unified_policy_rejected",
                trigger_id=trigger_id,
                tx_hash=tx_hash,
                reason=reason,
            )
            results.append({"trigger_id": trigger_id, "status": "filtered", "reason": f"policy: {reason}"})
            continue

        # Finality depth gating for dynamic triggers
        if finality is not None:
            ep_policies = endpoint.policies or EndpointPolicies()
            ep_depth = ep_policies.finality_depth
            required = ep_depth if ep_depth is not None else FINALITY_DEPTHS.get(
                transfer_data.chain_id, 3
            )
            if finality.confirmations < required:
                logger.info(
                    "unified_finality_deferred",
                    trigger_id=trigger_id,
                    endpoint_id=endpoint.id,
                    confirmations=finality.confirmations,
                    required=required,
                )
                results.append({"trigger_id": trigger_id, "status": "deferred", "reason": "finality_pending"})
                continue

        # Determine execution state
        from tripwire.types.models import (
            ExecutionState,
            TrustSource,
            derive_execution_metadata,
            build_finality_data,
        )
        finality_data = build_finality_data(finality)
        if finality and finality.is_finalized:
            exec_state = ExecutionState.FINALIZED
            safe = True
            trust = TrustSource.ONCHAIN
        else:
            exec_state = ExecutionState.CONFIRMED
            safe = False
            trust = TrustSource.ONCHAIN

        # Build TWSS-1 compliant payload with nested execution block
        event_id = str(uuid.uuid4())
        event_type_str = WebhookEventType.TRIGGER_MATCHED.value
        execution_block = {
            "state": exec_state.value,
            "safe_to_execute": safe,
            "trust_source": trust.value,
            "finality": (
                {
                    "confirmations": finality_data.confirmations,
                    "required_confirmations": finality_data.required_confirmations,
                    "is_finalized": finality_data.is_finalized,
                }
                if finality_data
                else None
            ),
        }
        identity_dict = identity.model_dump() if identity else None
        payload = {
            "id": event_id,
            "idempotency_key": f"dyn_{tx_hash}_{decoded.log_index}_{trigger_id}",
            "type": event_type_str,
            "mode": endpoint.mode.value,
            "timestamp": int(time.time()),
            "version": "v1",
            "execution": execution_block,
            "trigger_id": trigger_id,
            "data": {
                "event": decoded.fields,
                "identity": identity_dict,
            },
        }

        # Dispatch
        message_id = None
        if endpoint.mode == EndpointMode.EXECUTE and endpoint.convoy_project_id:
            try:
                message_id = await processor._webhook_provider.send(
                    app_id=endpoint.convoy_project_id,
                    event_type=event_type_str,
                    payload=payload,
                )
            except Exception:
                logger.exception(
                    "unified_dispatch_failed",
                    trigger_id=trigger_id,
                    tx_hash=tx_hash,
                )

        # Notify mode -- use realtime_events table (same as legacy path)
        notify_event_ids: list[str] = []
        if endpoint.mode == EndpointMode.NOTIFY:
            # Check subscription filters
            subs = processor._fetch_subscriptions(endpoint.id)
            should_notify = True
            if subs and chain_id is not None:
                should_notify = any(
                    (not s.filters.chains or (chain_id in s.filters.chains))
                    for s in subs
                )
            if should_notify:
                try:
                    notify_id = str(uuid.uuid4())
                    now_ts = datetime.now(timezone.utc).isoformat()
                    notify_row = {
                        "id": notify_id,
                        "endpoint_id": endpoint.id,
                        "type": event_type_str,
                        "data": payload,
                        "chain_id": chain_id,
                        "recipient": decoded.payment_to or decoded.contract_address,
                        "created_at": now_ts,
                    }
                    if processor._sb:
                        processor._sb.table("realtime_events").insert(notify_row).execute()
                        notify_event_ids.append(notify_id)
                except Exception:
                    logger.exception(
                        "unified_notify_failed",
                        trigger_id=trigger_id,
                        endpoint_id=endpoint.id,
                    )

        # Record event
        now = datetime.now(timezone.utc).isoformat()
        row: dict[str, Any] = {
            "id": event_id,
            "type": event_type_str,
            "data": decoded.fields,
            "created_at": now,
            "chain_id": chain_id,
            "tx_hash": tx_hash,
            "block_number": decoded.block_number,
            "block_hash": decoded.block_hash,
            "log_index": decoded.log_index,
            "from_address": decoded.identity_address or "",
            "to_address": decoded.contract_address,
            "status": exec_state.value,
            "endpoint_id": trigger.endpoint_id,
            "finality_depth": finality.confirmations if finality else 0,
            # New schema columns (migration 026)
            "event_type": trigger.webhook_event_type or event_type_str,
            "decoded_fields": decoded.fields,
            "trigger_id": trigger_id,
            "product_source": "pulse",
            "source": "onchain",
        }
        if identity:
            row["identity_data"] = identity.model_dump()

        try:
            processor._event_repo.insert(row)
            processor._event_repo.link_endpoints(event_id, [trigger.endpoint_id])
        except Exception:
            logger.exception("unified_event_record_failed", event_id=event_id)

        if message_id:
            processor._record_delivery(
                endpoint_id=endpoint.id,
                event_id=event_id,
                provider_message_id=message_id,
            )

        results.append({
            "trigger_id": trigger_id,
            "status": "processed",
            "event_id": event_id,
            "message_id": message_id,
            "execution_state": exec_state.value,
            "notify_sent": len(notify_event_ids),
        })

    total_ms = round((time.perf_counter() - pipeline_start) * 1000, 2)
    timings["total_ms"] = total_ms
    processed_count = sum(1 for r in results if r.get("status") == "processed")

    record_pipeline_timing(
        timings,
        chain_id=chain_id or 0,
        status="processed" if processed_count > 0 else "filtered",
    )

    logger.info(
        "unified_event_processed",
        tx_hash=tx_hash,
        triggers_evaluated=len(triggers) if triggers else 0,
        triggers_dispatched=processed_count,
        **timings,
    )

    return {
        "status": "processed" if processed_count > 0 else "filtered",
        "tx_hash": tx_hash,
        "triggers_evaluated": len(triggers) if triggers else 0,
        "triggers_dispatched": processed_count,
        "results": results,
    }


# ── C3: Payment gating check ─────────────────────────────────────


def _check_payment_gate(
    decoded: DecodedEvent, trigger: Any
) -> tuple[bool, str]:
    """Check if a DecodedEvent meets a trigger's payment gating requirements.

    Returns (passed, reason).  If the trigger does not require payment
    gating, returns (True, "").
    """
    if not trigger.require_payment:
        return True, ""

    # Check payment metadata on the DecodedEvent (populated by decoders)
    if decoded.payment_amount is None:
        return False, "no_payment_data"

    # Token check
    if trigger.payment_token:
        if not decoded.payment_token:
            return False, "no_payment_token"
        if decoded.payment_token.lower() != trigger.payment_token.lower():
            return False, f"wrong_token:{decoded.payment_token}"

    # Amount check
    if trigger.min_payment_amount:
        try:
            actual = int(decoded.payment_amount)
            required = int(trigger.min_payment_amount)
            if actual < required:
                return False, f"amount_below_minimum:{actual}<{required}"
        except (ValueError, TypeError):
            return False, "invalid_payment_amount"

    return True, ""
