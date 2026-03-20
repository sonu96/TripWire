"""Convoy REST API wrapper for TripWire webhook delivery.

All webhook deliveries are routed through Convoy for reliable delivery with
retries, DLQ, and delivery logs.
"""

from __future__ import annotations

from typing import Any

import time

import httpx
import structlog

from tripwire.config.settings import settings
from tripwire.observability.metrics import (
    tripwire_convoy_circuit_state,
    tripwire_webhook_delivery_duration_seconds,
    tripwire_webhooks_sent_total,
)
from tripwire.observability.tracing import tracer, StatusCode

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Circuit breaker — protects against Convoy being down
# ---------------------------------------------------------------------------

class ConvoyCircuitOpenError(Exception):
    """Raised when the Convoy circuit breaker is open and requests are rejected fast."""


# State machine: closed (normal) → open (failing) → half_open (probing)
_circuit_state: str = "closed"
_failure_count: int = 0
_last_failure_time: float = 0.0
_FAILURE_THRESHOLD: int = 5        # consecutive failures to trip the circuit
_RECOVERY_TIMEOUT: float = 30.0    # seconds before allowing a probe request
_HALF_OPEN_TIMEOUT: float = 10.0   # shorter timeout for probe requests


def _check_circuit() -> bool:
    """Return True if the circuit allows the request, False to reject fast."""
    global _circuit_state

    if _circuit_state == "closed":
        return True

    if _circuit_state == "open":
        if time.monotonic() - _last_failure_time > _RECOVERY_TIMEOUT:
            _circuit_state = "half_open"
            tripwire_convoy_circuit_state.set(2)
            logger.info("convoy_circuit_half_open", after_seconds=_RECOVERY_TIMEOUT)
            return True  # allow one probe
        return False  # reject fast

    # half_open — allow the probe request through
    return True


def _record_success() -> None:
    """Record a successful Convoy call; reset the circuit to closed."""
    global _circuit_state, _failure_count
    if _circuit_state != "closed":
        logger.info("convoy_circuit_closed", previous_state=_circuit_state)
    _circuit_state = "closed"
    _failure_count = 0
    tripwire_convoy_circuit_state.set(0)


def _record_failure() -> None:
    """Record a failed Convoy call; open the circuit after threshold breached."""
    global _circuit_state, _failure_count, _last_failure_time
    _failure_count += 1
    _last_failure_time = time.monotonic()
    if _failure_count >= _FAILURE_THRESHOLD:
        _circuit_state = "open"
        tripwire_convoy_circuit_state.set(1)
        logger.warning("convoy_circuit_opened", failures=_failure_count)


def _guard_circuit() -> None:
    """Check the circuit breaker; raise ConvoyCircuitOpenError if open."""
    if not _check_circuit():
        raise ConvoyCircuitOpenError(
            f"Convoy circuit breaker is open (failures={_failure_count}, "
            f"recovery in {_RECOVERY_TIMEOUT - (time.monotonic() - _last_failure_time):.1f}s)"
        )


def get_circuit_state() -> str:
    """Return the current circuit breaker state for diagnostics."""
    return _circuit_state

# ---------------------------------------------------------------------------
# Module-level clients — lazy-initialized
# ---------------------------------------------------------------------------

# Convoy API client (long-lived, reused across requests)
_convoy_client: httpx.AsyncClient | None = None

def _get_convoy_client() -> httpx.AsyncClient:
    """Return the module-level Convoy API httpx client, initializing lazily."""
    global _convoy_client
    if _convoy_client is None:
        base_url = getattr(settings, "convoy_url", "http://localhost:5005")
        api_key = settings.convoy_api_key.get_secret_value()
        _convoy_client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(30.0),
        )
        logger.info("convoy_client_initialized", base_url=base_url)
    return _convoy_client


# ---------------------------------------------------------------------------
# Convoy project management
# ---------------------------------------------------------------------------

async def init_convoy(api_key: str | None = None, base_url: str | None = None) -> httpx.AsyncClient:
    """Re-initialize the Convoy client with an explicit API key and/or base URL.

    Normally the client is initialized lazily from settings; call this only
    when you need to swap credentials at runtime.

    Returns the underlying httpx.AsyncClient for inspection / testing.
    """
    global _convoy_client
    base_url = base_url or getattr(settings, "convoy_url", "http://localhost:5005")
    key = api_key or settings.convoy_api_key.get_secret_value()
    _convoy_client = httpx.AsyncClient(
        base_url=base_url,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=httpx.Timeout(30.0),
    )
    logger.info("convoy_client_reinitialized", base_url=base_url)
    return _convoy_client


async def create_application(developer_id: str, name: str) -> str:
    """Create a Convoy project for a developer.

    Uses ``developer_id`` as the
    project name prefix for human-readable identification.

    Returns the Convoy project ID (``data.uid``).
    """
    _guard_circuit()
    client = _get_convoy_client()
    body: dict[str, Any] = {
        "name": name,
        "type": "outgoing",
        "config": {
            "strategy": {
                "type": "exponential",
                "duration": 10,
                "retry_count": 10,
            }
        },
    }
    try:
        response = await client.post("/api/v1/projects", json=body)
        response.raise_for_status()
        data = response.json()
        project_id: str = data["data"]["uid"]
        _record_success()
        logger.info(
            "convoy_project_created",
            developer_id=developer_id,
            project_id=project_id,
            name=name,
        )
        return project_id
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        _record_failure()
        logger.exception(
            "convoy_project_create_failed",
            developer_id=developer_id,
            name=name,
        )
        raise
    except Exception:
        logger.exception(
            "convoy_project_create_failed",
            developer_id=developer_id,
            name=name,
        )
        raise


# ---------------------------------------------------------------------------
# Convoy endpoint
# ---------------------------------------------------------------------------

async def create_endpoint(
    app_id: str,
    url: str,
    description: str | None = None,
    secret: str | None = None,
) -> str:
    """Register a webhook endpoint URL for a Convoy project.

    ``app_id`` maps to the Convoy project ID returned by ``create_application``.
    ``secret`` is used for HMAC signing; if omitted Convoy generates one.

    Returns the Convoy endpoint ID.
    """
    _guard_circuit()
    client = _get_convoy_client()
    if not secret:
        raise ValueError("secret is required — never let Convoy auto-generate")
    body: dict[str, Any] = {
        "name": description or f"endpoint-{url}",
        "url": url,
        "description": description or "",
        "secret": secret,
    }

    try:
        response = await client.post(
            f"/api/v1/projects/{app_id}/endpoints",
            json=body,
        )
        response.raise_for_status()
        data = response.json()
        endpoint_id: str = data["data"]["uid"]
        _record_success()
        logger.info(
            "convoy_endpoint_created",
            project_id=app_id,
            endpoint_id=endpoint_id,
            url=url,
        )
        return endpoint_id
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        _record_failure()
        logger.exception(
            "convoy_endpoint_create_failed",
            project_id=app_id,
            url=url,
        )
        raise
    except Exception:
        logger.exception(
            "convoy_endpoint_create_failed",
            project_id=app_id,
            url=url,
        )
        raise


# ---------------------------------------------------------------------------
# Convoy event (send webhook)
# ---------------------------------------------------------------------------

async def send_webhook(
    app_id: str,
    event_type: str,
    payload: dict,
    endpoint_id: str | None = None,
    idempotency_key: str | None = None,
) -> str:
    """Send a webhook event via Convoy (reliable path).

    ``app_id`` maps to the Convoy project ID.  If ``endpoint_id`` is supplied
    the event is targeted at that specific endpoint; otherwise Convoy fans it
    out to all subscribed endpoints for the project.

    Returns the Convoy event ID.
    """
    _guard_circuit()
    with tracer.start_as_current_span("convoy_send_webhook") as span:
        span.set_attribute("convoy.project_id", app_id)
        span.set_attribute("convoy.event_type", event_type)
        if endpoint_id:
            span.set_attribute("convoy.endpoint_id", endpoint_id)

        client = _get_convoy_client()
        # Use a shorter timeout when probing in half_open state
        timeout_override = (
            httpx.Timeout(_HALF_OPEN_TIMEOUT)
            if _circuit_state == "half_open"
            else None
        )
        body: dict[str, Any] = {
            "event_type": event_type,
            "data": payload,
        }
        if endpoint_id:
            body["endpoint_id"] = endpoint_id
        if idempotency_key:
            body["idempotency_key"] = idempotency_key

        send_start = time.perf_counter()
        try:
            response = await client.post(
                f"/api/v1/projects/{app_id}/events",
                json=body,
                **({"timeout": timeout_override} if timeout_override else {}),
            )
            response.raise_for_status()
            tripwire_webhook_delivery_duration_seconds.observe(
                time.perf_counter() - send_start
            )
            data = response.json()
            event_id: str = data["data"]["uid"]
            _record_success()
            tripwire_webhooks_sent_total.labels(status="success", mode="execute").inc()
            span.set_attribute("convoy.status", "success")
            logger.info(
                "convoy_webhook_sent",
                project_id=app_id,
                event_type=event_type,
                event_id=event_id,
            )
            return event_id
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            tripwire_webhook_delivery_duration_seconds.observe(
                time.perf_counter() - send_start
            )
            _record_failure()
            tripwire_webhooks_sent_total.labels(status="failed", mode="execute").inc()
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            span.set_attribute("convoy.status", "failed")
            logger.exception(
                "convoy_webhook_send_failed",
                project_id=app_id,
                event_type=event_type,
            )
            raise
        except Exception as exc:
            tripwire_webhook_delivery_duration_seconds.observe(
                time.perf_counter() - send_start
            )
            tripwire_webhooks_sent_total.labels(status="failed", mode="execute").inc()
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            span.set_attribute("convoy.status", "failed")
            logger.exception(
                "convoy_webhook_send_failed",
                project_id=app_id,
                event_type=event_type,
            )
            raise


# ---------------------------------------------------------------------------
# Dead-letter queue helpers
# ---------------------------------------------------------------------------


async def list_failed_deliveries(
    project_id: str,
    *,
    per_page: int = 50,
    page: int = 1,
) -> list[dict[str, Any]]:
    """List failed event deliveries for a Convoy project.

    Queries the Convoy event deliveries endpoint filtered by ``status=Failed``.
    Returns a list of raw delivery dicts from the API response.
    """
    client = _get_convoy_client()
    try:
        response = await client.get(
            f"/api/v1/projects/{project_id}/eventdeliveries",
            params={"status": "Failed", "perPage": per_page, "page": page},
        )
        response.raise_for_status()
        data = response.json()
        deliveries: list[dict[str, Any]] = data.get("data", {}).get("content", [])
        logger.debug(
            "convoy_failed_deliveries_listed",
            project_id=project_id,
            count=len(deliveries),
        )
        return deliveries
    except Exception:
        logger.exception("convoy_failed_deliveries_list_failed", project_id=project_id)
        raise


async def force_resend(project_id: str, delivery_ids: list[str]) -> None:
    """Batch-retry failed event deliveries for a Convoy project.

    Sends a POST to the Convoy batch retry endpoint with the given delivery IDs.
    """
    client = _get_convoy_client()
    try:
        response = await client.post(
            f"/api/v1/projects/{project_id}/eventdeliveries/batchretry",
            json={"ids": delivery_ids},
        )
        response.raise_for_status()
        logger.info(
            "convoy_batch_retry_sent",
            project_id=project_id,
            delivery_count=len(delivery_ids),
        )
    except Exception:
        logger.exception(
            "convoy_batch_retry_failed",
            project_id=project_id,
            delivery_ids=delivery_ids,
        )
        raise


# ---------------------------------------------------------------------------
# Retry event delivery (replaces retry_message)
# ---------------------------------------------------------------------------

async def retry_message(app_id: str, delivery_id: str) -> None:
    """Retry delivery of a failed Convoy event.

    ``delivery_id`` here is actually the Convoy **event ID** as stored in
    ``webhook_deliveries.provider_message_id``.  Because TripWire stores the
    event ID (returned by ``send_webhook``) rather than the event-delivery ID,
    we first look up the event deliveries for this event and then retry
    the failed ones.

    NOTE: The ``webhook_deliveries`` table stores the Convoy *event* ID in
    ``provider_message_id``, not the Convoy *event-delivery* ID.  A future
    migration could add a ``provider_delivery_id`` column to store the
    event-delivery ID directly and skip this lookup.
    """
    client = _get_convoy_client()

    # Step 1: Look up event deliveries for the given event ID
    try:
        resp = await client.get(
            f"/api/v1/projects/{app_id}/eventdeliveries",
            params={"event_id": delivery_id},
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("data", {}).get("content", [])
    except Exception:
        logger.exception(
            "convoy_delivery_lookup_failed",
            project_id=app_id,
            event_id=delivery_id,
        )
        raise

    # Step 2: Find failed deliveries and retry them
    failed_ids = [
        d["uid"] for d in content
        if d.get("status", "").lower() in ("failure", "failed")
    ]

    if not failed_ids:
        # Fall back: retry all deliveries for this event (Convoy will skip
        # already-successful ones)
        failed_ids = [d["uid"] for d in content if d.get("uid")]

    if not failed_ids:
        logger.warning(
            "convoy_no_deliveries_for_event",
            project_id=app_id,
            event_id=delivery_id,
        )
        raise ValueError(
            f"No Convoy event deliveries found for event {delivery_id}"
        )

    # Step 3: Retry each failed delivery
    for ed_id in failed_ids:
        try:
            response = await client.put(
                f"/api/v1/projects/{app_id}/eventdeliveries/{ed_id}/resend",
            )
            response.raise_for_status()
            logger.info(
                "convoy_delivery_retried",
                project_id=app_id,
                event_id=delivery_id,
                event_delivery_id=ed_id,
            )
        except Exception:
            logger.exception(
                "convoy_delivery_retry_failed",
                project_id=app_id,
                event_id=delivery_id,
                event_delivery_id=ed_id,
            )
            raise
