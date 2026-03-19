"""TripWire SDK — Python client for programmable onchain event triggers."""

__version__ = "0.1.0"

from tripwire_sdk.client import TripwireClient
from tripwire_sdk.errors import (
    BudgetExhaustedError,
    SessionError,
    SessionExpiredError,
    TripWireAuthError,
    TripWireError,
    TripWireNotFoundError,
    TripWireRateLimitError,
    TripWireServerError,
    TripWireValidationError,
)
from tripwire_sdk.types import (
    ChainId,
    Endpoint,
    EndpointMode,
    EndpointPolicies,
    Event,
    FinalityData,
    PaginatedResponse,
    Session,
    Subscription,
    SubscriptionFilter,
    TransferData,
    TripWireBaseModel,
    WebhookData,
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
    # Client
    "TripwireClient",
    # Errors
    "BudgetExhaustedError",
    "SessionError",
    "SessionExpiredError",
    "TripWireError",
    "TripWireAuthError",
    "TripWireNotFoundError",
    "TripWireRateLimitError",
    "TripWireServerError",
    "TripWireValidationError",
    # Webhook verification
    "WebhookVerificationError",
    "verify_webhook_signature",
    "verify_webhook_signature_safe",
    "sign_payload",
    # Types
    "TripWireBaseModel",
    "ChainId",
    "Endpoint",
    "EndpointMode",
    "EndpointPolicies",
    "Event",
    "FinalityData",
    "PaginatedResponse",
    "Session",
    "Subscription",
    "SubscriptionFilter",
    "TransferData",
    "WebhookData",
    "WebhookEventType",
    "WebhookPayload",
]
