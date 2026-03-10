"""TripWire webhook delivery — provider abstraction + Svix integration."""

from tripwire.webhook.dispatcher import (
    dispatch_event,
    match_endpoints,
    match_subscriptions,
)
from tripwire.webhook.provider import (
    LogOnlyProvider,
    SvixProvider,
    WebhookProvider,
    create_webhook_provider,
)
from tripwire.webhook.svix_client import (
    create_application,
    create_endpoint,
    init_svix,
    list_messages,
    retry_message,
    send_webhook,
)
from tripwire.webhook.verify import verify_webhook

__all__ = [
    # Provider abstraction
    "WebhookProvider",
    "SvixProvider",
    "LogOnlyProvider",
    "create_webhook_provider",
    # Svix client (low-level)
    "init_svix",
    "create_application",
    "create_endpoint",
    "send_webhook",
    "list_messages",
    "retry_message",
    # Dispatcher
    "dispatch_event",
    "match_endpoints",
    "match_subscriptions",
    # Verification
    "verify_webhook",
]
