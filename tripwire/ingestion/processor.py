"""End-to-end ingestion pipeline orchestrator.

Receives a raw onchain event and runs the full pipeline:
  detect -> decode -> evaluate -> dispatch

The processor is event-type agnostic: it detects the event type from
the raw log's topic signature and routes to the appropriate handler.
Product-specific logic lives in ``tripwire.ingestion.handlers``:
  - ``PaymentHandler``: ERC-3009 TransferWithAuthorization (Keeper)
  - ``TriggerHandler``: Dynamic trigger events (Pulse)

Shared utilities (event recording, delivery recording, endpoint caching,
subscription fetching) remain on the ``EventProcessor`` class so that
handlers can call them via the ``processor`` reference.
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

# Handler imports
from tripwire.ingestion.handlers.base import EventHandler
from tripwire.ingestion.handlers.payment import PaymentHandler
from tripwire.ingestion.handlers.trigger import TriggerHandler
from tripwire.ingestion.handlers.trigger import (
    _process_dynamic_event,
    _process_unified,
    _check_payment_gate,
)

logger = structlog.get_logger(__name__)

# ── Event Signature Registry ───────────────────────────────────
# Maps known topic0 signatures to their canonical event type string.
# To add a new event type, register its topic0 here and implement a
# corresponding handler in tripwire/ingestion/handlers/.

_EVENT_SIGNATURES: dict[str, str] = {
    AUTHORIZATION_USED_TOPIC.lower(): "erc3009_transfer",
    TRANSFER_TOPIC.lower(): "erc3009_transfer",
}

# ── Endpoint cache ───────────────────────────────────────────────
# In-memory TTL cache for endpoint lookups. Endpoints change rarely
# (only at registration time), so a 30s TTL is safe.
_endpoint_cache: dict[str, tuple[float, list]] = {}
ENDPOINT_CACHE_TTL = 30  # seconds

# Default handler instances (created once, reused by all processors)
_DEFAULT_HANDLERS: list[EventHandler] = [
    PaymentHandler(),
    TriggerHandler(),
]


class EventProcessor:
    """Orchestrates the full ingestion->dispatch pipeline.

    The processor is a thin orchestrator: ``process_event`` detects the
    event type from the raw log and delegates to the first registered
    handler that can process it.  Shared utilities (endpoint matching,
    policy evaluation, webhook dispatch, identity enrichment, event
    recording) live on this class so handlers can access them.
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
        handlers: list[EventHandler] | None = None,
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
        self._handlers = handlers if handlers is not None else list(_DEFAULT_HANDLERS)

    # ── Public entry point ────────────────────────────────────────

    async def process_event(self, raw_log: dict[str, Any]) -> dict[str, Any]:
        """Process a single onchain event through the full pipeline.

        Detects the event type from the raw log's topic signature, then
        delegates to the first registered handler that can process it.
        Returns a summary dict with processing results.

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
                # ── Handler-based routing ─────────────────────────────
                result = None
                for handler in self._handlers:
                    if await handler.can_handle(detection, raw_log):
                        event_label = detection[0] if isinstance(detection, tuple) else detection
                        span.set_attribute("event.type", event_label)
                        if isinstance(detection, tuple):
                            span.set_attribute("event.trigger_count", len(detection[1]))
                        result = await handler.handle(raw_log, self, detection)
                        break

                if result is None:
                    event_label = detection if isinstance(detection, str) else "unknown"
                    span.set_attribute("event.type", event_label)
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

        # 2. Identity resolution (no finality check -- tx not yet onchain)
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

    # ── Legacy method stubs (delegate to handlers) ────────────────
    # Kept for backward compatibility with any code that calls these
    # methods directly on the processor instance.

    async def _process_erc3009_event(self, raw_log: dict[str, Any]) -> dict[str, Any]:
        """ERC-3009 TransferWithAuthorization handler (delegates to PaymentHandler)."""
        from tripwire.ingestion.handlers.payment import _process_erc3009_event
        return await _process_erc3009_event(raw_log, self)

    async def _process_dynamic_event(
        self, raw_log: dict[str, Any], triggers: list
    ) -> dict[str, Any]:
        """Dynamic trigger handler (delegates to TriggerHandler)."""
        return await _process_dynamic_event(raw_log, triggers, self)

    async def _process_unified(
        self,
        raw_log: dict[str, Any],
        triggers: list | None = None,
    ) -> dict[str, Any]:
        """Unified processing loop (delegates to trigger handler module)."""
        return await _process_unified(raw_log, triggers, self)

    @staticmethod
    def _check_payment_gate(
        decoded: DecodedEvent, trigger: Any
    ) -> tuple[bool, str]:
        """Payment gating check (delegates to trigger handler module)."""
        return _check_payment_gate(decoded, trigger)

    # ── Facilitator -> Goldsky promotion ─────────────────────────────

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

        # 6. Policy evaluation -- filter out endpoints that fail policy
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

        # 6b. Finality depth gate -- defer endpoints whose configured
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
                    # No subscriptions defined -> backwards-compatible, receive all events
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
