"""Webhook provider abstraction — decouples TripWire from Convoy."""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

import structlog

from tripwire.config.settings import Settings

logger = structlog.get_logger(__name__)


@runtime_checkable
class WebhookProvider(Protocol):
    """Interface for webhook delivery backends.

    Implementations must provide methods for managing projects,
    endpoints, and sending webhooks. Currently backed by Convoy;
    swap by implementing this protocol and updating the factory.
    """

    async def send(self, app_id: str, event_type: str, payload: dict) -> str:
        """Send a webhook message via the managed Convoy project. Returns a provider-specific message ID."""
        ...

    async def create_app(self, developer_id: str, name: str) -> str:
        """Create a webhook project. Returns the provider project ID."""
        ...

    async def create_endpoint(self, app_id: str, url: str, description: str | None = None, secret: str | None = None) -> str:
        """Register a webhook endpoint URL. Returns the provider endpoint ID."""
        ...

class ConvoyProvider:
    """WebhookProvider backed by Convoy self-hosted."""

    def __init__(self, api_key: str, convoy_url: str) -> None:
        self._api_key = api_key
        self._convoy_url = convoy_url
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Lazily initialize the Convoy HTTP client on first async call."""
        if not self._initialized:
            from tripwire.webhook.convoy_client import init_convoy

            await init_convoy(self._api_key, self._convoy_url)
            self._initialized = True

    async def send(self, app_id: str, event_type: str, payload: dict) -> str:
        """Send a webhook event to all endpoints registered under the given Convoy project.

        ``app_id`` is treated as ``project_id`` in Convoy terminology.
        ``endpoint_id`` is derived from the project; pass via payload meta when needed.
        Returns the Convoy event ID.
        """
        await self._ensure_initialized()
        from tripwire.webhook.convoy_client import send_webhook

        # app_id carries the convoy project_id; endpoint_id is embedded as metadata
        # when the caller also has an endpoint_id it passes it via the payload dict
        # under the reserved key "__convoy_endpoint_id__" which we pop here.
        endpoint_id: str = payload.pop("__convoy_endpoint_id__", "")
        idempotency_key: str = payload.get("idempotency_key", "")
        return await send_webhook(
            app_id=app_id,
            event_type=event_type,
            payload=payload,
            endpoint_id=endpoint_id,
            idempotency_key=idempotency_key or None,
        )

    async def create_app(self, developer_id: str, name: str) -> str:
        """Create a Convoy project for the given developer. Returns the project ID."""
        await self._ensure_initialized()
        from tripwire.webhook.convoy_client import create_application

        return await create_application(developer_id=developer_id, name=name)

    async def create_endpoint(self, app_id: str, url: str, description: str | None = None, secret: str | None = None) -> str:
        """Register a webhook endpoint in the given Convoy project. Returns the endpoint ID."""
        await self._ensure_initialized()
        from tripwire.webhook.convoy_client import create_endpoint as _create_endpoint

        return await _create_endpoint(
            app_id=app_id,
            url=url,
            description=description or "",
            secret=secret or "",
        )

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

    async def create_endpoint(self, app_id: str, url: str, description: str | None = None, secret: str | None = None) -> str:
        endpoint_id = f"log-ep-{uuid.uuid4()}"
        logger.info(
            "log_only_endpoint_created",
            app_id=app_id,
            url=url,
            endpoint_id=endpoint_id,
        )
        return endpoint_id

def create_webhook_provider(settings: Settings) -> WebhookProvider:
    """Factory: build the appropriate WebhookProvider for the current environment."""
    if settings.convoy_api_key.get_secret_value():
        logger.info("using_convoy_webhook_provider", convoy_url=settings.convoy_url)
        return ConvoyProvider(api_key=settings.convoy_api_key.get_secret_value(), convoy_url=settings.convoy_url)

    logger.warning(
        "using_log_only_webhook_provider",
        msg="No CONVOY_API_KEY — webhooks will not be delivered",
    )
    return LogOnlyProvider()
