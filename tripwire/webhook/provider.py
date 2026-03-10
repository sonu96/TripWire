"""Webhook provider abstraction — decouples TripWire from Svix."""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

import structlog

from tripwire.config.settings import Settings

logger = structlog.get_logger(__name__)


@runtime_checkable
class WebhookProvider(Protocol):
    """Interface for webhook delivery backends.

    Implementations must provide methods for managing applications,
    endpoints, and sending webhooks. Currently backed by Svix;
    swap by implementing this protocol and updating the factory.
    """

    async def send(self, app_id: str, event_type: str, payload: dict) -> str:
        """Send a webhook message. Returns a provider-specific message ID."""
        ...

    async def create_app(self, developer_id: str, name: str) -> str:
        """Create a webhook application. Returns the provider app ID."""
        ...

    async def create_endpoint(self, app_id: str, url: str, description: str | None = None) -> str:
        """Register a webhook endpoint URL. Returns the provider endpoint ID."""
        ...


class SvixProvider:
    """WebhookProvider backed by Svix."""

    def __init__(self, api_key: str) -> None:
        from tripwire.webhook.svix_client import init_svix

        self._client = init_svix(api_key)

    async def send(self, app_id: str, event_type: str, payload: dict) -> str:
        from tripwire.webhook.svix_client import send_webhook

        return await send_webhook(app_id=app_id, event_type=event_type, payload=payload)

    async def create_app(self, developer_id: str, name: str) -> str:
        from tripwire.webhook.svix_client import create_application

        return await create_application(developer_id=developer_id, name=name)

    async def create_endpoint(self, app_id: str, url: str, description: str | None = None) -> str:
        from tripwire.webhook.svix_client import create_endpoint

        return await create_endpoint(app_id=app_id, url=url, description=description)


class LogOnlyProvider:
    """WebhookProvider that only logs — for development and testing."""

    async def send(self, app_id: str, event_type: str, payload: dict) -> str:
        msg_id = f"log-{uuid.uuid4()}"
        logger.info(
            "log_only_webhook_sent",
            app_id=app_id,
            event_type=event_type,
            message_id=msg_id,
        )
        return msg_id

    async def create_app(self, developer_id: str, name: str) -> str:
        app_id = f"log-app-{uuid.uuid4()}"
        logger.info("log_only_app_created", developer_id=developer_id, app_id=app_id)
        return app_id

    async def create_endpoint(self, app_id: str, url: str, description: str | None = None) -> str:
        endpoint_id = f"log-ep-{uuid.uuid4()}"
        logger.info("log_only_endpoint_created", app_id=app_id, endpoint_id=endpoint_id)
        return endpoint_id


def create_webhook_provider(settings: Settings) -> WebhookProvider:
    """Factory: build the appropriate WebhookProvider for the current environment."""
    if settings.svix_api_key:
        logger.info("using_svix_webhook_provider")
        return SvixProvider(api_key=settings.svix_api_key)

    logger.warning("using_log_only_webhook_provider", msg="No SVIX_API_KEY — webhooks will not be delivered")
    return LogOnlyProvider()
