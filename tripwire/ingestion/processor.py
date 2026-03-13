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
from tripwire.ingestion.finality import check_finality
from tripwire.notify.realtime import RealtimeNotifier
from tripwire.types.models import (
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
        """
        with tracer.start_as_current_span("process_event") as span:
            detection = self._detect_event_type(raw_log)

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

        logger.info(
            "processing_pre_confirmed",
            event_type="payment.pre_confirmed",
            authorizer=transfer.authorizer,
        )

        # 1. Nonce deduplication
        t0 = time.perf_counter()
        try:
            is_new = await asyncio.to_thread(
                self._nonce_repo.record_nonce,
                chain_id=chain_id.value,
                nonce=transfer.nonce,
                authorizer=transfer.authorizer,
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
        return await self._dispatch_for_transfer(
            transfer=transfer,
            finality=None,
            identity=identity,
            timings=timings,
            pipeline_start=pipeline_start,
            event_type_override=WebhookEventType.PAYMENT_PRE_CONFIRMED,
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
                transfer = decode_transfer_event(raw_log)
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

        # 2. Deduplication
        #    ERC-3009: use authorizer + nonce (unique per authorization)
        #    Plain Transfer: use tx_hash + log_index (no meaningful nonce)
        t0 = time.perf_counter()
        is_plain_transfer = not transfer.authorizer
        dedup_nonce = transfer.nonce if not is_plain_transfer else f"{tx_hash}:{transfer.log_index}"
        dedup_authorizer = transfer.authorizer if not is_plain_transfer else "transfer"
        try:
            is_new = await asyncio.to_thread(
                self._nonce_repo.record_nonce,
                chain_id=chain_id.value,
                nonce=dedup_nonce,
                authorizer=dedup_authorizer,
            )
        except Exception:
            logger.exception("nonce_dedup_failed", tx_hash=tx_hash)
            return {"status": "error", "reason": "nonce_dedup_failed", "tx_hash": tx_hash}
        timings["dedup_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        if not is_new:
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
                decoded = decode_event_with_abi(raw_log, trigger.abi)
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

            # 7. Record event
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

    # ── Generic dispatch pipeline (shared across event types) ──────

    async def _dispatch_for_transfer(
        self,
        transfer,
        finality,
        identity,
        timings: dict[str, float],
        pipeline_start: float,
        event_type_override: WebhookEventType | None = None,
    ) -> dict[str, Any]:
        """Run the generic stages shared across all transfer-like events.

        Covers: endpoint matching, policy evaluation, event recording,
        webhook dispatch, and realtime notification.

        If *event_type_override* is provided it takes precedence over the
        finality-based type derivation (used by the facilitator fast path).
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
            event_id = self._record_event(
                transfer, finality, identity, event_type=webhook_event_type
            )
            return {
                "status": "no_endpoints",
                "tx_hash": tx_hash,
                "event_id": event_id,
            }

        matched = match_endpoints(transfer, endpoints)
        if not matched:
            logger.info("no_endpoints_matched_filters", tx_hash=tx_hash)
            webhook_event_type = event_type_override or (WebhookEventType.PAYMENT_CONFIRMED if (finality and finality.is_finalized) else WebhookEventType.PAYMENT_PENDING)
            event_id = self._record_event(
                transfer, finality, identity, event_type=webhook_event_type
            )
            return {
                "status": "no_match",
                "tx_hash": tx_hash,
                "event_id": event_id,
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

        # Determine event type based on finality (or use override)
        if event_type_override:
            webhook_event_type = event_type_override
        elif finality and finality.is_finalized:
            webhook_event_type = WebhookEventType.PAYMENT_CONFIRMED
        else:
            if finality is None:
                logger.warning("finality_unknown_defaulting_to_pending", tx_hash=tx_hash)
            webhook_event_type = WebhookEventType.PAYMENT_PENDING

        # 7. Record the event (link to the first matched endpoint)
        first_endpoint_id = matched[0].id if matched else None
        event_id = self._record_event(transfer, finality, identity, webhook_event_type, endpoint_id=first_endpoint_id)

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

    def _record_event(self, transfer, finality, identity, event_type, endpoint_id: str | None = None) -> str:
        """Insert an event row into the events table via EventRepository."""
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
            "status": "confirmed" if (finality and finality.is_finalized) else "pending",
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
