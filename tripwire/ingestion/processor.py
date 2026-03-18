"""End-to-end ingestion pipeline orchestrator.

Receives a raw onchain event and runs the full pipeline:
  detect → decode → evaluate → dispatch

The processor is event-type agnostic: it detects the event type from
the raw log's topic signature and routes to the appropriate handler.
Currently supported event types:
  - erc3009_transfer: ERC-3009 TransferWithAuthorization payments

New event types (DeFi pool state changes, whale alerts, etc.) can be
added by implementing a new ``_process_<type>_event`` method and
registering its topic signature(s) in ``_EVENT_SIGNATURES``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

from tripwire.observability.tracing import tracer, StatusCode
from tripwire.api.policies.engine import evaluate_policy
from tripwire.db.repositories.endpoints import EndpointRepository
from tripwire.db.repositories.events import EventRepository
from tripwire.db.repositories.nonces import NonceRepository
from tripwire.db.repositories.webhooks import WebhookDeliveryRepository
from tripwire.identity.resolver import IdentityResolver
from tripwire.ingestion.decoder import (
    AUTHORIZATION_USED_TOPIC,
    TRANSFER_TOPIC,
    _parse_topics,
    decode_transfer_event,
)
from tripwire.ingestion.finality import check_finality, check_finality_generic
from tripwire.notify.realtime import RealtimeNotifier
from tripwire.types.models import (
    FINALITY_DEPTHS,
    Endpoint,
    EndpointMode,
    EndpointPolicies,
    Subscription,
    WebhookEventType,
)
from tripwire.webhook.dispatcher import (
    build_transfer_data,
    dispatch_event,
    match_endpoints,
    match_subscriptions,
)
from tripwire.observability.metrics import record_pipeline_timing
from tripwire.webhook.provider import WebhookProvider
from tripwire.db.repositories.triggers import TriggerRepository
from tripwire.ingestion.generic_decoder import decode_event_with_abi
from tripwire.ingestion.filter_engine import evaluate_filters
from tripwire.ingestion.decoders import ERC3009Decoder, AbiGenericDecoder
from tripwire.ingestion.decoders.protocol import DecodedEvent
from tripwire.config.settings import settings

logger = structlog.get_logger(__name__)

# ── Event Signature Registry ───────────────────────────────────
# Maps known topic0 signatures to their canonical event type string.
# To add a new event type, register its topic0 here and implement a
# corresponding ``_process_<type>_event`` handler on EventProcessor.

_EVENT_SIGNATURES: dict[str, str] = {
    AUTHORIZATION_USED_TOPIC.lower(): "erc3009_transfer",
    TRANSFER_TOPIC.lower(): "erc3009_transfer",
}

# ── Endpoint cache ───────────────────────────────────────────────
# In-memory TTL cache for endpoint lookups. Endpoints change rarely
# (only at registration time), so a 30s TTL is safe.
_endpoint_cache: dict[str, tuple[float, list]] = {}
ENDPOINT_CACHE_TTL = 30  # seconds


class EventProcessor:
    """Orchestrates the full ingestion→dispatch pipeline.

    The processor is generic: ``process_event`` detects the event type from
    the raw log and delegates to the appropriate type-specific handler.
    Generic stages (endpoint matching, policy evaluation, webhook dispatch,
    identity enrichment) are shared across all event types.
    """

    def __init__(
        self,
        endpoint_repo: EndpointRepository,
        event_repo: EventRepository,
        nonce_repo: NonceRepository,
        delivery_repo: WebhookDeliveryRepository,
        identity_resolver: IdentityResolver,
        realtime_notifier: RealtimeNotifier,
        webhook_provider: WebhookProvider,
        supabase_client: Any | None = None,
        trigger_repo: TriggerRepository | None = None,
    ) -> None:
        self._endpoint_repo = endpoint_repo
        self._event_repo = event_repo
        self._nonce_repo = nonce_repo
        self._delivery_repo = delivery_repo
        self._resolver = identity_resolver
        self._realtime_notifier = realtime_notifier
        self._webhook_provider = webhook_provider
        # Supabase client for subscription queries; falls back to endpoint_repo's client
        self._sb = supabase_client or getattr(endpoint_repo, "_sb", None)
        self._trigger_repo = trigger_repo

    # ── Public entry point ────────────────────────────────────────

    async def process_event(self, raw_log: dict[str, Any]) -> dict[str, Any]:
        """Process a single onchain event through the full pipeline.

        Detects the event type from the raw log's topic signature, then
        delegates to the appropriate type-specific handler.  Returns a
        summary dict with processing results.

        When ``UNIFIED_PROCESSOR=true``, both ERC-3009 and dynamic triggers
        flow through ``_process_unified`` (Phase C2).
        """
        with tracer.start_as_current_span("process_event") as span:
            detection = self._detect_event_type(raw_log)

            if settings.unified_processor:
                # ── C2: Unified processing loop ──────────────────────
                if isinstance(detection, tuple):
                    _, triggers = detection
                    span.set_attribute("event.type", "dynamic")
                    span.set_attribute("event.trigger_count", len(triggers))
                    result = await self._process_unified(raw_log, triggers=triggers)
                elif detection == "erc3009_transfer":
                    span.set_attribute("event.type", detection)
                    result = await self._process_unified(raw_log, triggers=None)
                else:
                    span.set_attribute("event.type", detection)
                    logger.warning("unknown_event_type", event_type=detection, raw_log=raw_log)
                    result = {"status": "skipped", "reason": f"unknown_event_type: {detection}"}
            else:
                # ── Legacy split paths ───────────────────────────────
                if isinstance(detection, tuple):
                    event_type_label, triggers = detection
                    span.set_attribute("event.type", event_type_label)
                    span.set_attribute("event.trigger_count", len(triggers))
                    result = await self._process_dynamic_event(raw_log, triggers)
                elif detection == "erc3009_transfer":
                    span.set_attribute("event.type", detection)
                    result = await self._process_erc3009_event(raw_log)
                else:
                    span.set_attribute("event.type", detection)
                    logger.warning("unknown_event_type", event_type=detection, raw_log=raw_log)
                    result = {"status": "skipped", "reason": f"unknown_event_type: {detection}"}

            span.set_attribute("event.status", result.get("status", "unknown"))
            if "tx_hash" in result:
                span.set_attribute("event.tx_hash", result["tx_hash"])
            return result

    # ── Pre-confirmed (facilitator fast path) ────────────────────

    async def process_pre_confirmed_event(
        self, transfer: "ERC3009Transfer"
    ) -> dict[str, Any]:
        """Process a pre-confirmed payment from the x402 facilitator.

        This is the fast path (~100ms target).  The facilitator has already
        verified the ERC-3009 signature but the transaction is NOT yet onchain.
        We skip decode (data is already structured) and finality (no tx yet)
        but still run: nonce dedup, identity resolution, policy evaluation,
        and dispatch.
        """
        from tripwire.types.models import ERC3009Transfer  # noqa: F811

        pipeline_start = time.perf_counter()
        timings: dict[str, float] = {}

        chain_id = transfer.chain_id

        structlog.contextvars.bind_contextvars(
            tx_hash=transfer.tx_hash, chain_id=chain_id.value
        )

        # Generate event_id upfront so we can pass it to nonce recording
        # for later correlation when Goldsky delivers the real onchain event.
        event_id = str(uuid.uuid4())

        logger.info(
            "processing_pre_confirmed",
            event_type="payment.pre_confirmed",
            event_id=event_id,
            authorizer=transfer.authorizer,
        )

        # 1. Nonce deduplication (with correlation support)
        t0 = time.perf_counter()
        try:
            is_new, existing_event_id, existing_source = await asyncio.to_thread(
                self._nonce_repo.record_nonce_or_correlate,
                chain_id=chain_id.value,
                nonce=transfer.nonce,
                authorizer=transfer.authorizer,
                event_id=event_id,
                source="facilitator",
            )
        except Exception:
            logger.exception("nonce_dedup_failed", tx_hash=transfer.tx_hash)
            return {"status": "error", "reason": "nonce_dedup_failed", "tx_hash": transfer.tx_hash}
        timings["dedup_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        if not is_new:
            logger.info("duplicate_nonce", tx_hash=transfer.tx_hash, nonce=transfer.nonce)
            return {"status": "duplicate", "tx_hash": transfer.tx_hash}

        # 2. Identity resolution (no finality check — tx not yet onchain)
        t0 = time.perf_counter()
        try:
            identity = await self._resolver.resolve(
                transfer.authorizer, chain_id.value
            )
        except Exception:
            logger.warning("identity_resolve_failed", authorizer=transfer.authorizer)
            identity = None
        timings["identity_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        # 3. Dispatch through generic pipeline with pre_confirmed event type
        #    finality=None signals no onchain confirmation yet.
        #    Pass the pre-generated event_id so it correlates with the nonce record.
        return await self._dispatch_for_transfer(
            transfer=transfer,
            finality=None,
            identity=identity,
            timings=timings,
            pipeline_start=pipeline_start,
            event_type_override=WebhookEventType.PAYMENT_PRE_CONFIRMED,
            event_id=event_id,
        )

    # ── Event type detection ───────────────────────────────────────

    def _detect_event_type(self, raw_log: dict[str, Any]) -> str | tuple[str, list]:
        """Identify the canonical event type from the raw log's topic0.

        Checks the first topic against ``_EVENT_SIGNATURES``.  Handles both
        list topics (Mirror) and comma-separated string topics (Turbo).
        Returns a human-readable event type string, a tuple of
        ``("dynamic", triggers)`` for dynamic triggers, or ``"unknown"``.
        """
        topics = _parse_topics(raw_log.get("topics", []))
        if not topics:
            return "unknown"

        topic0 = topics[0].lower()

        hardcoded = _EVENT_SIGNATURES.get(topic0)
        if hardcoded is not None:
            return hardcoded

        # Fallback: check dynamic trigger registry
        if self._trigger_repo is not None:
            chain_id = raw_log.get("chain_id")
            contract = raw_log.get("address", "").lower() or None
            triggers = self._trigger_repo.find_by_topic(topic0)
            # Filter by chain_id and contract_address locally
            matched = []
            for t in triggers:
                if t.chain_ids and chain_id is not None and chain_id not in t.chain_ids:
                    continue
                if t.contract_address and contract and t.contract_address.lower() != contract:
                    continue
                matched.append(t)
            if matched:
                return ("dynamic", matched)

        return "unknown"

    # ── ERC-3009 handler ───────────────────────────────────────────

    async def _process_erc3009_event(self, raw_log: dict[str, Any]) -> dict[str, Any]:
        """ERC-3009 TransferWithAuthorization handler.

        Runs the type-specific stages (decode, nonce dedup, finality,
        identity) and then hands off to the generic dispatch pipeline.
        """
        pipeline_start = time.perf_counter()
        timings: dict[str, float] = {}

        # 1. Decode the raw event into an ERC3009Transfer
        t0 = time.perf_counter()
        with tracer.start_as_current_span("decode") as decode_span:
            try:
                decoded_event = ERC3009Decoder().decode(raw_log)
                transfer = decoded_event.typed_model
            except Exception as exc:
                logger.exception("decode_failed", raw_log=raw_log)
                decode_span.record_exception(exc)
                decode_span.set_status(StatusCode.ERROR, str(exc))
                decode_span.set_attribute("decode.status", "error")
                return {"status": "error", "reason": "decode_failed"}
            decode_span.set_attribute("decode.status", "ok")
        timings["decode_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        tx_hash = transfer.tx_hash
        chain_id = transfer.chain_id

        # Bind to structlog contextvars so all downstream loggers include these
        structlog.contextvars.bind_contextvars(
            tx_hash=tx_hash, chain_id=chain_id.value
        )

        logger.info(
            "processing_event",
            event_type="erc3009_transfer",
            authorizer=transfer.authorizer,
        )

        # 2. Deduplication (with facilitator correlation)
        #    ERC-3009: use authorizer + nonce (unique per authorization)
        #    Plain Transfer: use tx_hash + log_index (no meaningful nonce)
        t0 = time.perf_counter()
        is_plain_transfer = not transfer.authorizer
        dedup_nonce = transfer.nonce if not is_plain_transfer else f"{tx_hash}:{transfer.log_index}"
        dedup_authorizer = transfer.authorizer if not is_plain_transfer else "transfer"
        try:
            is_new, existing_event_id, existing_source = await asyncio.to_thread(
                self._nonce_repo.record_nonce_or_correlate,
                chain_id=chain_id.value,
                nonce=dedup_nonce,
                authorizer=dedup_authorizer,
                source="goldsky",
            )
        except Exception:
            logger.exception("nonce_dedup_failed", tx_hash=tx_hash)
            return {"status": "error", "reason": "nonce_dedup_failed", "tx_hash": tx_hash}
        timings["dedup_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        if not is_new:
            # Check if this is a facilitator-claimed nonce that needs promotion
            if existing_source == "facilitator" and existing_event_id:
                logger.info(
                    "promoting_pre_confirmed_event",
                    tx_hash=tx_hash,
                    existing_event_id=existing_event_id,
                    nonce=dedup_nonce,
                )
                return await self._promote_pre_confirmed_event(
                    existing_event_id=existing_event_id,
                    transfer=transfer,
                    timings=timings,
                    pipeline_start=pipeline_start,
                )
            # True duplicate — same source or no correlation info
            logger.info("duplicate_nonce", tx_hash=tx_hash, nonce=dedup_nonce)
            return {"status": "duplicate", "tx_hash": tx_hash}

        # 3-4. Finality check and identity resolution
        #       run in parallel — they have ZERO data dependencies on each other.
        t0 = time.perf_counter()

        async def _do_finality():
            try:
                return await check_finality(transfer)
            except Exception:
                logger.exception("finality_check_failed", tx_hash=tx_hash)
                return None

        async def _do_identity():
            with tracer.start_as_current_span("identity") as id_span:
                try:
                    result = await self._resolver.resolve(
                        transfer.authorizer, chain_id.value
                    )
                    id_span.set_attribute("identity.status", "ok" if result else "empty")
                    return result
                except Exception as exc:
                    logger.warning("identity_resolve_failed", authorizer=transfer.authorizer)
                    id_span.record_exception(exc)
                    id_span.set_status(StatusCode.ERROR, str(exc))
                    id_span.set_attribute("identity.status", "error")
                    return None

        finality, identity = await asyncio.gather(
            _do_finality(), _do_identity()
        )
        parallel_elapsed = time.perf_counter() - t0
        timings["finality_ms"] = round(parallel_elapsed * 1000, 2)
        timings["identity_ms"] = round(parallel_elapsed * 1000, 2)

        # 5–8. Generic stages: endpoint matching → policy → dispatch
        return await self._dispatch_for_transfer(
            transfer=transfer,
            finality=finality,
            identity=identity,
            timings=timings,
            pipeline_start=pipeline_start,
        )

    # ── Dynamic trigger handler ─────────────────────────────────────

    async def _process_dynamic_event(
        self, raw_log: dict[str, Any], triggers: list
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
                    self._nonce_repo.record_nonce,
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

            # 4. Identity resolution — use first address field found
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
                    identity = await self._resolver.resolve(
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
                    self._endpoint_repo.get_by_id, trigger.endpoint_id
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
            event_type_str = trigger.webhook_event_type
            payload = {
                "id": str(uuid.uuid4()),
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
                "data": decoded,
            }

            message_id = None
            if endpoint.convoy_project_id:
                try:
                    message_id = await self._webhook_provider.send(
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
            event_id = str(uuid.uuid4())
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
            }
            if identity:
                row["identity_data"] = identity.model_dump()
            try:
                self._event_repo.insert(row)
                # Link endpoint via join table (#7)
                self._event_repo.link_endpoints(event_id, [trigger.endpoint_id])
            except Exception:
                logger.exception("dynamic_event_record_failed", event_id=event_id)

            # Record delivery
            if message_id:
                self._record_delivery(
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

    # ── C2: Unified processing loop ──────────────────────────────────

    async def _process_unified(
        self,
        raw_log: dict[str, Any],
        triggers: list | None = None,
    ) -> dict[str, Any]:
        """Unified processing loop for all event types (Phase C2).

        Single code path for ERC-3009 and dynamic trigger events.
        Uses DecodedEvent as the uniform data structure throughout.

        Steps:
          1. Decode (ERC3009Decoder or AbiGenericDecoder per trigger)
          2. Filter (trigger filter_rules, if applicable)
          3. Payment gating (C3 — check DecodedEvent payment metadata)
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

        # ── 1. DECODE ────────────────────────────────────────────────
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

                # ── 2. FILTER ────────────────────────────────────────
                passed, reason = evaluate_filters(decoded.fields, trigger.filter_rules)
                if not passed:
                    logger.info(
                        "unified_filter_rejected",
                        trigger_id=trigger.id,
                        reason=reason,
                    )
                    continue

                # ── 3. PAYMENT GATING (C3) ───────────────────────────
                if trigger.require_payment:
                    gate_ok, gate_reason = self._check_payment_gate(decoded, trigger)
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

        # ── 4. DEDUPLICATION ─────────────────────────────────────────
        t0 = time.perf_counter()
        active_pairs: list[tuple[DecodedEvent, Any]] = []

        for decoded, trigger in decoded_pairs:
            if trigger is not None:
                # Dynamic triggers: tx_hash:log_index:trigger_id
                dedup_key = f"{decoded.tx_hash}:{decoded.log_index}:{trigger.id}"
                dedup_authorizer = decoded.identity_address or "dynamic_trigger"
            elif is_erc3009 and decoded.typed_model is not None:
                # ERC-3009: match legacy dedup keys exactly
                # AuthorizationUsed: nonce=transfer.nonce, authorizer=transfer.authorizer
                # Plain Transfer: nonce=tx_hash:log_index, authorizer="transfer"
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
                        self._nonce_repo.record_nonce_or_correlate,
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
                        return await self._promote_pre_confirmed_event(
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
                        self._nonce_repo.record_nonce,
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

        # ── 5. FINALITY + IDENTITY (parallel) ────────────────────────
        t0 = time.perf_counter()

        async def _do_finality():
            # Unified finality: check_finality_generic works for all event types
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
                    result = await self._resolver.resolve(
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

        # ── 6. REPUTATION GATING ─────────────────────────────────────
        # (applies to dynamic triggers with reputation_threshold)
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

        # ── 7. ENDPOINT RESOLUTION + POLICY + DISPATCH ───────────────
        if is_erc3009:
            # ERC-3009: delegate to the existing _dispatch_for_transfer
            # which handles endpoint matching, policy, dispatch, and recording
            transfer = primary_decoded.typed_model
            return await self._dispatch_for_transfer(
                transfer=transfer,
                finality=finality,
                identity=identity,
                timings=timings,
                pipeline_start=pipeline_start,
            )

        # Dynamic triggers: resolve per-trigger endpoints and dispatch
        results: list[dict[str, Any]] = []

        for decoded, trigger in gated_pairs:
            trigger: Trigger
            trigger_id = trigger.id

            # Fetch endpoint
            try:
                endpoint = await asyncio.to_thread(
                    self._endpoint_repo.get_by_id, trigger.endpoint_id
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

            # Policy evaluation (full policy engine — upgrade over legacy path)
            t0 = time.perf_counter()
            with tracer.start_as_current_span("policy") as policy_span:
                policies = endpoint.policies or EndpointPolicies()
                # Build transfer-like data from DecodedEvent for policy engine
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
            event_type_str = trigger.webhook_event_type
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
            payload = {
                "id": event_id,
                "idempotency_key": f"dyn_{tx_hash}_{decoded.log_index}_{trigger_id}",
                "type": event_type_str,
                "mode": endpoint.mode.value,
                "timestamp": int(time.time()),
                "version": "v1",
                "execution": execution_block,
                "trigger_id": trigger_id,
                "data": decoded.fields,
            }

            if identity:
                payload["identity"] = identity.model_dump()

            # Dispatch
            message_id = None
            if endpoint.mode == EndpointMode.EXECUTE and endpoint.convoy_project_id:
                try:
                    message_id = await self._webhook_provider.send(
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

            # Notify mode — use realtime_events table (same as legacy path)
            notify_event_ids: list[str] = []
            if endpoint.mode == EndpointMode.NOTIFY:
                # Check subscription filters
                subs = self._fetch_subscriptions(endpoint.id)
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
                        if self._sb:
                            self._sb.table("realtime_events").insert(notify_row).execute()
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
            }
            if identity:
                row["identity_data"] = identity.model_dump()

            try:
                self._event_repo.insert(row)
                self._event_repo.link_endpoints(event_id, [trigger.endpoint_id])
            except Exception:
                logger.exception("unified_event_record_failed", event_id=event_id)

            if message_id:
                self._record_delivery(
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

    @staticmethod
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

    # ── Facilitator → Goldsky promotion ─────────────────────────────

    async def _promote_pre_confirmed_event(
        self,
        existing_event_id: str,
        transfer,
        timings: dict[str, float],
        pipeline_start: float,
    ) -> dict[str, Any]:
        """Promote a pre_confirmed event to confirmed when Goldsky delivers the real tx.

        Called when Goldsky delivers an onchain event whose nonce was already
        claimed by the facilitator fast path.  Updates the event row with real
        onchain data (tx_hash, block_number, block_hash, log_index), checks
        finality, and dispatches the appropriate webhook (payment.confirmed or
        payment.finalized).
        """
        tx_hash = transfer.tx_hash

        # 1. Promote the event row with real onchain data
        try:
            updated = await asyncio.to_thread(
                self._event_repo.promote_to_confirmed,
                event_id=existing_event_id,
                tx_hash=tx_hash,
                block_number=transfer.block_number,
                block_hash=transfer.block_hash,
                log_index=transfer.log_index,
            )
        except Exception:
            logger.exception(
                "promote_to_confirmed_failed",
                event_id=existing_event_id,
                tx_hash=tx_hash,
            )
            return {"status": "error", "reason": "promote_failed", "tx_hash": tx_hash}

        if not updated:
            logger.warning(
                "promote_event_not_found",
                event_id=existing_event_id,
                tx_hash=tx_hash,
            )
            return {"status": "error", "reason": "event_not_found", "tx_hash": tx_hash}

        # 2. Check finality on the real block
        t0 = time.perf_counter()
        try:
            finality = await check_finality(transfer)
        except Exception:
            logger.exception("finality_check_failed", tx_hash=tx_hash)
            finality = None
        timings["finality_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        # 3. Determine webhook type based on finality
        if finality and finality.is_finalized:
            webhook_event_type = WebhookEventType.PAYMENT_FINALIZED
        else:
            webhook_event_type = WebhookEventType.PAYMENT_CONFIRMED

        # 4. Identity resolution
        t0 = time.perf_counter()
        try:
            identity = await self._resolver.resolve(
                transfer.authorizer, transfer.chain_id.value
            )
        except Exception:
            logger.warning("identity_resolve_failed", authorizer=transfer.authorizer)
            identity = None
        timings["identity_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        # 5. Fetch linked endpoints from join table
        try:
            endpoint_ids = await asyncio.to_thread(
                self._event_repo.get_endpoint_ids, existing_event_id
            )
        except Exception:
            logger.exception(
                "promote_endpoint_ids_fetch_failed",
                event_id=existing_event_id,
            )
            endpoint_ids = []
            # Fall back to legacy endpoint_id column
            legacy_id = updated.get("endpoint_id")
            if legacy_id:
                endpoint_ids = [legacy_id]

        if not endpoint_ids:
            logger.info(
                "promote_no_endpoints",
                event_id=existing_event_id,
                tx_hash=tx_hash,
            )
            total_ms = round((time.perf_counter() - pipeline_start) * 1000, 2)
            return {
                "status": "promoted",
                "tx_hash": tx_hash,
                "event_id": existing_event_id,
                "webhook_type": webhook_event_type.value,
                "endpoints_matched": 0,
                "total_ms": total_ms,
            }

        # 6. Fetch full endpoint objects
        endpoints: list[Endpoint] = []
        for eid in endpoint_ids:
            try:
                ep = await asyncio.to_thread(self._endpoint_repo.get_by_id, eid)
                if ep is not None and ep.active:
                    endpoints.append(ep)
            except Exception:
                logger.exception(
                    "promote_endpoint_fetch_failed",
                    endpoint_id=eid,
                )

        if not endpoints:
            total_ms = round((time.perf_counter() - pipeline_start) * 1000, 2)
            return {
                "status": "promoted",
                "tx_hash": tx_hash,
                "event_id": existing_event_id,
                "webhook_type": webhook_event_type.value,
                "endpoints_matched": 0,
                "total_ms": total_ms,
            }

        # 7. Policy evaluation
        from tripwire.webhook.dispatcher import build_transfer_data as _build_td
        transfer_data = _build_td(transfer)
        approved_endpoints: list[Endpoint] = []
        for ep in endpoints:
            policies = ep.policies or EndpointPolicies()
            allowed, reason = evaluate_policy(transfer_data, identity, policies)
            if allowed:
                approved_endpoints.append(ep)
            else:
                logger.info(
                    "promote_policy_rejected",
                    endpoint_id=ep.id,
                    tx_hash=tx_hash,
                    reason=reason,
                )

        # 8. Dispatch webhooks
        t0 = time.perf_counter()
        message_ids: list[str] = []
        execute_endpoints = [
            ep for ep in approved_endpoints if ep.mode == EndpointMode.EXECUTE
        ]
        if execute_endpoints:
            try:
                message_ids = await dispatch_event(
                    transfer=transfer,
                    matched_endpoints=execute_endpoints,
                    provider=self._webhook_provider,
                    event_type=webhook_event_type,
                    finality=finality,
                    identity=identity,
                )
            except Exception:
                logger.exception("promote_dispatch_failed", tx_hash=tx_hash)

            for i, ep in enumerate(execute_endpoints):
                msg_id = message_ids[i] if i < len(message_ids) else None
                self._record_delivery(
                    endpoint_id=ep.id,
                    event_id=existing_event_id,
                    provider_message_id=msg_id,
                )

        # Notify-mode endpoints
        notify_endpoints = [
            ep for ep in approved_endpoints if ep.mode == EndpointMode.NOTIFY
        ]
        notify_event_ids: list[str] = []
        if notify_endpoints:
            try:
                notify_event_ids = await self._realtime_notifier.notify_batch(
                    endpoints=notify_endpoints,
                    transfer=transfer,
                    event_type=webhook_event_type,
                    finality=finality,
                    identity=identity,
                )
            except Exception:
                logger.exception("promote_notify_failed", tx_hash=tx_hash)

        timings["dispatch_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        timings["total_ms"] = round((time.perf_counter() - pipeline_start) * 1000, 2)

        logger.info(
            "event_promoted",
            tx_hash=tx_hash,
            event_id=existing_event_id,
            webhook_type=webhook_event_type.value,
            endpoints_matched=len(endpoints),
            endpoints_approved=len(approved_endpoints),
            webhooks_sent=len(message_ids),
            notify_sent=len(notify_event_ids),
            **timings,
        )

        return {
            "status": "promoted",
            "tx_hash": tx_hash,
            "event_id": existing_event_id,
            "webhook_type": webhook_event_type.value,
            "endpoints_matched": len(endpoints),
            "endpoints_approved": len(approved_endpoints),
            "webhooks_sent": len(message_ids),
            "notify_sent": len(notify_event_ids),
            "message_ids": message_ids,
        }

    # ── Generic dispatch pipeline (shared across event types) ──────

    async def _dispatch_for_transfer(
        self,
        transfer,
        finality,
        identity,
        timings: dict[str, float],
        pipeline_start: float,
        event_type_override: WebhookEventType | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Run the generic stages shared across all transfer-like events.

        Covers: endpoint matching, policy evaluation, event recording,
        webhook dispatch, and realtime notification.

        If *event_type_override* is provided it takes precedence over the
        finality-based type derivation (used by the facilitator fast path).

        If *event_id* is provided, it is used for the event record instead
        of generating a new UUID (used by the facilitator fast path to
        correlate the nonce record with the event).
        """
        tx_hash = transfer.tx_hash

        # 5. Match endpoints for this transfer's recipient + chain
        try:
            endpoints = await asyncio.to_thread(self._fetch_matching_endpoints, transfer)
        except Exception:
            logger.exception("endpoint_fetch_failed", tx_hash=tx_hash)
            return {"status": "error", "reason": "endpoint_fetch_failed", "tx_hash": tx_hash}

        if endpoints is None:
            logger.exception("endpoint_fetch_failed", tx_hash=tx_hash)
            return {"status": "error", "reason": "endpoint_fetch_failed", "tx_hash": tx_hash}
        if not endpoints:
            logger.info("no_matching_endpoints", tx_hash=tx_hash)
            # Still record the event even if no endpoints match
            webhook_event_type = event_type_override or (WebhookEventType.PAYMENT_CONFIRMED if (finality and finality.is_finalized) else WebhookEventType.PAYMENT_PENDING)
            recorded_id = self._record_event(
                transfer, finality, identity, event_type=webhook_event_type, event_id=event_id
            )
            return {
                "status": "no_endpoints",
                "tx_hash": tx_hash,
                "event_id": recorded_id,
            }

        matched = match_endpoints(transfer, endpoints)
        if not matched:
            logger.info("no_endpoints_matched_filters", tx_hash=tx_hash)
            webhook_event_type = event_type_override or (WebhookEventType.PAYMENT_CONFIRMED if (finality and finality.is_finalized) else WebhookEventType.PAYMENT_PENDING)
            recorded_id = self._record_event(
                transfer, finality, identity, event_type=webhook_event_type, event_id=event_id
            )
            return {
                "status": "no_match",
                "tx_hash": tx_hash,
                "event_id": recorded_id,
            }

        # 6. Policy evaluation — filter out endpoints that fail policy
        t0 = time.perf_counter()
        with tracer.start_as_current_span("policy") as policy_span:
            transfer_data = build_transfer_data(transfer)
            approved_endpoints: list[Endpoint] = []

            for ep in matched:
                policies = ep.policies or EndpointPolicies()
                allowed, reason = evaluate_policy(transfer_data, identity, policies)
                if allowed:
                    approved_endpoints.append(ep)
                else:
                    logger.info(
                        "policy_rejected",
                        endpoint_id=ep.id,
                        tx_hash=tx_hash,
                        reason=reason,
                    )
            policy_span.set_attribute("policy.matched", len(matched))
            policy_span.set_attribute("policy.approved", len(approved_endpoints))
        timings["policy_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        # 6b. Finality depth gate — defer endpoints whose configured
        #     finality_depth exceeds the current confirmation count.
        if finality is not None:
            ready_endpoints: list[Endpoint] = []
            for ep in approved_endpoints:
                policies = ep.policies or EndpointPolicies()
                ep_depth = policies.finality_depth
                if ep_depth is not None:
                    required = ep_depth
                else:
                    required = FINALITY_DEPTHS.get(transfer.chain_id, 3)
                if finality.confirmations < required:
                    logger.info(
                        "endpoint_finality_deferred",
                        endpoint_id=ep.id,
                        tx_hash=tx_hash,
                        confirmations=finality.confirmations,
                        required=required,
                    )
                    continue
                ready_endpoints.append(ep)
            approved_endpoints = ready_endpoints

        # Determine event type based on finality (or use override)
        if event_type_override:
            webhook_event_type = event_type_override
        elif finality and finality.is_finalized:
            webhook_event_type = WebhookEventType.PAYMENT_CONFIRMED
        else:
            if finality is None:
                logger.warning("finality_unknown_defaulting_to_pending", tx_hash=tx_hash)
            webhook_event_type = WebhookEventType.PAYMENT_PENDING

        # 7. Record the event and link ALL matched endpoints via join table
        first_endpoint_id = matched[0].id if matched else None
        event_id = self._record_event(transfer, finality, identity, webhook_event_type, endpoint_id=first_endpoint_id, event_id=event_id)

        # Link all matched endpoints via the event_endpoints join table (#7)
        all_endpoint_ids = [ep.id for ep in matched]
        if all_endpoint_ids:
            try:
                self._event_repo.link_endpoints(event_id, all_endpoint_ids)
            except Exception:
                logger.exception("link_endpoints_failed", event_id=event_id)

        # 8. Split approved endpoints by delivery mode
        execute_endpoints = [
            ep for ep in approved_endpoints if ep.mode == EndpointMode.EXECUTE
        ]
        notify_endpoints = [
            ep for ep in approved_endpoints if ep.mode == EndpointMode.NOTIFY
        ]

        # 8a. Dispatch webhooks via Convoy + direct fast path for Execute-mode endpoints
        t0 = time.perf_counter()
        message_ids: list[str] = []
        if execute_endpoints:
            with tracer.start_as_current_span("dispatch") as dispatch_span:
                dispatch_span.set_attribute("dispatch.endpoint_count", len(execute_endpoints))
                try:
                    message_ids = await dispatch_event(
                        transfer=transfer,
                        matched_endpoints=execute_endpoints,
                        provider=self._webhook_provider,
                        event_type=webhook_event_type,
                        finality=finality,
                        identity=identity,
                    )
                    dispatch_span.set_attribute("dispatch.status", "ok")
                except Exception as exc:
                    logger.exception("dispatch_failed", tx_hash=tx_hash)
                    dispatch_span.record_exception(exc)
                    dispatch_span.set_status(StatusCode.ERROR, str(exc))
                    dispatch_span.set_attribute("dispatch.status", "error")

                # Record webhook deliveries
                for i, ep in enumerate(execute_endpoints):
                    msg_id = message_ids[i] if i < len(message_ids) else None
                    self._record_delivery(
                        endpoint_id=ep.id,
                        event_id=event_id,
                        provider_message_id=msg_id,
                    )

        # 8b. Push via Supabase Realtime for Notify-mode endpoints
        #     Filter through subscription matching first
        notify_event_ids: list[str] = []
        if notify_endpoints:
            filtered_notify_endpoints: list[Endpoint] = []
            for ep in notify_endpoints:
                subs = self._fetch_subscriptions(ep.id)
                if not subs:
                    # No subscriptions defined → backwards-compatible, receive all events
                    filtered_notify_endpoints.append(ep)
                else:
                    matched_subs = match_subscriptions(transfer, identity, subs)
                    if matched_subs:
                        filtered_notify_endpoints.append(ep)
                    else:
                        logger.info(
                            "subscription_filter_skipped",
                            endpoint_id=ep.id,
                            tx_hash=tx_hash,
                            subscriptions_checked=len(subs),
                        )
            notify_endpoints = filtered_notify_endpoints

        if notify_endpoints:
            try:
                notify_event_ids = await self._realtime_notifier.notify_batch(
                    endpoints=notify_endpoints,
                    transfer=transfer,
                    event_type=webhook_event_type,
                    finality=finality,
                    identity=identity,
                )
            except Exception:
                logger.exception("notify_dispatch_failed", tx_hash=tx_hash)
        timings["dispatch_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        timings["total_ms"] = round((time.perf_counter() - pipeline_start) * 1000, 2)

        # Record Prometheus pipeline metrics
        record_pipeline_timing(
            timings,
            chain_id=transfer.chain_id.value,
            status="processed",
        )

        logger.info(
            "event_processed",
            tx_hash=tx_hash,
            event_id=event_id,
            endpoints_matched=len(matched),
            endpoints_approved=len(approved_endpoints),
            webhooks_sent=len(message_ids),
            notify_sent=len(notify_event_ids),
            finalized=finality.is_finalized if finality else None,
            **timings,
        )

        return {
            "status": "processed",
            "tx_hash": tx_hash,
            "event_id": event_id,
            "endpoints_matched": len(matched),
            "endpoints_approved": len(approved_endpoints),
            "webhooks_sent": len(message_ids),
            "notify_sent": len(notify_event_ids),
            "message_ids": message_ids,
        }

    async def process_batch(
        self, raw_logs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Process a batch of Goldsky-decoded events concurrently.

        Uses a semaphore to bound concurrency and avoid overwhelming
        downstream services (RPC nodes, Supabase, webhook targets).
        """
        sem = asyncio.Semaphore(10)

        async def _bounded(raw_log: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                try:
                    return await self.process_event(raw_log)
                except Exception:
                    logger.exception("batch_event_unexpected_failure", raw_log=raw_log)
                    return {"status": "error", "reason": "unexpected_failure"}

        results = await asyncio.gather(*[_bounded(log) for log in raw_logs])
        return list(results)

    # ── Internal helpers ────────────────────────────────────────

    def _fetch_matching_endpoints(self, transfer) -> list[Endpoint]:
        """Fetch active endpoints whose recipient matches the transfer.

        Uses an in-memory TTL cache to avoid hitting Supabase on every event.
        Endpoints change rarely (only at registration time).
        """
        cache_key = transfer.to_address.lower()
        now = time.monotonic()
        cached = _endpoint_cache.get(cache_key)
        if cached is not None:
            expires_at, endpoints = cached
            if now < expires_at:
                return endpoints

        endpoints = self._endpoint_repo.list_by_recipient(transfer.to_address)
        _endpoint_cache[cache_key] = (now + ENDPOINT_CACHE_TTL, endpoints)
        return endpoints

    def _fetch_subscriptions(self, endpoint_id: str) -> list[Subscription]:
        """Fetch active subscriptions for a given endpoint."""
        try:
            result = (
                self._sb.table("subscriptions")
                .select("*")
                .eq("endpoint_id", endpoint_id)
                .eq("active", True)
                .execute()
            )
            return [Subscription(**row) for row in result.data]
        except Exception:
            logger.exception("fetch_subscriptions_failed", endpoint_id=endpoint_id)
            return []

    def _record_event(self, transfer, finality, identity, event_type, endpoint_id: str | None = None, event_id: str | None = None) -> str:
        """Insert an event row into the events table via EventRepository.

        If *event_id* is provided, it is used instead of generating a new UUID.
        This is used by the facilitator fast path to ensure the event ID
        matches the one stored in the nonces table for later correlation.
        """
        if event_id is None:
            event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # JSONB data column (used by the events API routes)
        data: dict[str, Any] = {
            "chain_id": transfer.chain_id.value,
            "tx_hash": transfer.tx_hash,
            "from_address": transfer.from_address,
            "to_address": transfer.to_address,
            "amount": transfer.value,
            "nonce": transfer.nonce,
            "token": transfer.token,
        }
        if finality:
            data["finality"] = {
                "confirmations": finality.confirmations,
                "required_confirmations": finality.required_confirmations,
                "is_finalized": finality.is_finalized,
            }
        if identity:
            data["identity"] = identity.model_dump()

        row: dict[str, Any] = {
            "id": event_id,
            "type": event_type.value,
            "data": data,
            "created_at": now,
            # Structured columns
            "chain_id": transfer.chain_id.value,
            "tx_hash": transfer.tx_hash,
            "block_number": transfer.block_number,
            "block_hash": transfer.block_hash,
            "log_index": transfer.log_index if hasattr(transfer, "log_index") else 0,
            "from_address": transfer.from_address,
            "to_address": transfer.to_address,
            "amount": transfer.value,
            "authorizer": transfer.authorizer,
            "nonce": transfer.nonce,
            "token": transfer.token,
            "status": "pre_confirmed" if event_type == WebhookEventType.PAYMENT_PRE_CONFIRMED else ("confirmed" if (finality and finality.is_finalized) else "pending"),
            "finality_depth": finality.confirmations if finality else 0,
        }

        if endpoint_id:
            row["endpoint_id"] = endpoint_id

        if identity:
            row["identity_data"] = identity.model_dump()

        if finality and finality.is_finalized:
            row["confirmed_at"] = now

        try:
            self._event_repo.insert(row)
        except Exception:
            logger.exception("event_record_failed", event_id=event_id)

        return event_id

    def _record_delivery(
        self, endpoint_id: str, event_id: str, provider_message_id: str | None
    ) -> None:
        """Record a webhook delivery attempt via WebhookDeliveryRepository."""
        try:
            self._delivery_repo.create(
                endpoint_id=endpoint_id,
                event_id=event_id,
                provider_message_id=provider_message_id,
                status="sent" if provider_message_id else "failed",
            )
        except Exception:
            logger.exception(
                "delivery_record_failed",
                endpoint_id=endpoint_id,
                event_id=event_id,
            )
