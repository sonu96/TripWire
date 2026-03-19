"""Partitioned trigger worker for Redis Streams event consumption.

Consumes onchain events from Redis Streams and processes them through
the existing EventProcessor pipeline. Uses an in-memory trigger index
for O(1) lookup by topic0 (event_signature).

Architecture:
  WorkerPool → N x TriggerWorker → EventProcessor.process_event()
  All workers share a single TriggerIndex that refreshes periodically.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from tripwire.ingestion import event_bus
from tripwire.api.redis import get_redis as _get_redis
from tripwire.cache import RedisCache
from tripwire.db.repositories.triggers import TriggerRepository
from tripwire.types.models import Trigger

if TYPE_CHECKING:
    from tripwire.ingestion.processor import EventProcessor

logger = structlog.get_logger(__name__)

_STREAM_DISCOVERY_INTERVAL = 30.0  # seconds between new-stream scans
_SHUTDOWN_TIMEOUT = 30.0  # max seconds to wait for workers on shutdown
_MAX_RETRIES = 5  # ACK + DLQ after this many failed processing attempts
_MAX_STREAMS_PER_WORKER = 100  # cap streams per worker to prevent abuse
_DLQ_STREAM = "tripwire:dlq"  # dead-letter stream for permanently failed events
_FAILURE_COUNTS_CLEANUP_INTERVAL = 100  # clean stale entries every N iterations
_FAILURE_COUNTS_MAX_AGE = 300.0  # seconds before a failure entry is considered stale
_PROCESS_TIMEOUT = 30.0  # seconds before process_event is considered hung
_RESTART_COOLDOWN_BASE = 2.0  # base seconds for restart backoff
_RESTART_COOLDOWN_MAX = 120.0  # max seconds between restart attempts
_RESTART_COUNT_RESET_AFTER = 300.0  # reset restart counter after this many seconds of stability


# ── Trigger Index ────────────────────────────────────────────────


class TriggerIndex:
    """In-memory index for O(1) trigger lookup by topic0 (event_signature).

    Periodically refreshes from the database via TriggerRepository.
    Uses a Redis-backed cache for multi-instance consistency with
    in-memory fallback when Redis is unavailable.
    Shared across all workers in a pool.
    """

    def __init__(self, trigger_repo: TriggerRepository) -> None:
        self._trigger_repo = trigger_repo
        self._index: dict[str, list[Trigger]] = {}
        self._last_refresh: float = 0.0
        self._refresh_interval: float = 10.0
        self._refresh_lock = asyncio.Lock()
        # Redis-backed shared cache for cross-instance trigger lookups
        try:
            self._cache = RedisCache(_get_redis(), prefix="tripwire:triggers", default_ttl=10)
        except Exception:
            self._cache = RedisCache(None, prefix="tripwire:triggers", default_ttl=10)

    async def refresh(self) -> None:
        """Load all active triggers and rebuild the topic0 index."""
        from tripwire.utils.topic import compute_topic0

        try:
            triggers = await asyncio.to_thread(self._trigger_repo.list_active)
        except Exception:
            logger.exception("trigger_index_refresh_failed")
            return

        new_index: dict[str, list[Trigger]] = {}
        for trigger in triggers:
            # Use precomputed topic0, fall back to computing from event_signature
            key = trigger.topic0 or compute_topic0(trigger.event_signature)
            new_index.setdefault(key.lower(), []).append(trigger)

        self._index = new_index
        self._last_refresh = time.monotonic()

        # Write each topic's triggers to Redis for cross-instance visibility
        for topic_key, topic_triggers in new_index.items():
            try:
                serializable = [t.model_dump() for t in topic_triggers]
                await self._cache.set(f"topic:{topic_key}", serializable, ttl=15)
            except Exception:
                logger.debug("trigger_cache_set_failed", topic=topic_key)

        logger.info(
            "trigger_index_refreshed",
            total_triggers=len(triggers),
            unique_topics=len(new_index),
        )

    def match(self, topic0: str) -> list[Trigger]:
        """O(1) lookup of triggers matching a topic0 hash."""
        return self._index.get(topic0.lower(), [])

    async def match_with_cache(self, topic0: str) -> list[Trigger]:
        """Lookup triggers by topic0 with Redis cache fallback.

        Checks in-memory index first, then falls back to Redis cache
        for cross-instance consistency.
        """
        key = topic0.lower()
        # In-memory first (fast path)
        result = self._index.get(key)
        if result:
            return result

        # Redis cache fallback (cross-instance)
        try:
            cached = await self._cache.get(f"topic:{key}")
            if cached:
                triggers = [Trigger(**t) if isinstance(t, dict) else t for t in cached]
                # Populate in-memory index for subsequent lookups
                self._index[key] = triggers
                return triggers
        except Exception:
            logger.debug("trigger_cache_get_failed", topic=key)

        return []

    async def maybe_refresh(self) -> None:
        """Refresh the index if it is stale (older than _refresh_interval).

        Uses a lock to prevent concurrent refreshes from multiple workers.
        """
        if time.monotonic() - self._last_refresh > self._refresh_interval:
            async with self._refresh_lock:
                # Double-check after acquiring lock
                if time.monotonic() - self._last_refresh > self._refresh_interval:
                    await self.refresh()


# ── Dead Letter Queue ────────────────────────────────────────────


async def _send_to_dlq(
    stream_key: str,
    message_id: str,
    raw_log: dict,
    error_count: int,
) -> bool:
    """Write a permanently failed event to the dead-letter stream.

    Returns True if the DLQ write succeeded, False otherwise.
    Callers should only ACK the original message when this returns True.
    """
    try:
        redis = _get_redis()
        dlq_payload = json.dumps({
            "source_stream": stream_key,
            "source_message_id": message_id,
            "raw_log": raw_log,
            "error_count": error_count,
            "timestamp": time.time(),
        })
        await redis.xadd(
            _DLQ_STREAM,
            {"payload": dlq_payload},
            maxlen=event_bus.MAX_STREAM_LEN,
            approximate=True,
        )
        logger.error(
            "event_sent_to_dlq",
            stream_key=stream_key,
            message_id=message_id,
            error_count=error_count,
        )
        return True
    except Exception:
        logger.exception(
            "dlq_write_failed",
            stream_key=stream_key,
            message_id=message_id,
        )
        return False


# ── Trigger Worker ───────────────────────────────────────────────


class TriggerWorker:
    """Consumes events from assigned Redis Streams and processes them.

    Each worker has a unique worker_id used as the consumer name in
    Redis Streams consumer groups. Events are processed through the
    existing EventProcessor, which handles detection, decoding, dedup,
    identity resolution, policy evaluation, and dispatch.
    """

    def __init__(
        self,
        worker_id: str,
        stream_keys: list[str],
        processor: EventProcessor,
        trigger_index: TriggerIndex,
    ) -> None:
        self.worker_id = worker_id
        self.stream_keys = list(stream_keys)
        self._processor = processor
        self._trigger_index = trigger_index
        self._running: bool = False
        self._processed: int = 0
        self._errors: int = 0
        # Track per-message failure counts: (stream_key, message_id) → (count, timestamp)
        self._failure_counts: dict[tuple[str, str], tuple[int, float]] = {}

    async def start(self) -> None:
        """Main consumption loop.

        1. Ensures consumer groups exist for all assigned streams.
        2. Reads batches from Redis Streams via XREADGROUP.
        3. Periodically claims stale messages from other consumers.
        4. Feeds each event through EventProcessor.process_event().
        5. ACKs on success; sends to DLQ after max retries.
        """
        self._running = True
        log = logger.bind(worker_id=self.worker_id)

        # Ensure consumer groups exist
        for stream_key in list(self.stream_keys):
            try:
                await event_bus.ensure_consumer_group(stream_key)
            except Exception:
                log.exception(
                    "consumer_group_setup_failed",
                    stream_key=stream_key,
                )

        log.info(
            "trigger_worker_started",
            stream_keys=self.stream_keys,
        )

        iteration = 0
        consecutive_errors = 0
        while self._running:
            iteration += 1

            # Refresh trigger index if stale
            await self._trigger_index.maybe_refresh()

            # Snapshot stream_keys to avoid mutation during iteration
            current_streams = list(self.stream_keys)

            # Consume batch from event bus
            messages: list[tuple[str, str, dict[str, Any]]] = []
            try:
                batch = await event_bus.consume_events(
                    current_streams, self.worker_id
                )
                consecutive_errors = 0  # reset on any successful call
                if batch:
                    messages.extend(batch)
            except Exception:
                log.exception("consume_events_failed")
                consecutive_errors += 1
                backoff = min(2 ** consecutive_errors, 60)
                await asyncio.sleep(backoff)
                continue

            # Claim stale messages every 10 iterations
            if iteration % 10 == 0:
                try:
                    stale = await event_bus.claim_stale(
                        current_streams, self.worker_id
                    )
                    if stale:
                        messages.extend(stale)
                        log.info(
                            "claimed_stale_messages",
                            count=len(stale),
                        )
                except Exception:
                    log.exception("claim_stale_failed")

            # Periodic cleanup of stale failure count entries
            if iteration % _FAILURE_COUNTS_CLEANUP_INTERVAL == 0:
                self._cleanup_failure_counts()

            # Process each message, collect ACKs for batching
            ack_map: dict[str, list[str]] = {}
            for stream_key, message_id, raw_log in messages:
                try:
                    await asyncio.wait_for(
                        self._processor.process_event(raw_log),
                        timeout=_PROCESS_TIMEOUT,
                    )
                    ack_map.setdefault(stream_key, []).append(message_id)
                    self._processed += 1
                    # Clear failure count on success
                    self._failure_counts.pop((stream_key, message_id), None)
                except asyncio.TimeoutError:
                    log.error(
                        "process_event_timeout",
                        stream_key=stream_key,
                        message_id=message_id,
                        timeout=_PROCESS_TIMEOUT,
                    )
                    # Count as failure for retry/DLQ logic — fall through
                    self._errors += 1
                    fail_key = (stream_key, message_id)
                    prev_count, _ = self._failure_counts.get(fail_key, (0, 0.0))
                    new_count = prev_count + 1
                    self._failure_counts[fail_key] = (new_count, time.monotonic())

                    if new_count >= _MAX_RETRIES:
                        dlq_ok = await _send_to_dlq(stream_key, message_id, raw_log, new_count)
                        if dlq_ok:
                            ack_map.setdefault(stream_key, []).append(message_id)
                            del self._failure_counts[fail_key]
                except Exception:
                    self._errors += 1
                    fail_key = (stream_key, message_id)
                    prev_count, _ = self._failure_counts.get(fail_key, (0, 0.0))
                    new_count = prev_count + 1
                    self._failure_counts[fail_key] = (new_count, time.monotonic())

                    if new_count >= _MAX_RETRIES:
                        # Exceeded retry limit — send to DLQ and only ACK if DLQ write succeeded
                        dlq_ok = await _send_to_dlq(stream_key, message_id, raw_log, new_count)
                        if dlq_ok:
                            ack_map.setdefault(stream_key, []).append(message_id)
                            del self._failure_counts[fail_key]
                        # If DLQ write failed, don't ACK — message stays pending for retry
                    else:
                        log.warning(
                            "event_processing_failed",
                            stream_key=stream_key,
                            message_id=message_id,
                            attempt=new_count,
                        )
                        # Don't ACK — message will be retried or claimed

            # Batch ACK all successful (and DLQ'd) messages
            if ack_map:
                try:
                    await event_bus.ack_batch(ack_map)
                except Exception:
                    log.exception("batch_ack_failed")

        log.info(
            "trigger_worker_stopped",
            processed=self._processed,
            errors=self._errors,
        )

    def _cleanup_failure_counts(self) -> None:
        """Remove stale entries from _failure_counts to prevent memory leaks."""
        now = time.monotonic()
        stale_keys = [
            k for k, (_, ts) in self._failure_counts.items()
            if now - ts > _FAILURE_COUNTS_MAX_AGE
        ]
        for k in stale_keys:
            del self._failure_counts[k]

    async def stop(self) -> None:
        """Signal the worker to stop after the current iteration."""
        self._running = False

    @property
    def stats(self) -> dict[str, Any]:
        """Return current worker statistics."""
        return {
            "worker_id": self.worker_id,
            "stream_keys": list(self.stream_keys),
            "processed": self._processed,
            "errors": self._errors,
            "running": self._running,
        }


# ── Worker Pool ──────────────────────────────────────────────────


class WorkerPool:
    """Manages a pool of partitioned TriggerWorkers.

    Discovers active streams, partitions them across workers via
    round-robin, and manages lifecycle (start/stop) as asyncio tasks.
    Periodically scans for new streams and assigns them to workers.
    Automatically restarts crashed workers.
    """

    def __init__(
        self,
        num_workers: int,
        processor: EventProcessor,
        trigger_repo: TriggerRepository,
    ) -> None:
        self._num_workers = num_workers
        self._processor = processor
        self._trigger_repo = trigger_repo
        self._workers: list[TriggerWorker] = []
        self._tasks: list[asyncio.Task] = []
        self._trigger_index: TriggerIndex | None = None
        self._known_streams: set[str] = set()
        self._discovery_task: asyncio.Task | None = None
        self._stopping: bool = False
        # Restart rate limiting: worker_index → (restart_count, last_restart_time)
        self._restart_state: dict[int, tuple[int, float]] = {}

    async def start(self) -> None:
        """Initialize and start all workers.

        1. Create a shared TriggerIndex and perform initial refresh.
        2. Discover active stream keys from Redis and trigger index.
        3. Partition streams across workers via round-robin.
        4. Launch each worker as an asyncio task.
        5. Start periodic stream discovery loop.
        """
        # 1. Shared trigger index
        self._trigger_index = TriggerIndex(self._trigger_repo)
        await self._trigger_index.refresh()

        # 2. Discover stream keys
        stream_keys: set[str] = set()

        # From Redis: scan for tripwire:events:* pattern
        try:
            discovered = await event_bus.scan_streams("tripwire:events:*")
            stream_keys.update(discovered)
        except Exception:
            logger.exception("stream_discovery_failed")

        # From trigger index: derive stream keys from unique topics
        for topic0 in self._trigger_index._index:
            stream_keys.add(f"tripwire:events:{topic0}")

        stream_list = sorted(stream_keys)

        if not stream_list:
            logger.warning("no_streams_discovered")
            stream_list = ["tripwire:events:default"]

        # Cap total streams
        if len(stream_list) > event_bus.MAX_STREAMS:
            logger.warning(
                "too_many_streams_capped",
                discovered=len(stream_list),
                cap=event_bus.MAX_STREAMS,
            )
            stream_list = stream_list[:event_bus.MAX_STREAMS]

        self._known_streams = set(stream_list)

        # 3. Partition streams across workers (round-robin)
        partitions: list[list[str]] = [[] for _ in range(self._num_workers)]
        for i, key in enumerate(stream_list):
            partitions[i % self._num_workers].append(key)

        # 4. Create and start workers
        for i in range(self._num_workers):
            worker_id = f"worker-{i}"
            worker = TriggerWorker(
                worker_id=worker_id,
                stream_keys=partitions[i],
                processor=self._processor,
                trigger_index=self._trigger_index,
            )
            self._workers.append(worker)
            task = asyncio.create_task(
                worker.start(), name=f"trigger-worker-{i}"
            )
            task.add_done_callback(self._on_worker_done)
            self._tasks.append(task)

        # 5. Start periodic stream discovery
        self._discovery_task = asyncio.create_task(
            self._discover_streams_loop(), name="stream-discovery"
        )

        logger.info(
            "worker_pool_started",
            num_workers=self._num_workers,
            total_streams=len(stream_list),
        )

    def _on_worker_done(self, task: asyncio.Task) -> None:
        """Callback when a worker task finishes — log crashes and restart with backoff."""
        if task.cancelled():
            logger.warning("trigger_worker_cancelled", task_name=task.get_name())
            return
        if exc := task.exception():
            logger.error(
                "trigger_worker_crashed",
                task_name=task.get_name(),
                error=str(exc),
                exc_info=exc,
            )
            # Restart the worker unless we're shutting down
            if not self._stopping:
                try:
                    idx = self._tasks.index(task)
                except (ValueError, IndexError):
                    logger.exception("trigger_worker_restart_failed")
                    return
                # Rate-limit restarts with exponential backoff
                restart_count, last_restart = self._restart_state.get(idx, (0, 0.0))
                now = time.monotonic()
                # Reset counter if worker was stable long enough
                if now - last_restart > _RESTART_COUNT_RESET_AFTER:
                    restart_count = 0
                restart_count += 1
                self._restart_state[idx] = (restart_count, now)
                delay = min(_RESTART_COOLDOWN_BASE * (2 ** (restart_count - 1)), _RESTART_COOLDOWN_MAX)
                logger.info(
                    "trigger_worker_restart_scheduled",
                    task_name=task.get_name(),
                    restart_count=restart_count,
                    delay_seconds=delay,
                )
                # Schedule delayed restart
                asyncio.get_event_loop().call_later(
                    delay, self._restart_worker, idx, task.get_name()
                )

    def _restart_worker(self, idx: int, task_name: str) -> None:
        """Actually restart a worker after the cooldown delay."""
        if self._stopping:
            return
        try:
            worker = self._workers[idx]
            worker._running = False  # reset state
            new_task = asyncio.create_task(worker.start(), name=task_name)
            new_task.add_done_callback(self._on_worker_done)
            self._tasks[idx] = new_task
            logger.info("trigger_worker_restarted", task_name=task_name)
        except (ValueError, IndexError):
            logger.exception("trigger_worker_restart_failed")

    async def _discover_streams_loop(self) -> None:
        """Periodically scan for new streams and assign to workers."""
        while True:
            await asyncio.sleep(_STREAM_DISCOVERY_INTERVAL)
            try:
                current = await event_bus.scan_streams("tripwire:events:*")
                new_streams = set(current) - self._known_streams
                for stream_key in sorted(new_streams):
                    if len(self._known_streams) >= event_bus.MAX_STREAMS:
                        logger.warning(
                            "stream_cap_reached",
                            cap=event_bus.MAX_STREAMS,
                        )
                        break
                    await self.add_stream(stream_key)
                    self._known_streams.add(stream_key)
                if new_streams:
                    logger.info(
                        "new_streams_discovered",
                        count=len(new_streams),
                        streams=sorted(new_streams),
                    )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("stream_discovery_loop_error")

    async def stop(self) -> None:
        """Gracefully stop all workers and await task completion."""
        self._stopping = True
        logger.info("worker_pool_stopping", num_workers=len(self._workers))

        # Cancel stream discovery
        if self._discovery_task is not None:
            self._discovery_task.cancel()
            try:
                await self._discovery_task
            except asyncio.CancelledError:
                pass

        # Signal all workers to stop
        for worker in self._workers:
            await worker.stop()

        # Snapshot tasks to prevent race with _on_worker_done mutating the list
        tasks_snapshot = list(self._tasks)

        # Wait for all tasks to complete with timeout
        if tasks_snapshot:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks_snapshot, return_exceptions=True),
                    timeout=_SHUTDOWN_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "worker_pool_shutdown_timeout",
                    timeout=_SHUTDOWN_TIMEOUT,
                )
                for task in tasks_snapshot:
                    task.cancel()
                # Await cancelled tasks to suppress warnings
                await asyncio.gather(*tasks_snapshot, return_exceptions=True)

        logger.info(
            "worker_pool_stopped",
            stats=self.stats,
        )

    async def add_stream(self, stream_key: str) -> None:
        """Dynamically assign a new stream to the least-loaded worker."""
        if not self._workers:
            logger.warning(
                "add_stream_no_workers",
                stream_key=stream_key,
            )
            return

        # Ensure consumer group exists before assigning
        try:
            await event_bus.ensure_consumer_group(stream_key)
        except Exception:
            logger.exception(
                "add_stream_group_create_failed",
                stream_key=stream_key,
            )

        # Find worker with fewest streams (and under cap)
        least_loaded = min(
            self._workers, key=lambda w: len(w.stream_keys)
        )
        if len(least_loaded.stream_keys) >= _MAX_STREAMS_PER_WORKER:
            logger.warning(
                "worker_stream_cap_reached",
                worker=least_loaded.worker_id,
                cap=_MAX_STREAMS_PER_WORKER,
            )
            return

        least_loaded.stream_keys.append(stream_key)

        logger.info(
            "stream_added",
            stream_key=stream_key,
            assigned_to=least_loaded.worker_id,
            worker_stream_count=len(least_loaded.stream_keys),
        )

    @property
    def stats(self) -> dict[str, Any]:
        """Aggregate statistics from all workers."""
        worker_stats = [w.stats for w in self._workers]
        return {
            "num_workers": len(self._workers),
            "total_processed": sum(s["processed"] for s in worker_stats),
            "total_errors": sum(s["errors"] for s in worker_stats),
            "workers": worker_stats,
        }
