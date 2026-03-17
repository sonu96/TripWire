"""Redis Streams-based event bus for TripWire.

Provides pub/sub with consumer groups for reliable event distribution.
Streams are keyed by topic0 (event signature), so each event type gets
its own stream.  Workers use XREADGROUP for at-least-once delivery,
with XAUTOCLAIM handling crashed consumers.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

from tripwire.api.redis import get_redis as _get_redis
from tripwire.ingestion.decoder import _parse_topics

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STREAM_PREFIX = "tripwire:events:"
CONSUMER_GROUP = "trigger-workers"
MAX_STREAM_LEN = 100_000
MAX_STREAMS = 500  # cap distinct streams to prevent abuse
BLOCK_MS = 2000
CLAIM_IDLE_MS = 30_000

# Strict hex validation for topic0 (0x + 64 hex chars)
_HEX_TOPIC_RE = re.compile(r"^0x[0-9a-f]{64}$")

# Cache of stream keys where consumer group is already confirmed to exist.
_known_groups: set[str] = set()

# Track known stream keys for MAX_STREAMS enforcement at publish time.
_known_stream_keys: set[str] = set()


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

async def init_stream_keys() -> int:
    """Populate ``_known_stream_keys`` from Redis on startup.

    Scans for existing ``tripwire:events:*`` keys so the MAX_STREAMS cap
    is accurate across process restarts. Returns the number of keys found.
    """
    discovered = await scan_streams(f"{STREAM_PREFIX}*")
    _known_stream_keys.update(discovered)
    logger.info("event_bus.stream_keys_initialized", count=len(_known_stream_keys))
    return len(_known_stream_keys)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stream_key_for(raw_log: dict) -> str:
    """Derive the Redis stream key from a raw log's topic0."""
    topics = _parse_topics(raw_log.get("topics", []))
    if topics:
        topic0 = topics[0].lower()
        if _HEX_TOPIC_RE.match(topic0):
            return f"{STREAM_PREFIX}{topic0}"
    return f"{STREAM_PREFIX}unknown"


def _check_stream_cap(stream_key: str) -> str:
    """Enforce MAX_STREAMS cap. Route to 'unknown' if cap exceeded."""
    if stream_key in _known_stream_keys:
        return stream_key
    if len(_known_stream_keys) >= MAX_STREAMS:
        logger.warning(
            "event_bus.stream_cap_exceeded",
            attempted_key=stream_key,
            cap=MAX_STREAMS,
        )
        return f"{STREAM_PREFIX}unknown"
    _known_stream_keys.add(stream_key)
    return stream_key


def _invalidate_group(stream_key: str) -> None:
    """Remove a stream key from the known groups cache (e.g., after NOGROUP)."""
    _known_groups.discard(stream_key)


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

async def publish_event(raw_log: dict) -> str:
    """Publish a single raw log to the appropriate Redis stream.

    Returns the stream message ID assigned by Redis.
    """
    redis = _get_redis()
    stream_key = _check_stream_cap(_stream_key_for(raw_log))
    payload = json.dumps(raw_log)

    message_id: str = await redis.xadd(
        stream_key,
        {"payload": payload},
        maxlen=MAX_STREAM_LEN,
        approximate=True,
    )

    logger.debug(
        "event_bus.published",
        stream=stream_key,
        message_id=message_id,
    )
    return message_id


async def publish_batch(raw_logs: list[dict]) -> list[str]:
    """Publish multiple raw logs using a Redis pipeline.

    Logs are grouped by topic0 so each XADD targets the correct stream.
    Returns the list of message IDs in the same order as the input.
    """
    if not raw_logs:
        return []

    redis = _get_redis()
    pipe = redis.pipeline(transaction=False)

    # Track order so we can return IDs in input order.
    stream_keys: list[str] = []
    for raw_log in raw_logs:
        stream_key = _check_stream_cap(_stream_key_for(raw_log))
        stream_keys.append(stream_key)
        payload = json.dumps(raw_log)
        pipe.xadd(
            stream_key,
            {"payload": payload},
            maxlen=MAX_STREAM_LEN,
            approximate=True,
        )

    results = await pipe.execute()
    message_ids: list[str] = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.error(
                "event_bus.xadd_failed",
                stream=stream_keys[i],
                error=str(r),
            )
            raise r
        message_ids.append(str(r))

    logger.info(
        "event_bus.batch_published",
        count=len(message_ids),
        streams=list(set(stream_keys)),
    )
    return message_ids


# ---------------------------------------------------------------------------
# Consumer group management
# ---------------------------------------------------------------------------

async def ensure_consumer_group(stream_key: str) -> None:
    """Create the consumer group if it does not already exist.

    Uses MKSTREAM so the stream is created on-the-fly if absent.
    Silently ignores the BUSYGROUP error (group already exists).
    Results are cached in-memory to avoid redundant Redis calls.
    """
    if stream_key in _known_groups:
        return

    redis = _get_redis()
    try:
        await redis.xgroup_create(
            stream_key,
            CONSUMER_GROUP,
            id="0",
            mkstream=True,
        )
        logger.debug("event_bus.group_created", stream=stream_key, group=CONSUMER_GROUP)
    except Exception as exc:
        if "BUSYGROUP" in str(exc):
            # Consumer group already exists — expected on restarts.
            pass
        else:
            logger.error(
                "event_bus.group_create_failed",
                stream=stream_key,
                error=str(exc),
            )
            raise

    _known_groups.add(stream_key)


# ---------------------------------------------------------------------------
# Consume
# ---------------------------------------------------------------------------

async def consume_events(
    stream_keys: list[str],
    consumer_name: str,
    batch_size: int = 50,
) -> list[tuple[str, str, dict]]:
    """Read new events from one or more streams via XREADGROUP.

    Ensures the consumer group exists on every requested stream before
    reading.  Blocks for up to ``BLOCK_MS`` milliseconds when there are
    no pending messages.

    Returns a list of ``(stream_key, message_id, raw_log)`` tuples.
    """
    redis = _get_redis()

    # Ensure consumer groups exist for all requested streams.
    for key in stream_keys:
        await ensure_consumer_group(key)

    # XREADGROUP expects a dict of {stream_key: last_id}.
    # ">" means "give me new messages not yet delivered to this consumer".
    streams = {key: ">" for key in stream_keys}

    try:
        response = await redis.xreadgroup(
            CONSUMER_GROUP,
            consumer_name,
            streams,
            count=batch_size,
            block=BLOCK_MS,
        )
    except Exception as exc:
        # Handle NOGROUP after Redis restart — invalidate cache and retry once
        if "NOGROUP" in str(exc):
            logger.warning(
                "event_bus.nogroup_recovery",
                consumer=consumer_name,
                streams=stream_keys,
            )
            for key in stream_keys:
                _invalidate_group(key)
                await ensure_consumer_group(key)
            response = await redis.xreadgroup(
                CONSUMER_GROUP,
                consumer_name,
                streams,
                count=batch_size,
                block=BLOCK_MS,
            )
        else:
            raise

    results: list[tuple[str, str, dict]] = []
    if response is None:
        return results

    for stream_key, messages in response:
        for message_id, fields in messages:
            try:
                raw_log = json.loads(fields["payload"])
            except (KeyError, json.JSONDecodeError) as exc:
                # ACK poison messages so they don't loop forever
                await redis.xack(stream_key, CONSUMER_GROUP, message_id)
                logger.warning(
                    "event_bus.poison_message_acked",
                    stream=stream_key,
                    message_id=message_id,
                    error=str(exc),
                )
                continue
            results.append((stream_key, message_id, raw_log))

    logger.debug(
        "event_bus.consumed",
        consumer=consumer_name,
        count=len(results),
    )
    return results


# ---------------------------------------------------------------------------
# Acknowledge
# ---------------------------------------------------------------------------

async def ack_event(stream_key: str, message_id: str) -> None:
    """Acknowledge a message so it is removed from the pending entries list."""
    redis = _get_redis()
    await redis.xack(stream_key, CONSUMER_GROUP, message_id)


async def ack_batch(ack_map: dict[str, list[str]]) -> None:
    """Acknowledge multiple messages in a single pipeline.

    *ack_map* maps stream_key → list of message IDs to ACK.
    """
    if not ack_map:
        return
    redis = _get_redis()
    pipe = redis.pipeline(transaction=False)
    for stream_key, ids in ack_map.items():
        pipe.xack(stream_key, CONSUMER_GROUP, *ids)
    await pipe.execute()


# ---------------------------------------------------------------------------
# Claim stale messages
# ---------------------------------------------------------------------------

async def claim_stale(
    stream_keys: list[str],
    consumer_name: str,
    count: int = 10,
) -> list[tuple[str, str, dict]]:
    """Claim messages that have been pending (unacknowledged) for too long.

    Uses XAUTOCLAIM to transfer ownership of messages idle for at least
    ``CLAIM_IDLE_MS`` to ``consumer_name``.  This handles the case where
    a worker crashed before acknowledging.

    Returns a list of ``(stream_key, message_id, raw_log)`` tuples.
    """
    redis = _get_redis()
    results: list[tuple[str, str, dict]] = []

    for stream_key in stream_keys:
        await ensure_consumer_group(stream_key)
        try:
            # XAUTOCLAIM returns (next_start_id, claimed_messages, deleted_ids)
            response = await redis.xautoclaim(
                stream_key,
                CONSUMER_GROUP,
                consumer_name,
                min_idle_time=CLAIM_IDLE_MS,
                start_id="0-0",
                count=count,
            )

            # response format: [next_start_id, [(id, fields), ...], ...]
            if response and len(response) >= 2:
                claimed_messages = response[1]
                for message_id, fields in claimed_messages:
                    try:
                        raw_log = json.loads(fields["payload"])
                    except (KeyError, json.JSONDecodeError) as exc:
                        # ACK poison messages so they don't loop forever
                        await redis.xack(stream_key, CONSUMER_GROUP, message_id)
                        logger.warning(
                            "event_bus.claim_poison_acked",
                            stream=stream_key,
                            message_id=message_id,
                            error=str(exc),
                        )
                        continue
                    results.append((stream_key, message_id, raw_log))

        except Exception as exc:
            # Handle NOGROUP after Redis restart
            if "NOGROUP" in str(exc):
                _invalidate_group(stream_key)
                logger.warning("event_bus.nogroup_on_claim", stream=stream_key)
            else:
                logger.error(
                    "event_bus.claim_failed",
                    stream=stream_key,
                    error=str(exc),
                )
                raise

    logger.debug(
        "event_bus.claimed_stale",
        consumer=consumer_name,
        count=len(results),
    )
    return results


# ---------------------------------------------------------------------------
# Monitoring / health
# ---------------------------------------------------------------------------

async def scan_streams(pattern: str) -> list[str]:
    """Scan Redis for stream keys matching *pattern*.

    Returns a list of matching key names.
    """
    redis = _get_redis()
    cursor: int | str = 0
    keys: list[str] = []
    while True:
        cursor, batch = await redis.scan(
            cursor=cursor,
            match=pattern,
            count=100,
        )
        keys.extend(batch)
        if cursor == 0:
            break
    return keys


async def get_stream_info() -> dict[str, Any]:
    """Return per-stream length, pending count, and consumer group info.

    Scans for all streams matching ``STREAM_PREFIX*`` and gathers
    diagnostics useful for monitoring dashboards and health checks.
    """
    redis = _get_redis()
    info: dict[str, Any] = {}

    # Find all event streams.
    cursor: int | str = 0
    stream_keys: list[str] = []
    while True:
        cursor, keys = await redis.scan(
            cursor=cursor,
            match=f"{STREAM_PREFIX}*",
            count=100,
        )
        stream_keys.extend(keys)
        if cursor == 0:
            break

    # Also include the DLQ stream if it exists
    try:
        dlq_exists = await redis.exists("tripwire:dlq")
        if dlq_exists:
            stream_keys.append("tripwire:dlq")
    except Exception:
        pass

    for stream_key in stream_keys:
        try:
            length = await redis.xlen(stream_key)

            groups_raw = await redis.xinfo_groups(stream_key)
            groups = []
            for g in groups_raw:
                groups.append({
                    "name": g.get("name"),
                    "consumers": g.get("consumers"),
                    "pending": g.get("pending"),
                    "last_delivered_id": g.get("last-delivered-id"),
                })

            info[stream_key] = {
                "length": length,
                "consumer_groups": groups,
            }
        except Exception as exc:
            logger.warning(
                "event_bus.stream_info_failed",
                stream=stream_key,
                error=str(exc),
            )
            info[stream_key] = {"error": str(exc)}

    return info
