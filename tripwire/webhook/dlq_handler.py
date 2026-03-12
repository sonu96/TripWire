"""Dead Letter Queue handler for TripWire.

Polls Convoy for failed webhook deliveries, retries them up to a
configurable maximum, and fires alerts when deliveries are dead-lettered.
Runs as a background asyncio task during the application lifespan.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from tripwire.config.settings import Settings
from tripwire.db.repositories.endpoints import EndpointRepository
from tripwire.db.repositories.webhooks import WebhookDeliveryRepository
from tripwire.observability.health import health_registry
from tripwire.observability.metrics import tripwire_dlq_backlog, tripwire_errors_total
from tripwire.webhook.convoy_client import force_resend, list_failed_deliveries

logger = structlog.get_logger(__name__)


class DLQHandler:
    """Background poller that processes Convoy dead-letter deliveries.

    For each active endpoint with a ``convoy_project_id``, the handler:

    1. Fetches failed deliveries from Convoy.
    2. Attempts a batch retry via ``force_resend``.
    3. If a delivery has exceeded ``dlq_max_retries``, marks it as
       ``dead_lettered`` in the local delivery repository and fires an
       alert to the configured webhook URL.
    """

    def __init__(
        self,
        endpoint_repo: EndpointRepository,
        delivery_repo: WebhookDeliveryRepository,
        settings: Settings,
    ) -> None:
        self._endpoint_repo = endpoint_repo
        self._delivery_repo = delivery_repo
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        # Track per-delivery retry counts (convoy_delivery_uid -> count)
        self._retry_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background polling task."""
        if self._task is not None and not self._task.done():
            logger.warning("dlq_handler_already_running")
            return
        # Clear stale reference if the previous task finished unexpectedly
        if self._task is not None and self._task.done():
            logger.warning("dlq_handler_previous_task_finished_unexpectedly")
            self._task = None
        health_registry.register("dlq_handler")
        self._task = asyncio.create_task(self._poll_loop(), name="dlq-handler")
        logger.info(
            "dlq_handler_started",
            poll_interval=self._settings.dlq_poll_interval_seconds,
            max_retries=self._settings.dlq_max_retries,
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
        logger.info("dlq_handler_stopped")

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Infinite loop that calls ``_poll_once`` on a fixed interval."""
        while True:
            try:
                await self._poll_once()
                health_registry.record_run("dlq_handler")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("dlq_poll_error")
                health_registry.record_error("dlq_handler", str("poll error"))

            try:
                await asyncio.sleep(self._settings.dlq_poll_interval_seconds)
            except asyncio.CancelledError:
                raise

    async def _poll_once(self) -> None:
        """Single poll iteration across all active endpoints."""
        endpoints = self._endpoint_repo.list_active()
        total_dlq_backlog = 0

        for endpoint in endpoints:
            convoy_project_id = getattr(endpoint, "convoy_project_id", None)
            if not convoy_project_id:
                continue

            try:
                count = await self._process_endpoint(endpoint.id, convoy_project_id)
                total_dlq_backlog += count
            except Exception:
                logger.exception(
                    "dlq_endpoint_error",
                    endpoint_id=endpoint.id,
                    convoy_project_id=convoy_project_id,
                )

        # Set DLQ backlog gauge once with the total across all endpoints
        tripwire_dlq_backlog.set(total_dlq_backlog)

    # ------------------------------------------------------------------
    # Per-endpoint processing
    # ------------------------------------------------------------------

    async def _process_endpoint(
        self, endpoint_id: str, convoy_project_id: str
    ) -> int:
        """Fetch failed deliveries for one endpoint and handle them.

        Returns the number of failed deliveries found for this endpoint.
        """
        failed = await list_failed_deliveries(convoy_project_id)
        if not failed:
            return 0

        logger.info(
            "dlq_failed_deliveries_found",
            endpoint_id=endpoint_id,
            convoy_project_id=convoy_project_id,
            count=len(failed),
        )

        retryable_ids: list[str] = []
        max_retries = self._settings.dlq_max_retries

        for delivery in failed:
            delivery_uid: str = delivery.get("uid", "")
            event_id: str = self._extract_event_id(delivery)

            if not delivery_uid:
                continue

            current_count = self._retry_counts.get(delivery_uid, 0)

            if current_count >= max_retries:
                # Dead-lettered — mark and alert
                await self._dead_letter(
                    endpoint_id=endpoint_id,
                    delivery_uid=delivery_uid,
                    event_id=event_id,
                )
                # Remove from tracking to avoid re-processing
                self._retry_counts.pop(delivery_uid, None)
            else:
                retryable_ids.append(delivery_uid)
                self._retry_counts[delivery_uid] = current_count + 1

        # Batch retry the retryable deliveries
        if retryable_ids:
            try:
                await force_resend(convoy_project_id, retryable_ids)
                logger.info(
                    "dlq_batch_retry_dispatched",
                    endpoint_id=endpoint_id,
                    convoy_project_id=convoy_project_id,
                    count=len(retryable_ids),
                )
            except Exception:
                logger.exception(
                    "dlq_batch_retry_error",
                    endpoint_id=endpoint_id,
                    convoy_project_id=convoy_project_id,
                )

        return len(failed)

    # ------------------------------------------------------------------
    # Dead-letter handling
    # ------------------------------------------------------------------

    async def _dead_letter(
        self, endpoint_id: str, delivery_uid: str, event_id: str
    ) -> None:
        """Mark a delivery as dead-lettered and fire an alert."""
        tripwire_errors_total.labels(error_type="dead_lettered").inc()
        logger.warning(
            "dlq_delivery_dead_lettered",
            endpoint_id=endpoint_id,
            delivery_uid=delivery_uid,
            event_id=event_id,
        )

        # Update local delivery status if we can find the record.
        # delivery_uid is a Convoy delivery UID, so we must look up by
        # provider_message_id to find the corresponding TripWire record.
        try:
            local_delivery = self._delivery_repo.get_by_provider_message_id(delivery_uid)
            if local_delivery:
                self._delivery_repo.update_status(local_delivery["id"], "dead_lettered")
            else:
                logger.warning(
                    "dlq_no_local_delivery_found",
                    delivery_uid=delivery_uid,
                )
        except Exception:
            logger.exception(
                "dlq_update_status_error",
                delivery_uid=delivery_uid,
            )

        # Fire alert
        error_msg = (
            f"Delivery {delivery_uid} for event {event_id} exceeded "
            f"{self._settings.dlq_max_retries} retries and has been dead-lettered."
        )
        await self._send_alert(
            endpoint_id=endpoint_id,
            delivery_id=delivery_uid,
            event_id=event_id,
            error=error_msg,
        )

    # ------------------------------------------------------------------
    # Alerting
    # ------------------------------------------------------------------

    async def _send_alert(
        self,
        endpoint_id: str,
        delivery_id: str,
        event_id: str,
        error: str,
    ) -> None:
        """POST an alert payload to the configured DLQ alert webhook URL.

        Silently logs and returns on any failure — alerts must never crash
        the poll loop.
        """
        alert_url = self._settings.dlq_alert_webhook_url
        if not alert_url:
            logger.debug(
                "dlq_alert_skipped_no_url",
                endpoint_id=endpoint_id,
                delivery_id=delivery_id,
            )
            return

        payload: dict[str, Any] = {
            "type": "dlq.dead_lettered",
            "endpoint_id": endpoint_id,
            "delivery_id": delivery_id,
            "event_id": event_id,
            "error": error,
        }

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                response = await client.post(alert_url, json=payload)
                logger.info(
                    "dlq_alert_sent",
                    endpoint_id=endpoint_id,
                    delivery_id=delivery_id,
                    status_code=response.status_code,
                )
        except Exception:
            logger.exception(
                "dlq_alert_failed",
                endpoint_id=endpoint_id,
                delivery_id=delivery_id,
                alert_url=alert_url,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_event_id(delivery: dict[str, Any]) -> str:
        """Best-effort extraction of the event ID from a Convoy delivery dict."""
        # Convoy nests event metadata differently across versions
        event_metadata = delivery.get("event_metadata", {})
        if isinstance(event_metadata, dict):
            eid = event_metadata.get("uid", "")
            if eid:
                return eid
        return delivery.get("event_id", "unknown")
