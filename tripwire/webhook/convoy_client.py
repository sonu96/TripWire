"""Convoy REST API wrapper for TripWire webhook delivery.

All webhook deliveries are routed through Convoy for reliable delivery with
retries, DLQ, and delivery logs.
"""

from __future__ import annotations

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
