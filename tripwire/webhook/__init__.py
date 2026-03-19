"""TripWire webhook delivery — provider abstraction + Convoy integration."""

from tripwire.webhook.convoy_client import (
    create_application,
    create_endpoint,
    force_resend,
    init_convoy,
    list_failed_deliveries,
    retry_message,
    send_webhook,
)
from tripwire.webhook.dispatcher import (
    dispatch_event,
    dispatch_generic_event,
    match_endpoints,
    match_subscriptions,
)
from tripwire.webhook.payload import (
    build_generic_payload,
    build_payment_payload,
)
from tripwire.webhook.provider import (
    ConvoyProvider,
    LogOnlyProvider,
    WebhookProvider,
    create_webhook_provider,
)
from tripwire.webhook.dlq_handler import DLQHandler
from tripwire.webhook.verify import verify_webhook

__all__ = [
    # Provider abstraction
    "WebhookProvider",
    "ConvoyProvider",
    "LogOnlyProvider",
    "create_webhook_provider",
    # Convoy client (low-level)
    "init_convoy",
    "create_application",
    "create_endpoint",
    "send_webhook",
    "retry_message",
    "list_failed_deliveries",
    "force_resend",
    # DLQ handler
    "DLQHandler",
    # Dispatcher
    "dispatch_event",
    "dispatch_generic_event",
    "match_endpoints",
    "match_subscriptions",
    # Payload builders
    "build_generic_payload",
    "build_payment_payload",
    # Verification
    "verify_webhook",
]
