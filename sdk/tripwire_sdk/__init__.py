"""TripWire SDK — Python client for programmable onchain event triggers."""

__version__ = "0.1.0"

from tripwire_sdk.client import TripwireAPIError, TripwireClient
from tripwire_sdk.signer import build_auth_message, make_auth_headers, sign_auth_message
from tripwire_sdk.types import (
    ChainId,
    Endpoint,
    EndpointMode,
    EndpointPolicies,
    Event,
    FinalityData,
    PaginatedResponse,
    Subscription,
    SubscriptionFilter,
    TransferData,
    WebhookEventType,
    WebhookPayload,
)
from tripwire_sdk.verify import (
    WebhookVerificationError,
    sign_payload,
    verify_webhook_signature,
    verify_webhook_signature_safe,
)

__all__ = [
    "TripwireClient",
    "TripwireAPIError",
    "build_auth_message",
    "sign_auth_message",
    "make_auth_headers",
    "WebhookVerificationError",
    "verify_webhook_signature",
    "verify_webhook_signature_safe",
    "sign_payload",
    "ChainId",
    "Endpoint",
    "EndpointMode",
    "EndpointPolicies",
    "Event",
    "FinalityData",
    "PaginatedResponse",
    "Subscription",
    "SubscriptionFilter",
    "TransferData",
    "WebhookEventType",
    "WebhookPayload",
]
