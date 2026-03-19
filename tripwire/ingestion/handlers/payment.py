"""Payment (Keeper) event handler.

Extracts ERC-3009 TransferWithAuthorization processing from the
monolithic processor into a standalone handler.  Covers:
  - ERC-3009 detection
  - Decode via ERC3009Decoder
  - Nonce deduplication (with facilitator correlation)
  - Finality + identity resolution (parallel)
  - Dispatch via _dispatch_for_transfer (shared utility on processor)

Also handles the facilitator pre-confirmed fast path via
``process_pre_confirmed_event`` on the processor.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, TYPE_CHECKING

import structlog

from tripwire.observability.tracing import tracer, StatusCode
from tripwire.ingestion.decoders import ERC3009Decoder
from tripwire.ingestion.finality import check_finality

if TYPE_CHECKING:
    from tripwire.ingestion.processor import EventProcessor

logger = structlog.get_logger(__name__)


class PaymentHandler:
    """Handles ERC-3009 TransferWithAuthorization events (Keeper product)."""

    async def can_handle(
        self,
        event_type: str | tuple[str, list],
        raw_log: dict[str, Any],
    ) -> bool:
        """Return True for ``"erc3009_transfer"`` events."""
        return event_type == "erc3009_transfer"

    async def handle(
        self,
        raw_log: dict[str, Any],
        processor: EventProcessor,
        event_type: str | tuple[str, list],
    ) -> dict[str, Any]:
        """Process an ERC-3009 TransferWithAuthorization event.

        Runs the type-specific stages (decode, nonce dedup, finality,
        identity) and then hands off to the generic dispatch pipeline
        on the processor.
        """
        return await _process_erc3009_event(raw_log, processor)


# ── Implementation (extracted from EventProcessor._process_erc3009_event) ──


async def _process_erc3009_event(
    raw_log: dict[str, Any],
    processor: EventProcessor,
) -> dict[str, Any]:
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
            processor._nonce_repo.record_nonce_or_correlate,
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
            return await processor._promote_pre_confirmed_event(
                existing_event_id=existing_event_id,
                transfer=transfer,
                timings=timings,
                pipeline_start=pipeline_start,
            )
        # True duplicate -- same source or no correlation info
        logger.info("duplicate_nonce", tx_hash=tx_hash, nonce=dedup_nonce)
        return {"status": "duplicate", "tx_hash": tx_hash}

    # 3-4. Finality check and identity resolution
    #       run in parallel -- they have ZERO data dependencies on each other.
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
                result = await processor._resolver.resolve(
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

    # 5-8. Generic stages: endpoint matching -> policy -> dispatch
    return await processor._dispatch_for_transfer(
        transfer=transfer,
        finality=finality,
        identity=identity,
        timings=timings,
        pipeline_start=pipeline_start,
    )
