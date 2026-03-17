"""Redis Streams DLQ consumer for TripWire.

Reads permanently-failed events from the ``tripwire:dlq`` stream (written by
trigger_worker.py after _MAX_RETRIES failures), logs them, fires an alert
webhook if configured, and increments a Prometheus counter.

Runs as a background asyncio task during the application lifespan — only
started when ``EVENT_BUS_ENABLED=true``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import structlog

from tripwire.api.redis import get_redis as _get_redis
from tripwire.config.settings import Settings
from tripwire.observability.health import health_registry
from tripwire.observability.metrics import tripwire_redis_dlq_total

logger = structlog.get_logger(__name__)

_DLQ_STREAM = "tripwire:dlq"
_DEFAULT_POLL_INTERVAL = 30.0  # seconds between XREAD polls
_BATCH_SIZE = 50  # max messages per XREAD call
_TRIM_THRESHOLD = 100  # trim after processing this many messages


class RedisDLQConsumer:
    """Background consumer for the Redis Streams dead-letter queue.

    Uses XREAD (not consumer groups) since this is a single consumer.
    Tracks its position via the last-seen message ID so it never
    re-processes messages across poll cycles.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._poll_interval = _DEFAULT_POLL_INTERVAL
        self._task: asyncio.Task[None] | None = None
        self._last_id = "0-0"  # start from the beginning on first run
        self._total_processed = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background polling task."""
        if self._task is not None and not self._task.done():
            logger.warning("redis_dlq_consumer_already_running")
            return
        if self._task is not None and self._task.done():
            logger.warning("redis_dlq_consumer_previous_task_finished")
            self._task = None

        health_registry.register("redis_dlq_consumer")
        self._task = asyncio.create_task(
            self._poll_loop(), name="redis-dlq-consumer"
        )
        logger.info(
            "redis_dlq_consumer_started",
            poll_interval=self._poll_interval,
        )

    async def stop(self) -> None:
        """Cancel the background task gracefully."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info(
            "redis_dlq_consumer_stopped",
            total_processed=self._total_processed,
        )

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Infinite loop that reads from the DLQ stream on a fixed interval."""
        while True:
            try:
                await self._poll_once()
                health_registry.record_run("redis_dlq_consumer")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("redis_dlq_poll_error")
                health_registry.record_error(
                    "redis_dlq_consumer", "poll error"
                )

            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                raise

    async def _poll_once(self) -> None:
        """Single poll: XREAD from last known ID, process, trim."""
        redis = _get_redis()

        # XREAD blocks for up to 5s waiting for new messages
        result = await redis.xread(
            {_DLQ_STREAM: self._last_id},
            count=_BATCH_SIZE,
            block=5000,
        )

        if not result:
            return

        processed_ids: list[str] = []

        for _stream_name, messages in result:
            for message_id, fields in messages:
                # Decode message ID if bytes
                msg_id = (
                    message_id.decode()
                    if isinstance(message_id, bytes)
                    else message_id
                )

                await self._handle_message(msg_id, fields)
                processed_ids.append(msg_id)
                self._last_id = msg_id

        # Trim old messages to prevent unbounded growth
        if len(processed_ids) >= _TRIM_THRESHOLD:
            try:
                await redis.xtrim(_DLQ_STREAM, maxlen=10000, approximate=True)
            except Exception:
                logger.exception("redis_dlq_trim_failed")

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_message(
        self, message_id: str, fields: dict[bytes | str, bytes | str]
    ) -> None:
        """Process a single DLQ message: log, alert, count."""
        # Parse the payload JSON
        raw_payload = fields.get(b"payload") or fields.get("payload", "{}")
        if isinstance(raw_payload, bytes):
            raw_payload = raw_payload.decode()

        try:
            payload: dict[str, Any] = json.loads(raw_payload)
        except (json.JSONDecodeError, TypeError):
            logger.error(
                "redis_dlq_invalid_payload",
                message_id=message_id,
                raw=str(raw_payload)[:200],
            )
            tripwire_redis_dlq_total.inc()
            self._total_processed += 1
            return

        source_stream = payload.get("source_stream", "unknown")
        source_message_id = payload.get("source_message_id", "unknown")
        error_count = payload.get("error_count", 0)
        timestamp = payload.get("timestamp", 0)
        raw_log = payload.get("raw_log", {})

        # Log the dead-lettered event
        logger.error(
            "redis_dlq_event_consumed",
            dlq_message_id=message_id,
            source_stream=source_stream,
            source_message_id=source_message_id,
            error_count=error_count,
            timestamp=timestamp,
            raw_log_keys=list(raw_log.keys()) if isinstance(raw_log, dict) else None,
        )

        # Increment Prometheus counter
        tripwire_redis_dlq_total.inc()

        # Fire alert webhook if configured
        await self._send_alert(
            dlq_message_id=message_id,
            source_stream=source_stream,
            source_message_id=source_message_id,
            error_count=error_count,
            timestamp=timestamp,
        )

        self._total_processed += 1

    # ------------------------------------------------------------------
    # Alerting
    # ------------------------------------------------------------------

    async def _send_alert(
        self,
        dlq_message_id: str,
        source_stream: str,
        source_message_id: str,
        error_count: int,
        timestamp: float,
    ) -> None:
        """POST an alert payload to DLQ_ALERT_WEBHOOK_URL if configured.

        Silently logs and returns on any failure — alerts must never crash
        the poll loop.
        """
        alert_url = self._settings.dlq_alert_webhook_url
        if not alert_url:
            return

        payload: dict[str, Any] = {
            "type": "redis_dlq.event_dead_lettered",
            "dlq_message_id": dlq_message_id,
            "source_stream": source_stream,
            "source_message_id": source_message_id,
            "error_count": error_count,
            "timestamp": timestamp,
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0)
            ) as client:
                response = await client.post(alert_url, json=payload)
                logger.info(
                    "redis_dlq_alert_sent",
                    dlq_message_id=dlq_message_id,
                    status_code=response.status_code,
                )
        except Exception:
            logger.exception(
                "redis_dlq_alert_failed",
                dlq_message_id=dlq_message_id,
                alert_url=alert_url,
            )
