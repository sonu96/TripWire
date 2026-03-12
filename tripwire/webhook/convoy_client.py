"""Convoy REST API wrapper for TripWire webhook delivery.

Architecture:
- Primary fast path: Direct async POST via httpx to the developer's webhook URL (lowest latency).
- Secondary reliable path: Queue via Convoy REST API (handles retries, DLQ, delivery logs).

Both paths fire simultaneously — fast path for speed, Convoy for guaranteed delivery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import structlog

from tripwire.config.settings import settings

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level clients — lazy-initialized
# ---------------------------------------------------------------------------

# Convoy API client (long-lived, reused across requests)
_convoy_client: httpx.AsyncClient | None = None

# Direct delivery client (separate pool, short timeouts)
_direct_client: httpx.AsyncClient | None = None


def _get_convoy_client() -> httpx.AsyncClient:
    """Return the module-level Convoy API httpx client, initializing lazily."""
    global _convoy_client
    if _convoy_client is None:
        base_url = getattr(settings, "convoy_url", "http://localhost:5005")
        api_key = getattr(settings, "convoy_api_key", "")
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


def _get_direct_client() -> httpx.AsyncClient:
    """Return the module-level direct-delivery httpx client, initializing lazily."""
    global _direct_client
    if _direct_client is None:
        _direct_client = httpx.AsyncClient(
            timeout=httpx.Timeout(5.0),
        )
        logger.info("direct_delivery_client_initialized")
    return _direct_client


# ---------------------------------------------------------------------------
# HMAC signing — Convoy-compatible format
# ---------------------------------------------------------------------------

def _build_signature(secret: str, timestamp: int, payload_json: str) -> str:
    """Return a hex HMAC-SHA256 signature over '{timestamp}.{payload_json}'.

    This matches the Convoy signing format so consumers can verify with a
    single shared secret regardless of which delivery path arrived first.
    """
    message = f"{timestamp}.{payload_json}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _signature_headers(secret: str, payload_json: str, message_id: str) -> dict[str, str]:
    """Build the TripWire signature headers for a direct delivery.

    Returns a dict with all three headers expected by verify.py:
    - ``X-TripWire-Signature`` : ``t={ts},v1={hmac_hex}``
    - ``X-TripWire-ID``        : unique message/event ID
    - ``X-TripWire-Timestamp`` : unix timestamp (same value as in signature)
    """
    ts = int(time.time())
    sig = _build_signature(secret, ts, payload_json)
    return {
        "X-TripWire-Signature": f"t={ts},v1={sig}",
        "X-TripWire-ID": message_id,
        "X-TripWire-Timestamp": str(ts),
    }


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
    key = api_key or getattr(settings, "convoy_api_key", "")
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
    client = _get_convoy_client()
    body: dict[str, Any] = {
        "name": name,
        "type": "outgoing",
        "config": {
            "strategy": {
                "type": "linear",
                "duration": 60,
                "retry_count": 5,
            }
        },
    }
    try:
        response = await client.post("/api/v1/projects", json=body)
        response.raise_for_status()
        data = response.json()
        project_id: str = data["data"]["uid"]
        logger.info(
            "convoy_project_created",
            developer_id=developer_id,
            project_id=project_id,
            name=name,
        )
        return project_id
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
    client = _get_convoy_client()
    body: dict[str, Any] = {
        "url": url,
        "description": description or "",
    }
    if secret:
        body["secret"] = secret

    try:
        response = await client.post(
            f"/api/v1/projects/{app_id}/endpoints",
            json=body,
        )
        response.raise_for_status()
        data = response.json()
        endpoint_id: str = data["data"]["uid"]
        logger.info(
            "convoy_endpoint_created",
            project_id=app_id,
            endpoint_id=endpoint_id,
            url=url,
        )
        return endpoint_id
    except Exception:
        logger.exception(
            "convoy_endpoint_create_failed",
            project_id=app_id,
            url=url,
        )
        raise


# ---------------------------------------------------------------------------
# Fast path — direct delivery
# ---------------------------------------------------------------------------

async def direct_deliver(url: str, payload: dict, secret: str) -> bool:
    """Fire-and-forget POST directly to the developer's webhook URL.

    Signs the payload with HMAC-SHA256 (``X-TripWire-Signature`` header) and
    posts with a 5-second timeout.  Returns ``True`` on any 2xx response,
    ``False`` otherwise.  Convoy handles retries when this returns ``False``.
    """
    import uuid as _uuid

    client = _get_direct_client()
    payload_json = json.dumps(payload, separators=(",", ":"))

    # Use idempotency_key as the message ID when available, otherwise generate one
    idem_key = payload.get("idempotency_key")
    message_id = idem_key or str(_uuid.uuid4())

    sig_headers = _signature_headers(secret, payload_json, message_id)

    # Include Idempotency-Key header when present in the payload
    extra_headers: dict[str, str] = {}
    if idem_key:
        extra_headers["Idempotency-Key"] = idem_key

    try:
        response = await client.post(
            url,
            content=payload_json,
            headers={
                "Content-Type": "application/json",
                **sig_headers,
                **extra_headers,
            },
        )
        success = response.is_success
        logger.info(
            "direct_delivery_attempted",
            url=url,
            status_code=response.status_code,
            success=success,
        )
        return success
    except Exception:
        logger.exception("direct_delivery_failed", url=url)
        return False


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
    client = _get_convoy_client()
    body: dict[str, Any] = {
        "event_type": event_type,
        "data": payload,
    }
    if endpoint_id:
        body["endpoint_id"] = endpoint_id
    if idempotency_key:
        body["idempotency_key"] = idempotency_key

    try:
        response = await client.post(
            f"/api/v1/projects/{app_id}/events",
            json=body,
        )
        response.raise_for_status()
        data = response.json()
        event_id: str = data["data"]["uid"]
        logger.info(
            "convoy_webhook_sent",
            project_id=app_id,
            event_type=event_type,
            event_id=event_id,
        )
        return event_id
    except Exception:
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
    """Retry delivery of a failed Convoy event delivery.

    ``delivery_id`` is the event delivery ID (not the event ID itself).
    In Convoy, each attempt to deliver an event to an endpoint is tracked
    as a separate ``EventDelivery`` record with its own ID.
    """
    client = _get_convoy_client()
    try:
        response = await client.put(
            f"/api/v1/projects/{app_id}/eventdeliveries/{delivery_id}/resend",
        )
        response.raise_for_status()
        logger.info(
            "convoy_delivery_retried",
            project_id=app_id,
            delivery_id=delivery_id,
        )
    except Exception:
        logger.exception(
            "convoy_delivery_retry_failed",
            project_id=app_id,
            delivery_id=delivery_id,
        )
        raise
