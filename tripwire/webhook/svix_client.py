"""Svix SDK wrapper for TripWire webhook delivery."""

from __future__ import annotations

import structlog
from svix.api import (
    ApplicationIn,
    EndpointIn,
    MessageIn,
    SvixAsync,
)

from tripwire.config.settings import settings

logger = structlog.get_logger(__name__)

# Module-level client, initialized lazily.
_client: SvixAsync | None = None


def init_svix(api_key: str | None = None) -> SvixAsync:
    """Initialize and return an async Svix client.

    Uses the provided key or falls back to settings.svix_api_key.
    """
    global _client
    key = api_key or settings.svix_api_key
    _client = SvixAsync(key)
    logger.info("svix_client_initialized")
    return _client


def _get_client() -> SvixAsync:
    """Return the current Svix client, initializing if needed."""
    if _client is None:
        return init_svix()
    return _client


async def create_application(developer_id: str, name: str) -> str:
    """Create a Svix application for a developer.

    Returns the Svix application ID (uses developer_id as uid for idempotency).
    """
    client = _get_client()
    try:
        app = await client.application.create(
            ApplicationIn(name=name, uid=developer_id)
        )
        logger.info(
            "svix_application_created",
            developer_id=developer_id,
            app_id=app.id,
        )
        return app.id
    except Exception:
        logger.exception("svix_application_create_failed", developer_id=developer_id)
        raise


async def create_endpoint(
    app_id: str,
    url: str,
    description: str | None = None,
) -> str:
    """Register a webhook endpoint URL for a Svix application.

    Returns the Svix endpoint ID.
    """
    client = _get_client()
    try:
        endpoint = await client.endpoint.create(
            app_id,
            EndpointIn(url=url, description=description),
        )
        logger.info(
            "svix_endpoint_created",
            app_id=app_id,
            endpoint_id=endpoint.id,
            url=url,
        )
        return endpoint.id
    except Exception:
        logger.exception("svix_endpoint_create_failed", app_id=app_id, url=url)
        raise


async def send_webhook(
    app_id: str,
    event_type: str,
    payload: dict,
) -> str:
    """Send a webhook message via Svix.

    Svix handles retries, HMAC signing, and DLQ automatically.
    Returns the Svix message ID.
    """
    client = _get_client()
    try:
        msg = await client.message.create(
            app_id,
            MessageIn(event_type=event_type, payload=payload),
        )
        logger.info(
            "svix_webhook_sent",
            app_id=app_id,
            event_type=event_type,
            message_id=msg.id,
        )
        return msg.id
    except Exception:
        logger.exception(
            "svix_webhook_send_failed",
            app_id=app_id,
            event_type=event_type,
        )
        raise


async def list_messages(app_id: str):
    """List delivery history for a Svix application.

    Returns the Svix ListResponseMessageOut object.
    """
    client = _get_client()
    try:
        messages = await client.message.list(app_id)
        logger.debug("svix_messages_listed", app_id=app_id)
        return messages
    except Exception:
        logger.exception("svix_messages_list_failed", app_id=app_id)
        raise


async def retry_message(app_id: str, msg_id: str) -> None:
    """Retry delivery of a failed webhook message."""
    client = _get_client()
    try:
        await client.message_attempt.resend(app_id, msg_id)
        logger.info(
            "svix_message_retried",
            app_id=app_id,
            msg_id=msg_id,
        )
    except Exception:
        logger.exception(
            "svix_message_retry_failed",
            app_id=app_id,
            msg_id=msg_id,
        )
        raise
